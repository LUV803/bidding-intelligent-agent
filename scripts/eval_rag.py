"""RAG 质量评估：用大模型当裁判，按 RAGAS 的口径算出四个可对比的数字。

**这不是 RAGAS**。RAGAS 的依赖树（datasets / langchain / rapidfuzz / 新版 openai）与本项目
锁定的版本冲突装不进去，所以这里把它那四个指标的**公开定义**用「LLM 当裁判」在本地复刻：
保留方法论，**不继承它的代码，也不要拿这里的数字去对标任何 RAGAS 的公开数字**。

| 指标 | 定义 | 实现 |
| --- | --- | --- |
| faithfulness | 答案中被检索资料支持的陈述 / 全部陈述 | ① LLM 拆陈述 ② 批量判「资料能否支持」 |
| answer_relevancy | 从答案反推 n 个问题，与原问题的平均向量相似度 | ① LLM 反推 3 个问题 ② 本地 BGE 编码算余弦 |
| context_recall | 参考答案中能归因于检索资料的陈述 / 全部陈述 | ① 标点切句 ② 批量判「能否归因」 |
| context_precision | Σ_k (precision@k × v_k) / 相关总数 | ① 逐条判「与问题相关与否」② 套公式 |

它的用途是**数据驱动优化**：改一个旋钮（换检索模式、调闸门阈值、开关重排）跑两次，
对比两份 `<run>.summary.json` —— 数字变好还是变差一目了然，并且能直接指出**是哪几道题坏了**。

跑法：

    uv run python scripts/eval_rag.py --selftest          # 不联网、不花钱，验打分数学
    uv run python scripts/eval_rag.py --limit 5           # 小样：语料前 5 条
    uv run python scripts/eval_rag.py                     # 全量：语料 74 条（回归基线）
    uv run python scripts/eval_rag.py --questions data/processed/eval_questions.jsonl
    uv run python scripts/eval_rag.py --rerank --out data/eval/rerank-on

七条必须知道的局限（否则数字会被误读，详见 CLAUDE.md 的「RAG 质量评估」一节）：

1. **裁判和被评模型是同一个 DeepSeek** —— 天然偏袒自己生成的答案。绝对值不可尽信，
   **只拿同一份数据集上的相对变化做决策**。
2. **数字不可跨项目比对** —— 嵌入模型是 bge-small-zh（RAGAS 默认 ada-002），
   公式实现也未必逐位一致。
3. **语料自测是回归基线** —— 74 条问答对本身就在索引里，检索必然命中原文，
   `context_recall` 会接近 1.0。它测的是「有没有把已经对的东西弄坏」，不是检索的真实水平。
4. **留出集不是独立评测集** —— 改写题由同一个模型生成，仍偏向原问法的词汇。
5. **空检索单独成桶、不进均值** —— 闸门筛空时模型会回一句「暂未找到依据」，
   而那句拒答恰好被空上下文「支持」，faithfulness 会是满分。混进均值就是自欺。
6. **⚠⚠ 先量噪声底再看数字**（2026-09-20 实测）：同一批答案、同一个提示词**连跑两次**，
   全量均值差 **0.0045**、逐题有 **3/74 不同且单题极差达 0.333**，连拆出的陈述总条数都
   是 362 vs 369。⇒ **均值动 0.01 以上才当信号；单题分数绝不能用来看结论**。
   判「是不是机制性变化」看 **n（陈述条数）有没有变**：n 变了才是，n 没变基本都是抖动。
   来源是**判定步**：`temperature=0.0` 在本项目这个推理型模型上不等于确定性。
7. **语料里有一条坏数据**：`qa-0046` 的参考答案是 `'2023年上半年手术机器人招采情况如下：'`
   （20 字、冒号结尾、没有内容），它的 `context_recall=0.0` 是**指标正确报出坏数据**。
   ⚠ 这是**唯一**一条，只把该指标均值从 0.987 拉到 0.9867，**不影响任何结论**。

⚠ **跑之前必须先停掉后端服务**：本地 Qdrant 是嵌入式文件模式（`QdrantClient(path=...)`），
同一目录独占锁，服务在跑时这个脚本连不上。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa.config import settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------- 指标名 ----------
M_FAITH = "faithfulness"
M_RELEV = "answer_relevancy"
M_RECALL = "context_recall"
M_PRECISION = "context_precision"
ALL_METRICS = (M_FAITH, M_RELEV, M_RECALL, M_PRECISION)

# 检查点文件名里用的短名。⚠⚠ **不能拿 `name[:3]` 图省事**：`context_recall` 与
# `context_precision` 的前三个字母都是 `con` ⇒ 两次不同的运行**共用同一个检查点文件**，
# 后跑的那个把先跑的覆盖掉，而 `--resume` 还会拿前一份的 id 集合去跳过题目 ——
# 表现是「换了指标重跑，数字一模一样」，属于本仓库最警惕的那类静默错误。
# 所以写死一张表，并用 import 期的 assert 挡住漏改（照 agent.py 的 QUERY_TYPE_LABELS 惯例）。
_METRIC_SHORT = {
    M_FAITH: "fai",
    M_RELEV: "rel",
    M_RECALL: "rec",
    M_PRECISION: "pre",
}
assert set(_METRIC_SHORT) == set(ALL_METRICS), "增删指标时 _METRIC_SHORT 要同步"
assert len(set(_METRIC_SHORT.values())) == len(ALL_METRICS), "指标短名必须两两不同"

# 一次裁判调用最多判几条陈述。太大时模型容易漏条（漏的按「不支持」计，会压低分数），
# 太小时调用次数翻倍。8 条实测很少漏。措辞见下面 _verdict_prompt 的「必须覆盖」那句。
JUDGE_BATCH = 8
# answer_relevancy 反推几个问题。RAGAS 默认 3，照抄。
RELEVANCY_N = 3
# 单条资料进裁判提示词的截断上限。语料答案最长 500 字，正常情况下碰不到这个线；
# 它是为了防止 `--questions` 换成别处的长文档时把提示词撑爆。
CONTEXT_CHARS = 1200
# 连续多少题「一个指标都没算出来」就判定整轮不可用（通常是 Key 失效 / 欠费）。
# 没有它，一次跑一小时才知道全废。
ABORT_AFTER_BARREN = 5

_pass = 0
_fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _pass, _fail
    if ok:
        _pass += 1
        print(f"  [OK  ] {label}")
    else:
        _fail += 1
        print(f"  [FAIL] {label}")
        if detail:
            print(f"         {detail}")


def banner(text: str) -> None:
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)


# ============================================================================
# 宽容 JSON 解析
# ============================================================================


def _unwrap_fence(text: str) -> str:
    """剥掉 markdown 代码围栏。找不到围栏就原样返回。"""
    raw = (text or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
    return match.group(1).strip() if match else raw


def _extract_json(text: str) -> Any | None:
    """从模型输出里抠出 JSON —— **对象和数组都认**，抠不出来返回 None（调用方降级，不报错）。

    照抄 `vision._extract_json()` 的两轮思路（围栏剥离在「内容里本身含 ``` 」时会截断，
    那一轮用原文还能救回来），但**刻意不复用那个函数**：它是私有函数，且返回值写死
    `dict | None`（末尾那句 `isinstance(data, dict)`）。拆陈述返回的是 **JSON 数组**，
    用它会静默返回 None —— 表现是 faithfulness 整列变成 null，看起来像「模型不配合」。
    """
    stripped = (text or "").strip()
    for candidate in (_unwrap_fence(stripped), stripped):
        starts = [i for i in (candidate.find("["), candidate.find("{")) if i >= 0]
        if not starts:
            continue
        start = min(starts)
        closer = "]" if candidate[start] == "[" else "}"
        end = candidate.rfind(closer)
        if end <= start:
            continue
        slice_ = candidate[start : end + 1]
        # ⚠ 两轮：默认解析失败后**再试一次 `strict=False`**。
        #   模型真会写出「字符串里带裸换行」的 JSON（把一条多行的改写直接塞进数组）——
        #   那在 JSON 规范里是非法的（控制字符必须转义），默认解析器会直接拒掉，
        #   表现就是这个函数返回 None 而调用方把它当「模型没配合」。`strict=False` 正是
        #   为这种输入准备的开关，一行就能救回来。
        data = None
        for loader in (
            lambda s: json.loads(s),
            lambda s: json.loads(s, strict=False),
        ):
            try:
                data = loader(slice_)
                break
            except ValueError:
                continue
        if data is None:
            continue
        if isinstance(data, (list, dict)):
            return data
    return None


_TRUE_WORDS = {"true", "1", "yes", "y", "是", "支持", "相关", "可以", "能", "有", "属于"}
_FALSE_WORDS = {"false", "0", "no", "n", "否", "不支持", "不相关", "不能", "无", "不属于"}


def _as_bool(value: Any) -> bool | None:
    """把模型给的任意值转成 bool，认不出来返回 None。

    ⚠ 必须宽容：模型会写 `true` / `"true"` / `"是"` / `"支持"` / `1`。
    按 `is True` 判会让**大半的「支持」被判成没答**，而没答是按「不支持」计的 ——
    分数被无声压掉一截，且看起来完全像是答案本身不忠实。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            return int(match.group())
    return None


def _index_verdicts(data: Any, keys: tuple[str, ...], total: int) -> dict[int, bool]:
    """把 `[{"i":1,"relevant":true}, ...]` 收成 `{1: True}`。

    `i` 从 **1** 开始（与提示词里「必须覆盖 1..N」对齐）。越界 / 缺字段 / 判不出来的
    一律跳过 —— 它们在打分时按「没判出来」处理，由覆盖率兜住。
    """
    out: dict[int, bool] = {}
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        idx = None
        for key in ("i", "index", "id", "no", "编号", "序号"):
            if key in item:
                idx = _as_int(item[key])
                if idx is not None:
                    break
        if idx is None or not (1 <= idx <= total) or idx in out:
            continue
        for key in keys:
            if key in item:
                verdict = _as_bool(item[key])
                if verdict is not None:
                    out[idx] = verdict
                    break
    return out


# ============================================================================
# 打分（纯函数 —— --selftest 直接打这几条，不需要任何服务）
# ============================================================================


def score_ratio(flags: list[bool | None]) -> tuple[float | None, float]:
    """返回 `(比例, 覆盖率)`。

    `None` 表示这一条**没判出来**（模型漏条或格式判错）。它按「不支持」计入分子分母
    （保守），但覆盖率会如实反映有多少条没判出来 —— 覆盖率低是**解析坏了**的信号，
    不是答案坏了。两者一起看才不会被误导。
    """
    total = len(flags)
    if total == 0:
        return None, 0.0
    supported = sum(1 for f in flags if f is True)
    judged = sum(1 for f in flags if f is not None)
    return supported / total, judged / total


def score_context_precision(flags: list[bool]) -> float | None:
    """标准 `ContextPrecision@K`：`Σ_k (precision@k × v_k) / 相关总数`。

    ⚠ **一条都不相关时返回 0.0 而不是 None**：那是「检索回来一堆没用的」这个**事实**，
    必须进均值；`None` 留给「算不出来」（空检索 / 判定失败）。两者混为一谈会让
    「检索质量掉了」这件事在报表上消失。
    """
    if not flags:
        return None
    total_relevant = sum(1 for f in flags if f)
    if total_relevant == 0:
        return 0.0
    hits = 0
    acc = 0.0
    for k, flag in enumerate(flags, start=1):
        if flag:
            hits += 1
            acc += hits / k
    return acc / total_relevant


def _score_ratio_result(
    result: dict[str, Any], flags: list[bool | None], errors: list[str]
) -> dict[str, Any]:
    """把判定结果写进 `result`。faithfulness 与 context_recall 共用（两者同形）。

    ⚠ **覆盖率低于一半时判为「算不出来」（None）而不是给个低分**：那是**裁判坏了**
    的证据，不是答案差的证据。一个全线失败的裁判会给出漂亮的 `0.0`，看起来像「模型在
    胡说八道」—— 这条守卫就是不让那种数字进报表。
    """
    value, coverage = score_ratio(flags)
    if value is not None and coverage < 0.5:
        result["error"] = f"裁判只判出 {coverage:.0%} 的条目，这一项不予采信（{'; '.join(errors[:2])}）"
        result["coverage"] = coverage
        return result
    result["value"] = value
    result["coverage"] = coverage
    if coverage < 1.0:
        missed = len(flags) - int(round(coverage * len(flags)))
        detail = f"（{errors[0]}）" if errors else ""
        result["error"] = f"{missed} 条未判出结果，已按不支持计{detail}"
    return result


def score_similarity(matrix: Any) -> float | None:
    """`encode_documents` 出来的矩阵（已 L2 归一化）→ 第一行与其余各行的平均余弦。

    已归一化 ⇒ 余弦就是点积，不用再除模长。
    """
    if matrix is None or len(matrix) < 2:
        return None
    base = matrix[0]
    sims = [float(base @ row) for row in matrix[1:]]
    return sum(sims) / len(sims) if sims else None


# ============================================================================
# 提示词
# ============================================================================

_SPLIT_SYSTEM = "你是严谨的文本分析助手，擅长把一段话拆成彼此独立的原子陈述。"

_SPLIT_USER = """把下面这段【回答】拆解成若干条**原子陈述**。

要求：
1. 每条只包含一个可独立判断真假的事实或主张；
2. 不要改写、不要概括，尽量保留原文措辞与数字；
3. 客套话、过渡句、纯格式文字（「首先」「综上」「以上就是」）不要单独成条；
4. 如果这段回答只是在表示「没有找到依据 / 无法回答」，把它整体作为**一条**陈述；
5. 只输出 JSON 数组，形如 ["陈述一", "陈述二"]，不要任何其他文字。

【回答】
{answer}"""

_VERDICT_SYSTEM = "你是事实核查员，只根据给定资料判断，不使用自己的先验知识。"

_FAITH_VERDICT_USER = """下面给出【资料】和【待核查陈述】。请逐条判断：该陈述的内容**能否在资料中找到依据**。

判定标准：
- 资料里写明了、或可由资料直接推出 → 支持
- 资料里没提、与资料矛盾、或属于资料之外的事实与猜测 → 不支持
- 不要判断这条陈述本身对不对，只看**资料**能不能支持它
- 陈述里标注的引用序号（如 [1][2]）不参与判断，忽略它们

只输出 JSON 数组，每条一个对象，形如：
[{{"i": 1, "supported": true}}, {{"i": 2, "supported": false}}]
必须覆盖 1..{n} 每一条，一条都不能少。

【资料】
{context}

【待核查陈述】
{items}"""

_RECALL_VERDICT_USER = """下面给出【资料】和【参考要点】。请逐条判断：该要点**能否归因于资料**。

判定标准：
- 资料里写明了、或可由资料直接推出 → 可以归因
- 资料里完全没提到的要点 → 不能归因
- ⚠ 注意方向：这里问的是「**资料能不能支撑这条参考要点**」，
  不是「这条要点讲得对不对」。资料没提到就是不能归因。

只输出 JSON 数组，每条一个对象，形如：
[{{"i": 1, "attributable": true}}, {{"i": 2, "attributable": false}}]
必须覆盖 1..{n} 每一条，一条都不能少。

【资料】
{context}

【参考要点】
{items}"""

_RELEV_SYSTEM = "你是提问生成器，负责从一段回答反推出它原本在回答什么。"

_RELEV_USER = """下面是一段【回答】。请反推出 {n} 个**这段回答所针对的问题**，要像真实用户会问的那样。

要求：
1. {n} 个问题问的是同一个意思，但措辞各不相同；
2. 只根据回答的内容提问，不要发挥、不要引入回答里没有的限定条件；
3. 只输出 JSON 数组，形如 ["问题一", "问题二", "问题三"]，不要任何其他文字。

【回答】
{answer}"""

_PRECISION_SYSTEM = "你是检索质量评估员，判断检索到的资料对回答某个问题有没有用。"

_PRECISION_USER = """【问题】
{question}

下面是从知识库检索到的 {n} 条资料。请逐条判断：这条资料**对回答上面这个问题有没有用**？

判据是「这条资料里有没有能用来回答该问题的内容」，**不是**「它和问题有没有共同的词」。
只是字面相近但答非所问的，判**没用**。

只输出 JSON 数组，每条一个对象，形如：
[{{"i": 1, "relevant": true}}, {{"i": 2, "relevant": false}}]
必须覆盖 1..{n} 每一条，一条都不能少。

{context}"""


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {text}" for i, text in enumerate(items, start=1))


def _contexts_text(contexts: list[str]) -> str:
    """把检索到的资料渲染成【资料N】块。

    ⚠ 这里**刻意等价于** `rag.build_context()` 的知识库分支（`【资料n】\\n答案：…`，
    只是省掉了「问题：」那一行 —— 评估用的是答案正文，那才是送进模型的部分）。
    评估脚本不该 import 生产模块里那个函数：它在 `--selftest` 下会连带拉起
    heavy 的 import，而这里只需要一段文本。
    """
    blocks = []
    for i, text in enumerate(contexts, start=1):
        body = text if len(text) <= CONTEXT_CHARS else text[:CONTEXT_CHARS] + "…（已截断）"
        blocks.append(f"【资料{i}】\n{body}")
    return "\n\n".join(blocks)


def _split_sentences(text: str) -> list[str]:
    """按中文句读切句。用于 context_recall 的**参考答案**那一侧。

    ⚠ 这一侧刻意不走大模型，与 RAGAS 的做法一致（它用 NLTK 的 SentenceTokenizer）：
    参考答案是**人工写的**、句子边界清楚，正则切就够稳；而 faithfulness 那一侧是
    模型生成的自由文本，才需要 LLM 拆。本仓库没有 nltk，也不值得为切句引一个包。
    """
    parts = re.split(r"[。！？；\n]+", text or "")
    return [p.strip() for p in parts if len(p.strip()) >= 4]


# ⚠ 判定「这条陈述太短、单独拿出来没有可核查的内容」的字数门槛（去掉标点后计）。
# 标定依据：光杆判词「不可以」「可以」「不行」是 2~3 字；一旦带上谓项
# （「应当公开招标」6 字）就有可核查的内容了。所以 6 是「判词 / 真陈述」的分界。
_MIN_STMT_CHARS = 6

_PUNCT_RE = re.compile(r"[\s。！？；，、：:.,;!?\"'“”‘’（）()\[\]【】—\-…]+")


def _merge_short_statements(statements: list[str]) -> list[str]:
    """把「太短、单独一条没有可核查内容」的片段并入相邻陈述。

    ⚠⚠ 这不是为了好看，是**修一个真实的测量缺陷**：本项目 `SYSTEM_PROMPT` 第 5 条要求
    「结论先行」，模型很自然地以「不可以。」这类判词起头。LLM 把它拆成独立一条时，
    那条**既没有主语也没有谓项**，裁判在资料里找不到任何东西能「支持」它，只能判不支持。

    实测（2026-09-20 语料基线）：qa-0060 的答案几乎是参考答案原文，只因开头多了个
    「不可以。」，faithfulness 就从 1.0 掉到 0.5 —— 分数测的是**拆句方式**，不是忠实度。
    ⚠ qa-0047 是铁证：语料里**人工写的参考答案本人**就是「不可以。规模条件无论…」，
    模型照着它作答，却因为拆句被扣分。`_SPLIT_USER` 第 3 条排除了「客套话、过渡句」，
    **没排除光杆判词**。

    与 `_split_sentences` 的 `>= 4` 是同一条纪律，但**处置相反**：那一侧直接丢噪声片段，
    这一侧**不能丢**（会静默改变 faithfulness 的分母）—— 改成**并进相邻条目**，
    内容一个字不少，只是不再单独计分。

    短片段在**前**就并进后一条（「结论先行」正是这个形状），在**后**就并进前一条。

    ⚠⚠ **试过给 `_SPLIT_USER` 加一条「光杆判词不要单独成条」，实测要撤掉**（2026-09-20）：
    模型把那条过度泛化，开始**合并整句**而不是只并光杆判词 —— 74 题里 qa-0004 稳定地从
    0.800 掉到 0.500（三次重跑逐字相同，不是噪声），因为「可以采用单一来源方式采购。」后面
    那一整句 4 个 claim 被揉成一条，**一个未支持的分句污染了另外三个**。
    同一个答案下，旧拆法的 5 条原子陈述本可以只标出那一条错的。
    ⇒ **代码侧这个只认「<6 字」的兜底才是对的形状**：它精确命中光杆判词，永远不动长句。
    已验证：**单靠它**就能把 qa-0060（0.500→1.000）与 qa-0047（0.667→1.000）治好，
    不需要提示词配合。**别再往提示词里加这条。**
    """
    merged: list[str] = []
    pending = ""
    for raw in statements:
        text = raw.strip()
        if not text:
            continue
        if len(_PUNCT_RE.sub("", text)) < _MIN_STMT_CHARS:
            pending += text          # 先攒着，等一条真陈述来认领
            continue
        merged.append(pending + text)
        pending = ""
    if pending:                      # 全篇都是短片段，或短片段收尾
        if merged:
            merged[-1] += pending
        else:
            merged.append(pending)   # 只有这一条可留，原样返回（不凭空造内容）
    return merged


# ============================================================================
# 裁判调用
# ============================================================================


# ⚠⚠ 限制**同时在飞的**大模型请求数。与 `--concurrency` 是两回事，别合并：
# 后者是「同时跑几道题」，而一道题内部就有 5 次裁判是并发的 —— 默认 4 题 × 5 次 = 20 个
# 请求同时在飞，很容易撞上服务端的并发限制。撞上时是一批请求突然 429，**指标成片变成
# null**，看起来像「模型不配合」，实际是本地并发开太大。默认 8 是保守值。
_LLM_GATE: asyncio.Semaphore | None = None

# ⚠ 嵌入必须**串行**：torch 的 8 个线程在这台机器上已被跑满（CLAUDE.md 明写），
# 并发 `model.encode` 只会互相抢核，总吞吐反而下降。而一次嵌入只要几十毫秒。
_EMBED_LOCK: asyncio.Lock | None = None


def _gate():
    """`--llm-concurrency` 的闸门。`--selftest` 路径不设置它，退回空上下文。"""
    return _LLM_GATE if _LLM_GATE is not None else contextlib.nullcontext()


def _embed_lock():
    return _EMBED_LOCK if _EMBED_LOCK is not None else contextlib.nullcontext()


async def _judge_text(
    llm: Any,
    system: str,
    user: str,
    *,
    thinking: bool,
    max_tokens: int = 2048,
    temperature: float = 0.0,
) -> str | None:
    """一次裁判调用，返回正文；拿不到终态事件返回 None（降级，不抛）。

    ⚠ **刻意用 `stream_with_tools(tools=None)` 而不是 `llm.complete()`**：
    后者不接受 `max_tokens` 覆盖（写死 `settings.deepseek_max_tokens`），而本项目模型是
    推理型的，思维链会先吃掉预算 —— 那个坑 CLAUDE.md 在 glm-4.6v 上记过一次
    （「给少了表现为返回空，最难查的那种」）。`stream_with_tools` 同时给了
    `max_tokens` 与 `disable_thinking` 两个旋钮，且**不用改 `llm.py` 一行**。

    ⚠ `temperature` 默认 **0.0**：全部**裁判**调用都必须是确定性的（同一份输入两次跑出
    两个判定，指标就没法用来做 A/B 对比了）。只有 `make_eval_questions.py` 生成改写题时
    才传 0.9 —— 那里**要**多样性，正好相反。这个参数就是为它留的，别拿它去调裁判。
    """
    async with _gate():
        async for event in llm.stream_with_tools(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=None,
            disable_thinking=not thinking,  # 裁判只要一段 JSON，思考纯属浪费
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            if event.type == "message":
                return event.content or None
        return None


async def _judge_json(
    llm: Any,
    system: str,
    user: str,
    *,
    thinking: bool,
    max_tokens: int = 2048,
    temperature: float = 0.0,
) -> tuple[Any | None, str]:
    """**绝不抛异常**，返回 `(解析结果, 错误文本)` —— 两者总有且只有一个有效。

    ⚠ 这个函数是全部裁判调用的**唯一咽喉**，所以「降级而非报错」放在这一层最省事：
    每个指标只需要判 `data is None`，不必各写一套 try/except。这也是错误原因能被如实
    记下来的唯一位置 —— 调用方拿到的是「连接超时」还是「模型吐了段散文」，修法完全不同。
    """
    try:
        text = await _judge_text(
            llm, system, user, thinking=thinking, max_tokens=max_tokens, temperature=temperature
        )
    except Exception as exc:  # noqa: BLE001
        # 网络抖动 / 401 / 429 都会走到这里。⚠ 别吞掉异常类型：`_stream_round` 那条
        # 「被拒就重试」的内部逻辑不在这条路径上，这里的异常是调用方唯一的线索。
        return None, f"{type(exc).__name__}: {exc}"
    if text is None:
        return None, "模型没有返回内容（可能被 max_tokens 截断，或流被中断）"
    data = _extract_json(text)
    if data is None:
        return None, f"模型输出无法解析成 JSON：{text.strip()[:120]}"
    return data, ""


# ============================================================================
# 四个指标
# ============================================================================


async def metric_faithfulness(
    llm: Any, answer: str, context: str, *, thinking: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {"value": None, "coverage": None, "n": 0, "error": None}
    if not answer.strip():
        result["error"] = "答案为空，无从判断忠实度"
        return result

    data, err = await _judge_json(
        llm,
        _SPLIT_SYSTEM,
        _SPLIT_USER.format(answer=answer),
        thinking=thinking,
        max_tokens=1536,
    )
    raw_statements = (
        [s for s in data if isinstance(s, str) and s.strip()] if isinstance(data, list) else []
    )
    # ⚠ 光杆判词（「不可以。」）必须并进相邻陈述再判 —— 理由见 `_merge_short_statements`。
    statements = _merge_short_statements(raw_statements)
    if not statements:
        # ⚠ 拆不出陈述时**返回 None 而不是 0 或 1**：模型答的是「我无法回答」这类没有
        # 事实断言的内容时，两种极端都是误导 —— 记 1.0 是给拒答发满分，记 0.0 是冤枉它。
        result["error"] = f"没有拆出可判定的陈述（{err}）" if err else "答案里没有可判定的事实陈述"
        return result
    result["n"] = len(statements)

    flags: list[bool | None] = []
    errors: list[str] = []
    for start in range(0, len(statements), JUDGE_BATCH):
        chunk = statements[start : start + JUDGE_BATCH]
        verdicts, err = await _judge_json(
            llm,
            _VERDICT_SYSTEM,
            _FAITH_VERDICT_USER.format(
                context=context or "（本次没有检索到任何资料）",
                items=_numbered(chunk),
                n=len(chunk),
            ),
            thinking=thinking,
            max_tokens=1536,
        )
        if verdicts is None:
            errors.append(err)
        got = _index_verdicts(verdicts, ("supported", "support", "被支持", "支持"), len(chunk))
        flags.extend(got.get(i) for i in range(1, len(chunk) + 1))

    return _score_ratio_result(result, flags, errors)


async def metric_answer_relevancy(
    llm: Any, question: str, answer: str, *, thinking: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {"value": None, "coverage": None, "n": 0, "error": None}
    if not answer.strip():
        result["error"] = "答案为空，无从反推问题"
        return result

    data, err = await _judge_json(
        llm,
        _RELEV_SYSTEM,
        _RELEV_USER.format(n=RELEVANCY_N, answer=answer),
        thinking=thinking,
        max_tokens=1024,
    )
    generated = [s.strip() for s in data if isinstance(s, str) and s.strip()] if isinstance(data, list) else []
    if not generated:
        result["error"] = f"反推问题失败（{err}）" if err else "反推不出问题"
        return result
    result["n"] = len(generated)

    from bidding_qa.embedding import encode_documents  # torch，import 本身就慢，放函数里

    try:
        # ⚠ 原问题与反推问题**一起编码、用同一个 `encode_documents`**：两者同形（都是问题），
        #   对称比较才成立。若一边用 `encode_query`（带 BGE 检索指令前缀）一边用
        #   `encode_documents`（不带），余弦会被前缀系统性拉偏 —— 而分数看起来完全正常。
        async with _embed_lock():
            matrix = await asyncio.to_thread(encode_documents, [question, *generated])
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"嵌入失败: {type(exc).__name__}: {exc}"
        return result
    value = score_similarity(matrix)
    if value is None:
        result["error"] = "嵌入结果不足两行，算不出相似度"
        return result
    result["value"] = value
    result["coverage"] = 1.0
    return result


async def metric_context_recall(
    llm: Any, reference: str, context: str, *, thinking: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {"value": None, "coverage": None, "n": 0, "error": None}
    if not (reference or "").strip():
        result["error"] = "没有参考答案（这条题不计 context_recall）"
        return result

    sentences = _split_sentences(reference)
    if not sentences:
        result["error"] = "参考答案切不出可用句子"
        return result
    result["n"] = len(sentences)

    flags: list[bool | None] = []
    errors: list[str] = []
    for start in range(0, len(sentences), JUDGE_BATCH):
        chunk = sentences[start : start + JUDGE_BATCH]
        verdicts, err = await _judge_json(
            llm,
            _VERDICT_SYSTEM,
            _RECALL_VERDICT_USER.format(
                context=context or "（本次没有检索到任何资料）",
                items=_numbered(chunk),
                n=len(chunk),
            ),
            thinking=thinking,
            max_tokens=1536,
        )
        if verdicts is None:
            errors.append(err)
        got = _index_verdicts(verdicts, ("attributable", "attribution", "可归因", "归因"), len(chunk))
        flags.extend(got.get(i) for i in range(1, len(chunk) + 1))

    return _score_ratio_result(result, flags, errors)


async def metric_context_precision(
    llm: Any, question: str, contexts: list[str], *, thinking: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {"value": None, "coverage": None, "n": 0, "error": None}
    if not contexts:
        result["error"] = "没有检索到资料，精确率无定义"
        return result
    result["n"] = len(contexts)

    data, err = await _judge_json(
        llm,
        _PRECISION_SYSTEM,
        _PRECISION_USER.format(
            question=question, n=len(contexts), context=_contexts_text(contexts)
        ),
        thinking=thinking,
        max_tokens=1024,
    )
    got = _index_verdicts(data, ("relevant", "relevance", "相关"), len(contexts))
    flags = [got.get(i) for i in range(1, len(contexts) + 1)]
    if all(f is None for f in flags):
        result["error"] = f"相关性判定失败（{err}）" if err else "一条都没判出来"
        return result
    # 没判出来的按「不相关」计 —— 与 faithfulness 同一口径（保守），由覆盖率暴露出来
    binary = [f is True for f in flags]
    result["value"] = score_context_precision(binary)
    result["coverage"] = sum(1 for f in flags if f is not None) / len(flags)
    return result


# ============================================================================
# 检索入口：本地直连 / 走已经在跑的后端
# ============================================================================


class Retrieval:
    """检索入口的抽象。两个实现，产出**同一种东西**（真正的 `Source` 对象）。

    ⚠⚠ 为什么需要两个实现 —— 这是本机最容易踩的坑：本地 Qdrant 是**嵌入式文件模式**
    （`QdrantClient(path=...)`，`vector_store.py:61`），对 `data/qdrant_local/.lock` 加
    **排他锁**。后端 API 一旦在跑（它拿着那把锁），本脚本直连 `get_retriever()` 会直接抛
    `RuntimeError: Storage folder ... is already accessed by another instance`。
    CLAUDE.md 里那条「起 API 后无法再跑 ingest」是同一个锁。

    于是有两条路，**默认先试本地、被占用就自动走 HTTP**：

    | 实现 | 什么时候 | 保真度 |
    | --- | --- | --- |
    | `LocalRetrieval` | 后端没在跑 | 直接调 `retriever.search()` |
    | `HttpRetrieval` | 后端在跑 | `POST /api/search` —— 服务端跑的**就是** `retriever.search()` 那一行 |

    ⚠ 两个都**不是替身**：HTTP 那条打的是生产端点的生产代码路径，返回的也是完整的
    `Source`（`SearchResponse.sources: list[Source]`），反序列化回来逐字段相同。
    这比在本脚本里另写一份「复刻检索器」安全得多 —— 那种复刻一旦与生产漂移，
    所有数字都悄悄失真，而且看不出来。
    """

    label = "?"

    async def search(self, question: str, opts: argparse.Namespace) -> list[Any]:
        raise NotImplementedError


class LocalRetrieval(Retrieval):
    label = "local"

    def __init__(self, retriever: Any) -> None:
        self.retriever = retriever
        self.lock = asyncio.Lock()

    async def search(self, question: str, opts: argparse.Namespace) -> list[Any]:
        # ⚠ 串行化：本地 Qdrant 多线程同时打不是有文档保证的用法，而检索热了只要
        #   15~25ms，串行化的代价约等于零。并发收益全在裁判那几次调用上。
        async with self.lock:
            return await asyncio.to_thread(
                self.retriever.search, question, opts.top_k, opts.mode, opts.rerank
            )


class HttpRetrieval(Retrieval):
    label = "http"

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    async def search(self, question: str, opts: argparse.Namespace) -> list[Any]:
        import httpx

        from bidding_qa.schemas import Source

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base}/api/search",
                json={
                    "query": question,
                    "top_k": opts.top_k,
                    "mode": opts.mode,
                    "rerank": opts.rerank,
                },
            )
            response.raise_for_status()
            payload = response.json()
        # 服务端回的就是 `list[Source]` 的 JSON，校验回来 —— 下游一律按 Source 处理
        return [Source.model_validate(item) for item in payload.get("sources") or []]


def build_retrieval(opts: argparse.Namespace) -> tuple[Retrieval, str]:
    """返回 `(检索入口, 一行说明)`。`--retrieval live|http|auto`（默认 auto）。"""
    if opts.retrieval == "http":
        return HttpRetrieval(opts.api_base), f"HTTP {opts.api_base}/api/search（后端在跑）"

    if opts.retrieval in ("auto", "live"):
        from bidding_qa.retriever import get_retriever

        try:
            retriever = get_retriever()
            return LocalRetrieval(retriever), "本地直连 Qdrant"
        except Exception as exc:  # noqa: BLE001
            if opts.retrieval == "live":
                print(f"✗ 本地检索不可用：{type(exc).__name__}: {exc}")
                print("  后端多半正在跑（本地 Qdrant 是排他锁）。用 --retrieval http，或先停掉后端。")
                raise SystemExit(1)
            print(f"⚠ 本地检索不可用（{type(exc).__name__}），自动改走 HTTP。")
            print(f"  原因通常是后端正在跑并占着 data/qdrant_local 的排他锁 —— 这是正常的。")
            return HttpRetrieval(opts.api_base), f"HTTP {opts.api_base}/api/search（本地锁被占）"

    raise SystemExit(f"✗ 不认识的 --retrieval：{opts.retrieval}")


# ============================================================================
# 生成答案（走真链路，不用任何替身）
# ============================================================================


async def _generate(
    llm: Any, retrieval: Retrieval, question: str, opts: argparse.Namespace
) -> tuple[str, list[Any]]:
    """跑一次真实问答，返回 `(答案, 检索到的来源)`。"""
    sources = await retrieval.search(question, opts)

    if opts.pipeline == "chat":
        from bidding_qa.rag import stream_answer

        parts: list[str] = []
        async with _gate():
            async for kind, text in stream_answer(llm, question, sources, [], use_llm=True):
                if kind == "content":  # reasoning / markup_leak 一律丢弃
                    parts.append(text)
        return "".join(parts), sources

    # ---------- agent ----------
    # ⚠ 变量隔离见 _isolate_agent_tools()：图谱 / 采购库 / 联网三个工具会被摘掉，只留检索。
    #   不隔离的话上下文里混进非知识库内容，四项指标全部失去意义。
    from bidding_qa.agent import run_agent_stream

    if not isinstance(retrieval, LocalRetrieval):
        # ⚠ agent 链路需要**把 retriever 对象交给** run_agent_stream（它内部自己发起检索），
        #   走 HTTP 就没有这个对象。硬要支持得让 Agent 也改成打 HTTP —— 那要动生产代码，
        #   而这个脚本一条生产链路都不该改。所以直接说清楚，别让它静默用一个空检索器。
        raise RuntimeError("--pipeline agent 需要本地检索（agent 内部自己调 retriever），"
                           "请先停掉后端再用 --retrieval live")

    parts = []
    sources = []
    async with _gate():
        async for event_type, data in run_agent_stream(
            llm,
            retrieval.retriever,
            question=question,
            session_id=None,
            history=[],
            top_k=opts.top_k,
            mode=opts.mode,
            rerank=opts.rerank,
            use_llm=True,
            enable_web=False,
        ):
            if event_type == "sources":
                sources = data.get("sources") or sources
            elif event_type == "delta":
                parts.append(data.get("content", ""))
            elif event_type == "answer_reset":
                # 模型调工具前先吐的那句「我先查一下」会作废，别把它算进答案
                parts = []
    return "".join(parts), sources


# ============================================================================
# 单题评估
# ============================================================================


def _fill_sources(record: dict[str, Any], sources: list[Any], item: dict[str, Any]) -> None:
    """把检索结果写进记录，并算出「原文命中」与它的名次。

    ⚠ 这两个字段是**回归基线**上最直观的信号：语料 74 条的 `doc_id` 与索引里的
    `doc_id` 是同一个，所以「问题原文检索自己的答案」**必然**命中且必然排第 1。
    跑出来不是第 1（或干脆没命中）就是检索真的坏了 —— 比任何分数都直观。
    留出集没有这个性质（题目是改写的），`doc_id` 指的是**它改写自哪一条**，
    命中与否是**信息**而不是断言。
    """
    contexts = [getattr(s, "answer", "") or "" for s in sources]
    ids = [getattr(s, "doc_id", "") for s in sources]
    record["contexts"] = contexts
    record["context_doc_ids"] = ids
    record["empty_retrieval"] = not sources
    gold = item.get("doc_id") or ""
    if gold:
        record["gold_rank"] = ids.index(gold) + 1 if gold in ids else None
        record["hit_source_doc"] = gold in ids


async def evaluate_one(
    llm: Any,
    retrieval: Retrieval,
    item: dict[str, Any],
    opts: argparse.Namespace,
) -> dict[str, Any]:
    """跑一题，返回一条记录。**任何异常都转成字段里的 error，绝不向上抛**。

    这是本项目「降级而非报错」的老规矩（见 `retriever._apply_rerank` 的注释）：
    一题挂掉不该让跑了一半的一小时全废。
    """
    started = time.perf_counter()
    record: dict[str, Any] = {
        "id": item["id"],
        "question": item["question"],
        "reference": item.get("reference", ""),
        "doc_id": item.get("doc_id", ""),
        "origin": item.get("origin", ""),
        "answer": "",
        "contexts": [],
        "context_doc_ids": [],
        "empty_retrieval": False,
        "hit_source_doc": None,
        "gold_rank": None,
        "metrics": {name: {"value": None, "coverage": None, "n": 0, "error": None} for name in opts.metrics},
    }

    if opts.no_llm:
        # 纯检索层：零 API 调用。用来扫闸门/模式/top_k 这些**只作用在检索层**的旋钮 ——
        # 这是整个脚本最便宜也最该先跑的一层。
        try:
            sources = await retrieval.search(item["question"], opts)
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"检索失败: {type(exc).__name__}: {exc}"
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            return record
        _fill_sources(record, sources, item)
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return record

    try:
        answer, sources = await _generate(llm, retrieval, item["question"], opts)
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"生成失败: {type(exc).__name__}: {exc}"
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return record

    record["answer"] = answer
    record["answer_chars"] = len(answer)
    # ⚠ 「有资料却一个字都没答出来」是个**独立的故障信号**，值得单独计数：
    #   `deepseek_max_tokens=2048` 是**思维链与正文共享**的，推理型模型思维链一长，
    #   正文就整个没有 —— 而 `stream_answer` 不暴露 finish_reason，这种截断在链路上
    #   完全不可见。这里是唯一能把它数出来的地方。
    record["answer_empty_with_context"] = bool(sources) and not answer.strip()
    _fill_sources(record, sources, item)
    contexts = record["contexts"]
    context_text = _contexts_text(contexts)
    question = item["question"]

    async def run(name: str):
        try:
            if name == M_FAITH:
                return name, await metric_faithfulness(llm, answer, context_text, thinking=opts.judge_thinking)
            if name == M_RELEV:
                return name, await metric_answer_relevancy(llm, question, answer, thinking=opts.judge_thinking)
            if name == M_RECALL:
                return name, await metric_context_recall(
                    llm, record["reference"], context_text, thinking=opts.judge_thinking
                )
            if name == M_PRECISION:
                return name, await metric_context_precision(
                    llm, question, contexts, thinking=opts.judge_thinking
                )
        except Exception as exc:  # noqa: BLE001
            return name, {"value": None, "coverage": None, "n": 0, "error": f"{type(exc).__name__}: {exc}"}
        return name, {"value": None, "coverage": None, "n": 0, "error": "未知指标"}

    # 同一题的几项指标并发跑（它们互相独立），但每题整体仍受外层并发闸门限制
    for name, value in await asyncio.gather(*(run(name) for name in opts.metrics)):
        record["metrics"][name] = value

    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return record


# ============================================================================
# 汇总与报告
# ============================================================================


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"mean": None, "median": None, "min": None, "max": None, "n": 0}
    return {
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "n": len(values),
    }


def summarize(records: list[dict[str, Any]], opts: argparse.Namespace) -> dict[str, Any]:
    """汇总。

    ⚠⚠ **空检索的题整体排除出四项均值**（单独报数）。理由不是洁癖：闸门筛空时模型回
    一句「暂未找到依据」，那句拒答是**唯一一条陈述**、又恰好被空上下文「支持」——
    faithfulness 会算成 1.0。混进均值里等于给「完全没答上来」的题发满分。
    判据用**结构**（`len(sources) == 0`）而不是匹配话术，理由见 CLAUDE.md：
    「模型说了某个意思 ⇒ 服务端做某个动作」的判据必须匹配语义而不是话术。
    """
    scored = [r for r in records if not r.get("empty_retrieval")]
    empty = [r for r in records if r.get("empty_retrieval")]
    errored = [r for r in records if r.get("error")]

    metrics: dict[str, Any] = {}
    for name in opts.metrics:
        cell = [r["metrics"].get(name) or {} for r in scored]
        values = [c["value"] for c in cell if isinstance(c.get("value"), (int, float))]
        coverages = [c["coverage"] for c in cell if isinstance(c.get("coverage"), (int, float))]
        stat = _stats(values)
        stat["null_count"] = sum(1 for c in cell if c.get("value") is None)
        stat["mean_coverage"] = round(statistics.fmean(coverages), 4) if coverages else None
        metrics[name] = stat

    # ---------- 检索层（零 LLM 成本，与四项指标并列报出）----------
    gold_ranks = [r["gold_rank"] for r in records if r.get("gold_rank") is not None]
    graded = [r for r in records if r.get("doc_id")]
    retrieval = {
        "empty_retrieval_rate": round(len(empty) / len(records), 4) if records else None,
        "n_sources_mean": round(statistics.fmean([len(r.get("context_doc_ids") or []) for r in records]), 2)
        if records else None,
        "gold_hit@k": round(sum(1 for r in graded if r.get("hit_source_doc")) / len(graded), 4)
        if graded else None,
        "gold_mrr": round(statistics.fmean([1 / r["gold_rank"] for r in graded if r.get("gold_rank")]),
                          4) if any(r.get("gold_rank") for r in graded) else None,
        "gold_rank_hist": {str(k): sum(1 for r in gold_ranks if r == k) for k in sorted(set(gold_ranks))},
        "gold_missing": sum(1 for r in graded if not r.get("hit_source_doc")),
    }

    hits = [r for r in records if r.get("hit_source_doc") is not None]
    return {
        "counts": {
            "total": len(records),
            "scored": len(scored),
            "empty_retrieval": len(empty),
            "generation_error": len(errored),
            "answer_empty_with_context": sum(1 for r in records if r.get("answer_empty_with_context")),
            "hit_source_doc": sum(1 for r in hits if r["hit_source_doc"]),
            "hit_source_doc_total": len(hits),
        },
        "retrieval": retrieval,
        "metrics": metrics,
    }


def _score_of(record: dict[str, Any], name: str) -> float | None:
    value = (record.get("metrics") or {}).get(name) or {}
    got = value.get("value")
    return got if isinstance(got, (int, float)) else None


def _worst(records: list[dict[str, Any]], metrics: tuple[str, ...], count: int) -> list[dict[str, Any]]:
    """挑出最差的几题 —— 「数据驱动优化」的落点，光有均值没法定位到题。"""
    rows = []
    for record in records:
        if record.get("empty_retrieval"):
            continue
        values = [v for v in (_score_of(record, name) for name in metrics) if v is not None]
        if not values:
            continue
        rows.append((statistics.fmean(values), record))
    rows.sort(key=lambda pair: pair[0])
    return [
        {
            "id": record["id"],
            "question": record["question"],
            "mean": round(score, 4),
            "scores": {name: _score_of(record, name) for name in metrics},
            "doc_ids": record.get("context_doc_ids") or [],
            "empty_retrieval": bool(record.get("empty_retrieval")),
        }
        for score, record in rows[:count]
    ]


def _fmt(value: float | None, width: int = 8) -> str:
    return f"{'—':>{width}}" if value is None else f"{value:>{width}.3f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def print_report(summary: dict[str, Any], worst: list[dict[str, Any]], opts: argparse.Namespace) -> None:
    counts = summary["counts"]
    banner("评估结果")
    print(
        f"链路={opts.pipeline}  模式={opts.mode}  top_k={opts.top_k}  "
        f"重排={'开' if opts.rerank else '关'}  并发={opts.concurrency}题/{opts.llm_concurrency}请求"
    )
    print(
        f"闸门: 向量≥{settings.min_vector_score}  BM25≥{settings.min_bm25_score}  "
        f"重排≥{settings.min_rerank_score}"
    )
    print(f"题目: {counts['total']} 条")
    print()

    # ⚠ 表头这些列名全是 ASCII，`:<{width}` 的对齐才成立。中文列名不吃这套
    #   （终端里是双宽，len() 只数 1），要对齐得另算显示宽度 —— 不值得，换个写法即可。
    if opts.metrics:
        width = max(len(name) for name in opts.metrics) + 2
        print(f"{'指标':<{width}}{'均值':>8}{'中位':>8}{'最低':>8}{'最高':>8}{'有效':>6}{'覆盖率':>8}")
        for name in opts.metrics:
            stat = summary["metrics"][name]
            tail = f"   ({stat['null_count']} 题未算出)" if stat["null_count"] else ""
            print(
                f"{name:<{width}}{_fmt(stat['mean'])}{_fmt(stat['median'])}"
                f"{_fmt(stat['min'])}{_fmt(stat['max'])}{stat['n']:>6}{_fmt(stat['mean_coverage'])}{tail}"
            )

    retrieval = summary.get("retrieval") or {}
    print()
    print("检索层（零 API 成本）：")
    print(f"  空检索（闸门筛空）: {counts['empty_retrieval']}/{counts['total']} "
          f"= {_pct(retrieval.get('empty_retrieval_rate'))}  ← 已排除出上面的均值")
    print(f"  平均命中条数: {retrieval.get('n_sources_mean')}")
    if counts["hit_source_doc_total"]:
        print(f"  原文命中: {counts['hit_source_doc']}/{counts['hit_source_doc_total']} "
              f"= {_pct(retrieval.get('gold_hit@k'))}   MRR={retrieval.get('gold_mrr')}")
        if retrieval.get("gold_rank_hist"):
            print(f"  原文名次分布: {retrieval['gold_rank_hist']}")
    if counts["generation_error"]:
        print(f"  生成失败: {counts['generation_error']} 题")
    if counts["answer_empty_with_context"]:
        print(f"  ⚠ 有资料却答出空正文: {counts['answer_empty_with_context']} 题"
              f"（多为 2048 max_tokens 被思维链吃光，见脚本注释）")

    if worst:
        print()
        banner(f"最差 {len(worst)} 题（按四项均分升序 —— 优化就从这几道下手）")
        for row in worst:
            print(f"  {row['mean']:.3f}  [{row['id']}]  {row['question'][:60]}")
            cells = " · ".join(
                f"{name} {row['scores'][name]:.2f}" if isinstance(row["scores"][name], (int, float))
                else f"{name} —"
                for name in opts.metrics
            )
            print(f"         {cells}")
            if row["doc_ids"]:
                print(f"         命中: {', '.join(row['doc_ids'])}")


# ============================================================================
# 数据集
# ============================================================================


def load_questions(opts: argparse.Namespace) -> tuple[list[dict[str, Any]], str]:
    """返回 `(题目列表, 来源标签)`。

    不传 `--questions` 就用语料自己的 74 条 —— 那是**回归基线**（题目与参考答案都是
    语料原文，检索必然命中原文，`context_recall` 会接近 1.0）。它测的是「有没有把已经
    对的东西弄坏」，测不出检索的真实水平；要测那个得用留出集。
    """
    path = opts.questions
    if path is None:
        from bidding_qa.dataset import load_qa_docs

        docs = load_qa_docs()
        items = [
            {
                "id": doc["doc_id"],
                "question": doc["question"],
                "reference": doc["answer"],
                "doc_id": doc["doc_id"],
                "origin": "corpus",
            }
            for doc in docs
        ]
        return items, f"语料 {len(items)} 条（回归基线）"

    if path.suffix.lower() == ".txt":
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
        items = [
            {"id": f"txt-{i:03d}", "question": line, "reference": "", "doc_id": "", "origin": "txt"}
            for i, line in enumerate([line for line in lines if line], start=1)
        ]
        return items, f"{path.name} {len(items)} 条（无参考答案）"

    items = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except ValueError as exc:
            print(f"  ⚠ 第 {lineno} 行不是合法 JSON，已跳过：{exc}")
            continue
        if not isinstance(raw, dict) or not str(raw.get("question", "")).strip():
            print(f"  ⚠ 第 {lineno} 行没有 question 字段，已跳过")
            continue
        items.append(
            {
                "id": str(raw.get("id") or f"q-{lineno:03d}"),
                "question": str(raw["question"]).strip(),
                "reference": str(raw.get("reference") or ""),
                "doc_id": str(raw.get("doc_id") or ""),
                "origin": str(raw.get("origin") or "file"),
            }
        )
    seen: set[str] = set()
    unique = []
    for item in items:
        if item["id"] in seen:
            print(f"  ⚠ id 重复，已跳过：{item['id']}")
            continue
        seen.add(item["id"])
        unique.append(item)
    return unique, f"{path.name} {len(unique)} 条"


# ============================================================================
# --selftest：不联网、不花钱，验的是一直在写的那部分逻辑
# ============================================================================


def selftest() -> int:
    banner("① 宽容 JSON 解析（模型从不老老实实只吐 JSON）")
    check("裸数组", _extract_json('["a","b"]') == ["a", "b"])
    check("裸对象", _extract_json('{"i": 1}') == {"i": 1})
    check("带 ```json 围栏", _extract_json('```json\n["a"]\n```') == ["a"])
    check("前后有废话", _extract_json('好的，结果如下：\n["a","b"]\n以上。') == ["a", "b"])
    check("对象前后有废话", _extract_json('分析完成：{"i":2,"relevant":true} 完毕')["i"] == 2)
    check("中间换行", _extract_json('[\n  "a",\n  "b"\n]') == ["a", "b"])
    check("空串 → None", _extract_json("") is None)
    check("纯文本无 JSON → None", _extract_json("我无法完成这个任务。") is None)
    check("坏 JSON → None（不能崩）", _extract_json('["a", "b"') is None)

    banner("② 宽容布尔/编号（模型会把 true 写成「是」「支持」）")
    check("True 本身", _as_bool(True) is True)
    check('字符串 "true"', _as_bool("true") is True)
    check('字符串 "是"', _as_bool("是") is True)
    check('字符串 "支持"', _as_bool("支持") is True)
    check('字符串 "否"', _as_bool("否") is False)
    check('字符串 "不支持"', _as_bool("不支持") is False)
    check("数字 1 / 0", _as_bool(1) is True and _as_bool(0) is False)
    check("认不出来 → None（不能当 False）", _as_bool("也许吧") is None)
    check('编号 "第2条" → 2', _as_int("第2条") == 2)

    banner("③ 判定结果归并：越界 / 重复 / 缺字段都不能带进来")
    data = [
        {"i": 1, "relevant": True},
        {"i": 2, "relevant": "否"},
        {"i": 99, "relevant": True},      # 越界
        {"i": 1, "relevant": False},       # 重复，第一条胜出
        {"relevant": True},                # 没有编号
        "垃圾",                             # 不是对象
    ]
    got = _index_verdicts(data, ("relevant",), 2)
    check("只收下 1..N 范围内的", got == {1: True, 2: False}, f"实际 {got}")

    banner("④ faithfulness 打分与覆盖率")
    value, coverage = score_ratio([True, True, False])
    check("3 条里 2 条被支持 → 2/3", abs(value - 2 / 3) < 1e-9, f"实际 {value}")
    check("全覆盖时覆盖率 = 1", coverage == 1.0)
    value, coverage = score_ratio([True, True, None])
    check("有一条没判出来 → 仍按不支持计：2/3", abs(value - 2 / 3) < 1e-9, f"实际 {value}")
    check("覆盖率如实反映 2/3（解析坏了要看得出来）", abs(coverage - 2 / 3) < 1e-9, f"实际 {coverage}")
    check("一条陈述都没有 → None（不是 0）", score_ratio([]) == (None, 0.0))

    banner("⑤ context_precision 的公式（手算对账）")
    # [相关, 不相关, 相关]：相关共 2 条。
    #   p@1 = 1/1（第 1 条命中）、p@2 不命中不计、p@3 = 2/3（前 3 条命中 2 条）
    #   (1×1 + 0 + 1×2/3) / 2 = 0.8333…
    got = score_context_precision([True, False, True])
    check("相关/不相关/相关 → 0.8333", abs(got - 5 / 6) < 1e-9, f"实际 {got}")
    check("全相关 → 1.0", score_context_precision([True, True, True]) == 1.0)
    check("只有第 2 条相关 → 0.5", score_context_precision([False, True]) == 0.5)
    check(
        "全不相关 → 0.0（**不是** None：那是个事实，必须进均值）",
        score_context_precision([False, False]) == 0.0,
    )
    check("没有资料 → None（算不出来）", score_context_precision([]) is None)
    # 尾部命中越多分越低 —— 这正是 precision@k 该有的性质（好结果排前面才得分）
    check(
        "相关的排后面 → 分数更低",
        score_context_precision([True, True, False, False]) > score_context_precision([False, False, True, True]),
    )

    banner("⑥ answer_relevancy 的余弦（L2 归一化后 = 点积）")
    try:
        import numpy as np

        # 用**手工归一化的合成矩阵**验数学，不加载真模型 —— 这一节是纯计算，
        # 不该为了它把 torch 拉起来（那要十几秒，还违背「selftest 立刻返回」）。
        base = np.array([1.0, 0.0, 0.0], dtype="float32")
        same = np.array([[1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype="float32")
        check("完全相同 → 1.0", abs(score_similarity(same) - 1.0) < 1e-6, f"实际 {score_similarity(same)}")
        mixed = np.array([[1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype="float32")
        check("一条正交 → 平均 0.5", abs(score_similarity(mixed) - 0.5) < 1e-6, f"实际 {score_similarity(mixed)}")
        check("只有一行 → None（没有可比的东西）", score_similarity(base.reshape(1, -1)) is None)
    except Exception as exc:  # noqa: BLE001
        check("numpy 可用", False, f"{type(exc).__name__}: {exc}")

    # 真模型那一段是可选的：模型没缓存时不加载（避免 selftest 卡十几秒），
    # 缓存了才顺手验一把「同一句话与自己的余弦确实是 1」这个端到端假设。
    cache = Path(settings.model_cache_dir)
    if not cache.is_absolute():
        cache = PROJECT_ROOT / cache
    cached = cache.exists() and any(cache.glob("models--*bge-small-zh*"))
    if cached:
        try:
            from bidding_qa.embedding import encode_documents

            question = "单一来源采购的适用情形有哪些？"
            value = score_similarity(encode_documents([question, question, question]))
            check("真模型：同一句话与自己的余弦 ≈ 1.0", value is not None and abs(value - 1.0) < 1e-4,
                  f"实际 {value}")
        except Exception as exc:  # noqa: BLE001
            check("真模型可加载", False, f"{type(exc).__name__}: {exc}")
    else:
        print("  [跳过] 嵌入模型未缓存，真机嵌入自检跳过（纯数学部分已验）")

    banner("⑦ 空检索单独成桶，绝不混进均值")
    opts = argparse.Namespace(metrics=ALL_METRICS)
    good = {
        "id": "a", "question": "q", "empty_retrieval": False, "error": None,
        "hit_source_doc": True, "context_doc_ids": ["qa-1"],
        "metrics": {name: {"value": 0.5, "coverage": 1.0, "n": 1, "error": None} for name in ALL_METRICS},
    }
    blank = {
        "id": "b", "question": "q", "empty_retrieval": True, "error": None,
        "hit_source_doc": None, "context_doc_ids": [],
        # 拒答那条陈述恰好被空上下文「支持」—— 这正是它不能进均值的原因
        "metrics": {name: {"value": 1.0, "coverage": 1.0, "n": 1, "error": None} for name in ALL_METRICS},
    }
    summary = summarize([good, blank], opts)
    check("空检索题不计入均值分母", summary["counts"]["scored"] == 1)
    check("空检索题单独报数", summary["counts"]["empty_retrieval"] == 1)
    check(
        "均值仍是 0.5（没有被那条 1.0 的拒答拉高）",
        all(summary["metrics"][name]["mean"] == 0.5 for name in ALL_METRICS),
        f"实际 {summary['metrics'][M_FAITH]['mean']}",
    )

    banner("⑧ 汇总统计：null 不参与计算")
    rows = [dict(good, id=f"x{i}", metrics={n: {"value": v, "coverage": 1.0, "n": 1, "error": None} for n in ALL_METRICS})
            for i, v in enumerate([0.2, 0.4, 0.6, None])]
    stat = summarize(rows, opts)["metrics"][M_FAITH]
    check("4 条里 3 条有效 → n=3", stat["n"] == 3, f"实际 {stat['n']}")
    check("均值只算有效的三条 → 0.4", stat["mean"] == 0.4, f"实际 {stat['mean']}")
    check("中位数同上", stat["median"] == 0.4, f"实际 {stat['median']}")
    check("未算出的题数如实报出", stat["null_count"] == 1)

    banner("⑨ 切句（context_recall 的参考答案那一侧）")
    check(
        "按句号/分号/换行切",
        len(_split_sentences("履约保证金不得超过中标合同金额的10%。采购人不得收取其他费用；"
                             "退还期限为30日\n逾期应支付利息。")) == 4,
        f"实际 {_split_sentences('履约保证金不得超过中标合同金额的10%。采购人不得收取其他费用；退回期限为30日\n逾期应支付利息。')}",
    )
    check("太短的片段丢掉（「如下：」这种噪声）", _split_sentences("如下：") == [])
    check("空串 → 空列表", _split_sentences("") == [])
    check("不切标点内部的顿号/逗号", len(_split_sentences("甲、乙、丙三方，应当共同签署。")) == 1)

    banner("⑩ 光杆判词必须并进相邻陈述（faithfulness 那一侧）")
    # ⚠ 这条对应一个**实测到的真缺陷**：qa-0060 的答案几乎是参考答案原文，只因开头多了个
    # 「不可以。」，faithfulness 从 1.0 掉到 0.5。断言的形状是「并进去、且一个字不丢」。
    check(
        "「不可以。」并进后一条",
        _merge_short_statements(["不可以。", "采购人不得收取其他费用。"])
        == ["不可以。采购人不得收取其他费用。"],
        f"实际 {_merge_short_statements(['不可以。', '采购人不得收取其他费用。'])}",
    )
    check(
        "「可以。」「不行。」同样并",
        _merge_short_statements(["可以。", "不行。", "应当公开招标。"]) == ["可以。不行。应当公开招标。"],
    )
    check(
        "收尾的短片段并进**前**一条",
        _merge_short_statements(["采购人不得收取其他费用。", "就是这样。"])
        == ["采购人不得收取其他费用。就是这样。"],
    )
    check(
        "**真**陈述一条都不动（不许误并）",
        _merge_short_statements(["履约保证金不得超过中标合同金额的10%。", "退还期限为30日。"])
        == ["履约保证金不得超过中标合同金额的10%。", "退还期限为30日。"],
    )
    check(
        "全篇都是判词 → 合成一条，不凭空造内容",
        _merge_short_statements(["可以。", "不行。"]) == ["可以。不行。"],
    )
    check("空列表 → 空列表", _merge_short_statements([]) == [])
    check(
        "纯空白项被丢掉",
        _merge_short_statements(["  ", "应当公开招标。"]) == ["应当公开招标。"],
    )
    check(
        "内容一个字都不丢（并前后拼接相等）",
        "".join(_merge_short_statements(["不可以。", "甲应当公开招标。", "不行。"]))
        == "不可以。甲应当公开招标。不行。",
    )

    banner("⑪ 检查点往返（中文 / 换行 / 引号 / 反斜杠）")
    import tempfile

    tricky = {"id": "中-1", "text": '含"引号"、换行\n、反斜杠\\ 与 emoji 🚀', "n": 1.5}
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "ck.jsonl"
        with target.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(tricky, ensure_ascii=False) + "\n")
        back = json.loads(target.read_text(encoding="utf-8").strip())
        check("逐字段相等", back == tricky, f"实际 {back}")

    banner("⑫ 裁判失败时必须降级，且**不能**给一个像样的分数")
    import asyncio as _asyncio

    class _DeadLLM:
        async def stream_with_tools(self, *args, **kwargs):
            raise RuntimeError("模拟 Key 失效")
            yield  # pragma: no cover

    class _GarbageLLM:
        """只吐散文、不吐 JSON —— 模型不守格式那种情况。"""

        async def stream_with_tools(self, *args, **kwargs):
            class _Ev:
                type = "message"
                content = "好的，我分析了一下，这条陈述应该是有资料支持的。"

            yield _Ev()

    async def _probe(llm):
        return await metric_faithfulness(llm, "答案", "资料", thinking=False)

    for label, llm in (("裁判抛异常", _DeadLLM()), ("裁判吐散文不吐 JSON", _GarbageLLM())):
        try:
            out = _asyncio.run(_probe(llm))
            ok = out["value"] is None and bool(out["error"])
            check(f"{label} → value 为 None 且带 error 原因", ok, f"实际 {out}")
        except Exception as exc:  # noqa: BLE001
            check(f"{label} → value 为 None 且带 error 原因", False, f"{type(exc).__name__}: {exc}")

    banner("⑬ 检查点文件名必须能区分不同的运行（同名 = 静默覆盖）")

    def _ns(**over):
        base = {
            "pipeline": "chat",
            "mode": "hybrid",
            "top_k": 5,
            "rerank": False,
            "no_llm": False,
            "metrics": list(ALL_METRICS),
            "questions": None,
            "min_vector_score": None,
            "min_bm25_score": None,
            "min_rerank_score": None,
            "rrf_k": None,
            "vector_weight": None,
            "bm25_weight": None,
        }
        return argparse.Namespace(**{**base, **over})

    import itertools

    # ① 短名两两不同（`context_recall` / `context_precision` 的前三字母都是 con）
    check(
        "指标短名两两不同",
        len(set(_METRIC_SHORT.values())) == len(ALL_METRICS),
        f"实际 {_METRIC_SHORT}",
    )
    # ② 逐对比较**所有**单指标运行的 run-id
    singles = {
        name: _run_id(_ns(metrics=[name]), "语料") for name in ALL_METRICS
    }
    pairs = list(itertools.combinations(singles, 2))
    clashes = [(a, b) for a, b in pairs if singles[a] == singles[b]]
    check(f"{len(pairs)} 对单指标运行的名两两不同", not clashes, f"撞名 {clashes}")
    # ③ 顺序无关：同一组指标换个写法必须是同一次运行（否则检查点凭空分家）
    check(
        "指标顺序不影响 run-id",
        _run_id(_ns(metrics=[M_RECALL, M_FAITH]), "语料")
        == _run_id(_ns(metrics=[M_FAITH, M_RECALL]), "语料"),
        "顺序不同却算出两个名字",
    )
    # ④ 三项阈值都要进名字 —— 否则扫阈值的几次会共用一份检查点、数字全一样
    base_id = _run_id(_ns(), "语料")
    for flag in ("min_vector_score", "min_bm25_score", "min_rerank_score"):
        check(
            f"{flag} 进 run-id",
            _run_id(_ns(**{flag: 0.9}), "语料") != base_id,
            "改了阈值名字却没变",
        )
    # ④b 三个融合参数同理 —— 扫 k / 扫权重是本轮评估的主要用途，共用检查点就等于没扫
    for flag in ("rrf_k", "vector_weight", "bm25_weight"):
        check(
            f"{flag} 进 run-id",
            _run_id(_ns(**{flag: 0.9}), "语料") != base_id,
            "改了融合参数名字却没变",
        )
    # ⑤ `--no-llm` 与四项指标跑的是两种运行，绝不能共用检查点
    check(
        "--no-llm 与全指标不共用检查点",
        _run_id(_ns(no_llm=True), "语料") != base_id,
        "两种运行算出同一个名字",
    )

    banner("⑭ 覆盖率守卫：裁判坏掉时不能给出一个像样的低分")
    # ⚠ None 是「没判出来」，不是「判成不支持」。4 条里 3 条判了、1 条没判 → 覆盖率 3/4，
    #   而 0/4 = 0.0 是**合法**分数（三条都说没支持），不该被守卫拦下。
    result = {"value": None, "coverage": None, "n": 4, "error": None}
    out = _score_ratio_result(result, [False, None, None, None], ["429 Too Many Requests"])
    check("4 条只判出 1 条 → 判为「算不出来」而不是 0.0", out["value"] is None, f"实际 {out['value']}")
    check("原因写进了 error", "429" in (out["error"] or ""), f"实际 {out['error']}")
    result = {"value": None, "coverage": None, "n": 4, "error": None}
    out = _score_ratio_result(result, [False, False, False, None], [])
    check("判出 3/4 且全不支持 → 0.0 照常采信（覆盖率够）", out["value"] == 0.0, f"实际 {out['value']}")
    result = {"value": None, "coverage": None, "n": 3, "error": None}
    out = _score_ratio_result(result, [True, True, None], [])
    check("判出 2/3，没判的那条按不支持 → 2/3", out["value"] == 2 / 3, f"实际 {out['value']}")
    check("同时记下未判出的条数", "1 条" in (out["error"] or ""), f"实际 {out['error']}")

    banner(f"结果：{_pass} 通过 / {_fail} 失败")
    return 0 if _fail == 0 else 1


# ============================================================================
# main
# ============================================================================


def _isolate_agent_tools() -> None:
    """把 Agent 的图谱 / 采购库 / 联网三个工具摘掉，只留检索。

    ⚠ 不摘的话四项指标全部失去意义：上下文里会混进图谱结果、数据库明细、网页正文，
    而 `context_recall` / `context_precision` 问的是「**知识库检索**回来的资料」。
    这是为了让指标可解释而付出的代价 —— 测的是「只会检索的 Agent」，报告里必须标出来。

    ⚠ `TOOL_SCHEMAS` 是 **import 期**按 `graph_store.ENABLED` / `pg_store.ENABLED` 算出来的
    常量，所以改那两个 `ENABLED` 对工具表**没用**，必须直接替换模块属性。
    好消息是 `tools_for()` 是**调用期**读全局，替换立刻生效（已实测）。

    ⚠⚠ **必须写 `rag_tools.TOOL_SCHEMAS`，不能写 `agent.TOOL_SCHEMAS`**（2026-09-20）：
    工具搬进 `bidding_qa/tools/` 之后，`agent` 那份是**只读 re-export** —— 对模块属性赋值
    只是给 `agent` 的命名空间加了个新名字，`tools_for()` 读的仍是 `rag_tools` 的全局。
    写成 `agent.` 会**静默失效**：图谱/数据库/联网三个工具照旧挂在提示词与工具表里，
    而报告看起来一切正常，四项指标的上下文里混进了图谱行与网页正文。
    """
    from bidding_qa import graph_store, pg_store
    from bidding_qa.tools import rag_tools

    rag_tools.TOOL_SCHEMAS = [
        schema
        for schema in rag_tools.TOOL_SCHEMAS
        if schema["function"]["name"] == rag_tools.TOOL_SEARCH
    ]
    rag_tools.TOOL_NAMES = tuple(s["function"]["name"] for s in rag_tools.TOOL_SCHEMAS)
    # 提示词片段是按这两个 ENABLED 在**调用期**拼的，改它们才管用
    graph_store.ENABLED = False
    pg_store.ENABLED = False


def _run_id(opts: argparse.Namespace, source_label: str) -> str:
    """检查点文件名：**只由配置决定**，不含时间戳。

    这样「同样的配置再跑一次」会接着上次的检查点续跑（`--resume` 默认开），而
    `--no-resume` 从头来过。⚠ 四项指标必须一起进签名：只跑一半指标的那次若被复用，
    另一半会**静默**变成 null，看起来像「模型不配合」。
    """
    parts = [
        opts.pipeline,
        opts.mode,
        f"k{opts.top_k}",
        "rerank" if opts.rerank else "norerank",
        # ⚠ `--no-llm` 与「跑了四项指标」是两种**不能共用检查点**的运行：前者没有任何
        #   指标，若共用，后者续跑时会跳过全部题目、拿到一排空的指标表。
        # ⚠ 按 ALL_METRICS 的**规范顺序**取短名，不按用户输入顺序 ——
        #   `--metrics recall,faith` 与 `--metrics faith,recall` 是同一次运行，
        #   顺序不同却算出两个 run-id 的话，检查点会凭空分家。
        "nollm"
        if opts.no_llm
        else "m" + "-".join(_METRIC_SHORT[name] for name in ALL_METRICS if name in opts.metrics),
        # ⚠ 用文件名而不是那句人读的 source_label（后者含中文，会变成一串难看的文件名）
        (re.sub(r"[^\w]+", "", opts.questions.stem) if opts.questions else "corpus")[:16] or "q",
    ]
    for flag, value in (
        ("mv", opts.min_vector_score),
        ("mb", opts.min_bm25_score),
        ("mr", opts.min_rerank_score),
        # ⚠ 融合参数同属「扫一遍」的对象，所以也**必须进名字**。漏了的症状与阈值那三个
        #   逐字相同：几次运行共用一份检查点，`--resume` 把题目全部跳过，表现是
        #   「换了参数重跑，数字一模一样」—— 看起来像旋钮无效，其实是压根没重跑。
        ("rk", opts.rrf_k),
        ("vw", opts.vector_weight),
        ("bw", opts.bm25_weight),
    ):
        if value is not None:
            parts.append(f"{flag}{value:g}")
    return "-".join(parts)


async def run(opts: argparse.Namespace) -> int:
    from bidding_qa.llm import get_llm

    items, source_label = load_questions(opts)
    if not items:
        print("✗ 一道题都没有，退出。")
        return 1
    if opts.limit:
        items = items[: opts.limit]

    if opts.pipeline == "agent":
        _isolate_agent_tools()

    retrieval, backend_note = build_retrieval(opts)

    # ⚠⚠ 闸门阈值与融合参数**都不是**请求参数，它们活在服务端进程的 `settings` 里。走 HTTP 时
    #   改本进程的 `settings.min_vector_score` 对服务端**毫无影响** —— 于是「扫一遍阈值」
    #   会产出一堆数字完全相同的报告，看起来像「这个旋钮没用」，实际是**它压根没生效**。
    #   这正是本仓库最警惕的那类「看起来完全合理的错误答案」，所以宁可当场报错。
    overridden = [
        flag
        for flag, value in (
            ("--min-vector-score", opts.min_vector_score),
            ("--min-bm25-score", opts.min_bm25_score),
            ("--min-rerank-score", opts.min_rerank_score),
            ("--rrf-k", opts.rrf_k),
            ("--vector-weight", opts.vector_weight),
            ("--bm25-weight", opts.bm25_weight),
        )
        if value is not None
    ]
    if overridden and isinstance(retrieval, HttpRetrieval):
        print(f"✗ {', '.join(overridden)} 在 --retrieval http 下**不生效**。")
        print("  闸门阈值与融合参数读的都是服务端进程的 settings，本进程改它没用 —— 跑出来的")
        print("  数字会与不加参数时一模一样，看起来像「这个旋钮没用」，其实是它压根没生效。")
        print("  要扫这些旋钮：先停掉后端，然后 --retrieval live。")
        return 1

    banner("RAG 质量评估")
    print(f"题目: {source_label}")
    print(f"检索: {backend_note}")
    print(f"参数: 模式={opts.mode} top_k={opts.top_k} 重排={'开' if opts.rerank else '关'}")
    print(
        f"闸门: 向量≥{settings.min_vector_score}  BM25≥{settings.min_bm25_score}  "
        f"重排≥{settings.min_rerank_score}"
        + ("   ⚠ 重排关着，min_rerank_score 不生效" if not opts.rerank else "")
    )
    # ⚠ 打印的是 settings 里的**当前值**（已被上面的 setattr 改过），不是 option 的默认值 ——
    #   否则不加参数时看不出来它到底是 60/1.0/1.0 还是别的。非 hybrid 模式下融合根本不跑，
    #   那三个数就是噪声，索性不印。
    if opts.mode == "hybrid":
        print(
            f"融合: RRF k={settings.rrf_k}  向量权重={settings.vector_weight}  "
            f"BM25 权重={settings.bm25_weight}"
        )

    llm = None
    if opts.no_llm:
        print("模式: 只跑检索层（--no-llm）—— 零 API 调用，四项指标全部跳过")
    else:
        print(f"链路: {opts.pipeline}"
              + ("（已摘掉图谱/采购库/联网，只留检索）" if opts.pipeline == "agent" else ""))
        llm = get_llm()
        if not llm.ready:
            # ⚠ 早退而不是硬跑：`stream_answer` 在没配 Key 时会**静默退化成 fallback_answer()**
            #   （把知识库原文原样端出来），那不是模型答案 —— 拿它算出来的四项指标全是废的。
            print("✗ 未配置 DEEPSEEK_API_KEY，问答链路会退化成「直接返回原文」，指标无意义。")
            return 1
        print(f"模型: {llm.model}   裁判思考: {'开' if opts.judge_thinking else '关（省时省钱）'}")

    out_dir = Path(opts.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = _run_id(opts, source_label)
    checkpoint = out_dir / f"{run_id}.jsonl"

    done: dict[str, dict[str, Any]] = {}
    if checkpoint.exists() and opts.resume:
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                print(f"⚠ 检查点里有半行 JSON，已忽略（文件被中断过？）：{checkpoint}")
                continue
            done[record.get("id")] = record
        print(f"检查点: {checkpoint.name} 已有 {len(done)} 条，续跑")
    elif checkpoint.exists():
        checkpoint.unlink()
        print(f"检查点: --no-resume，已清空 {checkpoint.name}")

    todo = [item for item in items if item["id"] not in done]
    print(f"本次要跑: {len(todo)} 条" + (f"（跳过已完成的 {len(done)} 条）" if done else ""))

    if todo:
        global _LLM_GATE, _EMBED_LOCK
        semaphore = asyncio.Semaphore(max(1, opts.concurrency))
        _LLM_GATE = asyncio.Semaphore(max(1, opts.llm_concurrency))
        _EMBED_LOCK = asyncio.Lock()

        if M_RELEV in opts.metrics:
            # ⚠ 先在主协程里把嵌入模型热起来：`get_model()` 是懒加载 + 双检锁，几十秒的
            #   首次加载若发生在并发段里，会**把整个事件循环卡住** —— 表现是进度条长时间
            #   一动不动，然后一次性涌出一批结果。预热一次就没事了。
            print("预热嵌入模型…", flush=True)
            try:
                from bidding_qa.embedding import encode_documents

                await asyncio.to_thread(encode_documents, ["预热"])
            except Exception as exc:  # noqa: BLE001
                print(f"⚠ 嵌入模型预热失败（answer_relevancy 那一项会逐题失败）：{exc}")

        async def guarded(item: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await evaluate_one(llm, retrieval, item, opts)

        tasks = [asyncio.create_task(guarded(item)) for item in todo]
        started = time.perf_counter()
        barren = 0
        try:
            with checkpoint.open("a", encoding="utf-8") as handle:
                for index, future in enumerate(asyncio.as_completed(tasks), start=1):
                    record = await future
                    done[record["id"]] = record
                    # ⚠ 只在这一个协程里写文件 —— 多个 task 各写各的会写出半行 JSON，
                    #   而半行 JSON 会让 `--resume` 整个读崩。
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    elapsed = time.perf_counter() - started
                    mark = "空检索" if record.get("empty_retrieval") else "      "
                    print(
                        f"  [{index:>3}/{len(todo)}] {mark} {record['elapsed_ms'] / 1000:>5.1f}s "
                        f"{record['id']:<12} {record['question'][:34]}",
                        flush=True,
                    )
                    # 连续多题「一个指标都没算出来」= 大概率是 Key 失效/欠费，
                    # 与其白跑一小时，不如当场停下说清楚。
                    usable = any(
                        isinstance((record["metrics"].get(name) or {}).get("value"), (int, float))
                        for name in opts.metrics
                    )
                    # ⚠ `--no-llm` 根本不产生指标，这个守卫在那儿只会误伤
                    barren = (
                        0
                        if (opts.no_llm or usable or record.get("empty_retrieval") or record.get("error"))
                        else barren + 1
                    )
                    if barren >= ABORT_AFTER_BARREN:
                        print(f"\n✗ 连续 {barren} 题一个指标都没算出来，判定这一轮不可用，提前中止。")
                        print("  先看 `llm.ready` 与 DeepSeek 额度，再重跑（检查点会续跑）。")
                        return 1
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    records = list(done.values())
    summary = summarize(records, opts)
    worst = _worst(records, opts.metrics, opts.worst)
    print_report(summary, worst, opts)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    payload = {
        "run_id": run_id,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "config": {
            "pipeline": opts.pipeline,
            "mode": opts.mode,
            "top_k": opts.top_k,
            "rerank": opts.rerank,
            "no_llm": opts.no_llm,
            "metrics": list(opts.metrics),
            "retrieval_backend": retrieval.label,
            "min_vector_score": settings.min_vector_score,
            "min_bm25_score": settings.min_bm25_score,
            "min_rerank_score": settings.min_rerank_score,
            # ⚠ 融合三参数与上面三个阈值同源（都是运行期改的 settings），所以也照同样方式记账。
            #   报告里读的是 settings 的**当前值**，不是 option 默认值 —— 扫完一轮回头看
            #   summary.json 时，这三个数就是「这一份是拿什么参数跑出来的」的唯一凭证。
            "rrf_k": settings.rrf_k,
            "vector_weight": settings.vector_weight,
            "bm25_weight": settings.bm25_weight,
            "judge_model": llm.model if llm else None,
            "judge_thinking": opts.judge_thinking,
            "question_source": source_label,
        },
        **summary,
    }
    target = out_dir / f"{stamp}-{run_id}.summary.json"
    target.write_text(json.dumps({**payload, "worst": worst}, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"逐题明细: {checkpoint}")
    print(f"汇总报告: {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RAG 质量评估（LLM 当裁判，按 RAGAS 口径算四项指标）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--selftest", action="store_true", help="只跑离线自检，不联网、不花钱")
    parser.add_argument(
        "--questions", type=Path, default=None,
        help="题目文件（.jsonl 带参考答案 / .txt 每行一题）。不给 = 用语料自带的 74 条做回归基线",
    )
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0 = 全量）")
    parser.add_argument(
        "--metrics", default=",".join(ALL_METRICS),
        help=f"逗号分隔，可选 {'/'.join(ALL_METRICS)}（默认全部四项）",
    )
    parser.add_argument("--pipeline", choices=("chat", "agent"), default="chat",
                        help="chat = 写死流水线（便宜、可复现）；agent = 前端真实链路（贵 3~5 倍，需本地检索）")
    parser.add_argument(
        "--retrieval", choices=("auto", "live", "http"), default="auto",
        help="检索怎么走。auto（默认）先试本地直连，被后端占着锁就自动改走 HTTP；"
             "live 强制本地（后端在跑时会失败）；http 强制打 %s/api/search" % "http://127.0.0.1:8000",
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8000",
                        help="--retrieval http 时后端地址（默认 http://127.0.0.1:8000）")
    parser.add_argument("--no-llm", action="store_true",
                        help="只跑检索层：空检索率 / 原文命中 / 名次。**零 API 调用**，"
                             "扫闸门与模式这些只作用在检索层的旋钮先跑它")
    parser.add_argument("--mode", choices=("hybrid", "vector", "bm25"), default="hybrid",
                        help="检索模式（默认 hybrid）")
    parser.add_argument("--top-k", type=int, default=None, help="检索条数（默认取 settings.top_k）")
    rerank = parser.add_mutually_exclusive_group()
    rerank.add_argument("--rerank", dest="rerank", action="store_true",
                        help="开启重排。⚠ 默认显式关掉，不读 settings.rerank_enabled，保证可复现")
    rerank.add_argument("--no-rerank", dest="rerank", action="store_false")
    parser.set_defaults(rerank=False)
    parser.add_argument("--min-vector-score", type=float, default=None,
                        help="覆盖向量闸门（不改 .env，只在这个进程里生效）")
    parser.add_argument("--min-bm25-score", type=float, default=None, help="覆盖 BM25 闸门")
    parser.add_argument("--min-rerank-score", type=float, default=None, help="覆盖重排闸门")
    # ⚠ 融合参数同样活在进程的 settings 里（`hybrid.reciprocal_rank_fusion` 调用期读），
    #   所以与上面三个阈值受同一条限制：走 HTTP 时改了没用。
    # ⚠ **两个权重同时放大等于没变**：`hybrid.py` 用 `(vw+bw)/(k+1)` 归一化，分子分母同倍
    #   缩放后排序与分数逐字不变。真正起作用的只有**权重比**与 `k`。
    parser.add_argument("--rrf-k", type=int, default=None,
                        help="覆盖 RRF 的 k（默认 60）。越小越偏向单路头部")
    parser.add_argument("--vector-weight", type=float, default=None,
                        help="覆盖向量路权重（默认 1.0）。⚠ 只有与 bm25_weight 的**比值**有意义")
    parser.add_argument("--bm25-weight", type=float, default=None,
                        help="覆盖 BM25 路权重（默认 1.0）")
    parser.add_argument("--concurrency", type=int, default=4, help="并发题数（默认 4；1 = 串行）")
    parser.add_argument("--llm-concurrency", type=int, default=8,
                        help="在飞的大模型请求上限（默认 8）。⚠ 与 --concurrency 是两回事："
                             "一道题内部就有 5 次裁判并发，4 题 × 5 = 20 个请求一起发会撞限流")
    parser.add_argument("--resume", dest="resume", action="store_true", default=True,
                        help="续跑检查点（默认开）")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="忽略检查点，重跑")
    parser.add_argument("--judge-thinking", action="store_true",
                        help="裁判开思考（更慢更贵，默认关）")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "data" / "eval"), help="产物目录")
    parser.add_argument("--worst", type=int, default=10, help="报告里列几道最差题（默认 10）")
    opts = parser.parse_args()

    if opts.selftest:
        return selftest()

    selected = [name.strip() for name in opts.metrics.split(",") if name.strip()]
    unknown = [name for name in selected if name not in ALL_METRICS]
    if unknown:
        print(f"✗ 不认识的指标：{', '.join(unknown)}；可选 {'/'.join(ALL_METRICS)}")
        return 1
    if opts.no_llm:
        # 纯检索层：任何指标都不会被计算，留着它们只会让汇总表里出现一排「—」
        selected = []
    opts.metrics = tuple(selected)
    opts.top_k = opts.top_k or settings.top_k

    # ⚠ 运行期改 settings 属性是**故意的**（已实测 pydantic-settings v2 未设 frozen）：
    #   这几个阈值与融合参数正是「数据驱动优化」最想扫的东西，而本仓库明确不给评估新增
    #   config 字段（新增字段要 .env / .env.example / config.py 三处一起改，漏一处就静默覆盖）。
    # ⚠ 生效靠的是「调用期读全局」：`hybrid.reciprocal_rank_fusion()` 在第 37-38 行现读
    #   `settings.vector_weight`，`retriever.search()` 调它时又不传权重 —— 所以改这里就够。
    #   哪天有人把权重改成 import 期的模块常量，这几个旋钮会**静默失效**。
    for attr, value in (
        ("min_vector_score", opts.min_vector_score),
        ("min_bm25_score", opts.min_bm25_score),
        ("min_rerank_score", opts.min_rerank_score),
        ("rrf_k", opts.rrf_k),
        ("vector_weight", opts.vector_weight),
        ("bm25_weight", opts.bm25_weight),
    ):
        if value is not None:
            setattr(settings, attr, value)

    try:
        return asyncio.run(run(opts))
    except KeyboardInterrupt:
        print("\n已中断。检查点保留，重跑同一条命令会续上。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
