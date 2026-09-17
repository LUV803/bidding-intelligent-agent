#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把语料里的 74 个问题改写成「同一个意思、另一种问法」，产出评估用的**留出集**。

    uv run python scripts/make_eval_questions.py --limit 5   # 先小样看改写质量
    uv run python scripts/make_eval_questions.py             # 全量 74 条
    uv run python scripts/make_eval_questions.py --selftest  # 不联网，验接线

## 为什么需要它

`eval_rag.py` 不传 `--questions` 时拿语料 74 条问答对自测，而**那些问题原文就在索引里**
—— 检索必然命中原文，`context_recall` 恒等于 1.0、原文命中率 100%（实测 MRR=0.9932）。
那是**回归基线**（看改动有没有把已经对的东西弄坏），**测不出检索的真实短板**。

留出集把问法换掉、参考答案不变，于是「改写后的问法还能不能召回原文」变成一个真问题。
它比「语料自测」强，但仍**不等于**真实用户分布（改写题由同一个模型基于原问题生成，
仍偏向原问法的词汇）—— 这条局限写进了 `eval_rag.py` 的文档字符串。

## 三条硬约束（都写进提示词了，改提示词前先读）

1. **不许引入原问题没有的限定条件**（省份 / 年份 / 金额 / 主体）。引入就变成另一个问题了，
   而参考答案没跟着变 —— 那道题的 `context_recall` 会莫名其妙地低，看起来像检索坏了。
2. **不许照抄原句**。产出后逐条跟原问题比对（去标点空白后比），完全相同的**丢弃并报数**。
3. **只要 JSON 数组、不要 markdown 围栏**。⚠ 实测本项目模型不守这条（`vision.py` 上记过一次），
   所以解析照样走宽容路径 —— 借的是 `eval_rag._extract_json`。

## 与 `eval_rag.py` 的关系

宽容解析与裁判调用**直接 import 那个模块的函数**，不抄第二份。理由是
`probe_conv_store.py` 复用 `pg_store._like` 时 CLAUDE.md 记下的那条教训：
**同一个函数对象，别抄第二份** —— 抄了之后两边会各自漂移，而漂移是静默的。

## 产物

`data/processed/eval_questions.jsonl`，一行一个 JSON，UTF-8 无 BOM：

    {"id": "qa-0012-p1", "question": "…", "reference": "原答案全文",
     "doc_id": "qa-0012", "origin": "paraphrase"}

⚠ 这是**可手工编辑的数据文件**：不满意哪几条直接改文本，重跑不用重新生成。
改完直接 `uv run python scripts/eval_rag.py --questions data/processed/eval_questions.jsonl`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

# 照 probe_pg_store.py:26-36 的成例：中文不走 GBK 管道
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 为了 import eval_rag

from bidding_qa.config import settings  # noqa: E402

# ⚠ 复用同一份宽容解析与裁判调用，不抄第二份（见模块文档字符串）
import eval_rag  # noqa: E402
from eval_rag import _extract_json, _judge_json, banner, check  # noqa: E402

# ⚠⚠ `_pass` / `_fail` 是 `eval_rag` 的**模块级变量**，`check()` 用 `global` 就地自增。
#   `from eval_rag import _pass` 只会把**此刻的 0** 拷一份过来，后面怎么加都看不见 ——
#   表现是「屏幕上打了一堆 [FAIL]，退出码却是 0」，脚本化检查会当成通过。
#   所以必须通过模块对象读。这是本文件唯一一处「不 from ... import」的地方。


DEFAULT_OUT = PROJECT_ROOT / "data" / "processed" / "eval_questions.jsonl"

# 改写要**多样性**，所以温度高。⚠ 与裁判的 0.0 正好相反 —— 那边要的是可复现。
DEFAULT_TEMPERATURE = 0.9

_SYSTEM = """\
你是招投标领域的评测集构造助手。用户给你一个真实用户问过的问题，你把它改写成 \
{n} 个**意思完全相同、但问法不同**的新问题，用来测试检索系统。

改写方向（尽量分散，别 {n} 条都用同一种套路）：
- 口语化：「……是怎么回事」→「……是咋弄的」「……怎么办啊」
- 省略主语或宾语（口语里常省略）
- 关键词式（不带疑问词，只堆几个词）
- 同义词替换（「投标保证金」→「投标押金」；「采购人」→「招标方」）
- 调整语序

三条**绝对不能碰**的红线：

1. **不许引入原问题里没有的限定条件**。省份、年份、金额、具体主体、具体项目名称
   —— 原问题没提，改写里就一个字都不许加。加了就变成另一个问题了，而参考答案没跟着变。
2. **不许照抄原句**。改写后的字符串必须与原问题不同。
3. **不要改变提问的意图**。原问题问什么，改写后还是问什么。

只输出一个 JSON 数组，形如 ["改写1", "改写2"]，**不要** markdown 围栏、不要任何解释。\
"""


def _normalize(text: str) -> str:
    """去标点、去空白、统一大小写 —— 专门用来判「是不是照抄了原句」。

    ⚠ 只用于**比对**，不用于输出：`「」`/`""` 这类引号在中文问题里是有意义的。
    """
    return re.sub(r"[\s\W_]+", "", text).lower()


# 常见的「限定条件」形态。改写里出现而原问题里没有的，基本就是红线 1 被踩了。
_QUALIFIER_RE = re.compile(
    r"\d{4}\s*年"  # 年份
    r"|[\d.]+\s*(?:万|亿|元|%|％)"  # 金额、比例
    r"|[一-龥]{2,8}(?:省|市|自治区|自治州)"  # 省份 / 地市
)


def _new_qualifiers(original: str, paraphrase: str) -> list[str]:
    """改写里新出现、而原问题里没有的限定条件。**警告用，不据此丢弃。**

    ⚠ 刻意只警告不丢弃：这个正则会误报（比如原问题里的省份换个写法就匹配不上了），
    而误丢一条改写题的代价是**留出集少一条**，比留一条略偏的题更难发现。人扫一眼
    `--limit 5` 的产出就能判断，不值得让脚本自作主张。

    （与 `rag.is_enum_question()` 那条「误判代价不对称」是同一种取舍，方向相反：
    那边宁可多判、这边宁可少扔。）
    """
    have = set(_QUALIFIER_RE.findall(original))
    return [q for q in dict.fromkeys(_QUALIFIER_RE.findall(paraphrase)) if q not in have]


def _build_user_prompt(question: str, n: int) -> str:
    return f"原问题（{n} 条改写都基于它）：\n{question}"


def _parse_paraphrases(data: object, n: int) -> list[str]:
    """从模型输出里取回改写文本。宽容 —— 模型可能回数组、也可能回 {{"questions": [...]}}。

    ⚠ 返回条数可能少于 `n`（模型少给了），调用方按实际条数处理，**不补齐、不报错**。
    """
    candidates: object = data
    if isinstance(data, dict):
        # 模型偶尔会自作主张包一层。按可能性从高到低找第一个 list。
        for key in ("questions", "paraphrases", "rewrites", "result", "data", "items"):
            if isinstance(data.get(key), list):
                candidates = data[key]
                break
        else:
            candidates = list(data.values())

    if not isinstance(candidates, list):
        return []
    # ⚠ `isinstance(True, int)` 是真的，但这里 `isinstance(x, str)` 已排除布尔
    return [item.strip() for item in candidates if isinstance(item, str) and item.strip()][:n]


def build_records(docs: list[dict], paraphrases: dict[str, list[str]]) -> tuple[list[dict], dict]:
    """把改写结果拼成留出集记录，顺带统计丢弃情况。

    丢弃 = 与原问题去标点后完全相同（红线 2）。**完全相同的不写进文件**，
    否则那条题测的还是原文，`context_recall` 会虚高，等于把留出集偷偷退回成基线。
    """
    records: list[dict] = []
    verbatim = 0
    dup_within = 0
    warnings: list[tuple[str, str, list[str]]] = []

    for doc in docs:
        doc_id = doc["doc_id"]
        seen: set[str] = set()
        index = 0
        for text in paraphrases.get(doc_id, []):
            key = _normalize(text)
            if not key:
                continue
            if key == _normalize(doc["question"]):
                verbatim += 1
                continue
            if key in seen:  # 同一条问题自己改出两个一样的
                dup_within += 1
                continue
            seen.add(key)
            index += 1
            new_q = _new_qualifiers(doc["question"], text)
            if new_q:
                warnings.append((f"{doc_id}-p{index}", text, new_q))
            records.append(
                {
                    "id": f"{doc_id}-p{index}",
                    "question": text,
                    # ⚠ 参考答案**原样沿用**语料答案：改写的只是问法，答案没变。
                    #   这正是 context_recall 在留出集上仍然算得出来的原因。
                    "reference": doc["answer"],
                    "doc_id": doc_id,
                    "origin": "paraphrase",
                }
            )

    return records, {
        "verbatim": verbatim,
        "dup_within": dup_within,
        "warnings": warnings,
    }


def write_jsonl(records: list[dict], path: Path) -> None:
    """UTF-8 无 BOM，`newline="\\n"`。

    ⚠ `newline="\\n"` 不能省：Windows 上默认会把 `\\n` 翻成 `\\r\\n`，而 `eval_rag.py`
    读回时按行切 —— 多出来的 `\\r` 会进到 JSON 字符串里（表现为问题末尾多一个看不见的字符）
    或让 `json.loads` 直接报错。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


async def generate(docs: list[dict], opts: argparse.Namespace) -> tuple[dict, list[str]]:
    """逐条改写。返回 `({doc_id: [改写…]}, 错误列表)`。

    ⚠ **串行**：这里没有并发需求（74 次调用，每次十几秒也就二十来分钟，而且只跑一次），
    而 `eval_rag` 那套并发是为「反复跑全量」准备的。少一处并发就少一处难查的问题。
    """
    from bidding_qa.llm import get_llm

    llm = get_llm()
    if not llm.ready:
        return {}, ["未配置 DEEPSEEK_API_KEY"]

    system = _SYSTEM.replace("{n}", str(opts.n_per_doc))
    out: dict[str, list[str]] = {}
    errors: list[str] = []

    for i, doc in enumerate(docs, start=1):
        data, error = await _judge_json(
            llm,
            system,
            _build_user_prompt(doc["question"], opts.n_per_doc),
            thinking=False,  # 改写不需要思维链，纯浪费
            max_tokens=1024,
            temperature=opts.temperature,
        )
        if error:
            errors.append(f"{doc['doc_id']}: {error}")
            print(f"  [{i}/{len(docs)}] ✗ {doc['doc_id']}  {error[:70]}")
            continue
        got = _parse_paraphrases(data, opts.n_per_doc)
        if not got:
            errors.append(f"{doc['doc_id']}: 解析成功但一条改写都没有")
            print(f"  [{i}/{len(docs)}] ✗ {doc['doc_id']}  解析成功但一条改写都没有")
            continue
        out[doc["doc_id"]] = got
        print(f"  [{i}/{len(docs)}] ✓ {doc['doc_id']}  {len(got)} 条  {got[0][:36]}")

    return out, errors


def _read_back(path: Path) -> list[dict]:
    """读回并**逐字段校验** —— 文件是给人手改的，改了格式要当场报错而不是静默跑偏。"""
    records = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {lineno} 行不是合法 JSON：{exc}") from exc
        missing = [key for key in ("id", "question") if not record.get(key)]
        if missing:
            raise ValueError(f"第 {lineno} 行缺必填字段：{missing}")
        records.append(record)
    return records


def selftest() -> int:
    """不联网、不需要任何服务。"""
    banner("① 宽容解析：模型不守「只吐 JSON 数组」的约定")
    for label, raw, want in [
        ("裸数组", '["改写一", "改写二"]', ["改写一", "改写二"]),
        ("带 ```json 围栏", '```json\n["改写一"]\n```', ["改写一"]),
        ("前后有废话", '好的，这是改写：\n["改写一"]\n希望有帮助！', ["改写一"]),
        ("包了一层对象", '{"questions": ["改写一"]}', ["改写一"]),
        ("里层换行", '["改写\n一"]', ["改写\n一"]),
    ]:
        check(f"{label} → 解出来", _parse_paraphrases(_extract_json(raw), 3) == want,
              f"实际 {_parse_paraphrases(_extract_json(raw), 3)}")

    banner("② 模型少给 / 给错类型时不能崩")
    for label, data in [
        ("少给（要 3 给 1）", ["只有一条"]),
        ("混进非字符串", ["好的", 42, None, "坏的"]),
        ("全是非字符串", [1, 2, 3]),
        ("根本不是列表", "一段散文"),
        ("None", None),
    ]:
        try:
            got = _parse_paraphrases(data, 3)
            ok = isinstance(got, list) and all(isinstance(x, str) for x in got)
            check(f"{label} → 返回 str 列表（{len(got)} 条）", ok, f"实际 {got!r}")
        except Exception as exc:  # noqa: BLE001
            check(f"{label} → 返回 str 列表", False, f"{type(exc).__name__}: {exc}")

    banner("③ 照抄原句必须被丢弃（否则留出集偷偷退回成基线）")
    docs = [
        {"doc_id": "qa-0001", "question": "单一来源采购公示被质疑怎么办？", "answer": "答一"},
        {"doc_id": "qa-0002", "question": "保证金比例是多少？", "answer": "答二"},
    ]
    paraphrases = {
        # 第 1 条原样照抄；第 2 条只是标点不同（也必须算照抄）
        "qa-0001": ["单一来源采购公示被质疑怎么办？", "公示被质疑了该怎么办"],
        "qa-0002": ["保证金比例是多少", "保证金要交多少比例"],
    }
    records, stats = build_records(docs, paraphrases)
    check("照抄的被丢弃（2 条：原样 + 仅标点不同）", stats["verbatim"] == 2, f"实际 {stats}")
    check("留下的都是真改写", len(records) == 2, f"实际留下 {len(records)} 条")
    check("id 形如 qa-0001-p1", [r["id"] for r in records] == ["qa-0001-p1", "qa-0002-p1"],
          f"实际 {[r['id'] for r in records]}")
    check("参考答案原样沿用（context_recall 靠它才算得出来）",
          [r["reference"] for r in records] == ["答一", "答二"],
          f"实际 {[r['reference'] for r in records]}")
    check("doc_id 指回原语料条目", [r["doc_id"] for r in records] == ["qa-0001", "qa-0002"], "")

    banner("④ 同一条问题改出两个一样的 → 只留一个")
    _, stats2 = build_records(docs, {"qa-0001": ["甲问法", "甲问法！"]})
    check("去重丢弃 1 条", stats2["dup_within"] == 1, f"实际 {stats2}")

    banner("⑤ 新出现的限定条件要被标出来（只警告，不丢弃）")
    docs3 = [{"doc_id": "qa-0003", "question": "投标保证金可以退还吗？", "answer": "答"}]
    records3, stats3 = build_records(docs3, {"qa-0003": ["安徽省投标保证金可以退还吗", "保证金能退吗"]})
    check("安徽那条被标出来", len(stats3["warnings"]) == 1, f"实际 {stats3['warnings']}")
    check("⚠ 但照样写进文件（宁可人工扫一眼，也不脚本自作主张）", len(records3) == 2,
          f"实际留下 {len(records3)} 条")
    check("原问题里本来就有的限定词不算新增",
          _new_qualifiers("2026年的保证金比例", "2026年保证金交多少") == [],
          f"实际 {_new_qualifiers('2026年的保证金比例', '2026年保证金交多少')}")
    check("金额也算限定条件",
          _new_qualifiers("保证金比例是多少", "50万的保证金比例是多少") == ["50万"],
          f"实际 {_new_qualifiers('保证金比例是多少', '50万的保证金比例是多少')}")

    banner("⑥ JSONL 往返（中文 / 换行 / 引号 / 反斜杠）")
    import tempfile

    tricky = [{"id": "qa-0001-p1", "question": '含"引号"、换行\n、反斜杠\\ 与 emoji 🚀',
               "reference": "中文答案", "doc_id": "qa-0001", "origin": "paraphrase"}]
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sub" / "q.jsonl"  # 顺带验目录不存在时会自动建
        write_jsonl(tricky, target)
        back = _read_back(target)
        check("逐字段相等", back == tricky, f"实际 {back}")
        raw = target.read_bytes()
        check("无 BOM", not raw.startswith(b"\xef\xbb\xbf"), f"头三字节 {raw[:3]!r}")
        check("行尾是 \\n 而不是 \\r\\n", b"\r\n" not in raw, "出现了 CRLF")

    banner("⑦ 手改过的文件格式有错时必须当场报错，而不是静默跑偏")
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.jsonl"
        bad.write_text('{"id": "a", "question": "q"}\n这不是 JSON\n', encoding="utf-8")
        try:
            _read_back(bad)
            check("非法 JSON 报错", False, "居然没报错")
        except ValueError as exc:
            check("非法 JSON 报错", "第 2 行" in str(exc), f"实际 {exc}")
        missing = Path(tmp) / "missing.jsonl"
        missing.write_text('{"id": "a"}\n', encoding="utf-8")
        try:
            _read_back(missing)
            check("缺 question 报错", False, "居然没报错")
        except ValueError as exc:
            check("缺 question 报错", "question" in str(exc), f"实际 {exc}")

    banner(f"结果：{eval_rag._pass} 通过 / {eval_rag._fail} 失败")
    return 0 if eval_rag._fail == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把语料问题改写成留出集（LLM 改写，参考答案沿用原文）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--n-per-doc", type=int, default=1, help="每个问题改写几条（默认 1）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"输出路径（默认 {DEFAULT_OUT}）")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE,
                        help=f"改写温度（默认 {DEFAULT_TEMPERATURE}，要多样性）")
    parser.add_argument("--selftest", action="store_true", help="不联网，只验解析与拼装")
    opts = parser.parse_args()

    if opts.selftest:
        return selftest()

    if opts.n_per_doc < 1:
        print("✗ --n-per-doc 至少为 1")
        return 1

    from bidding_qa.dataset import load_qa_docs

    docs = load_qa_docs()
    if opts.limit > 0:
        docs = docs[: opts.limit]
    print(f"语料 {len(docs)} 条，每条改写 {opts.n_per_doc} 遍，温度 {opts.temperature}")
    print(f"预计 {len(docs)} 次调用\n")

    paraphrases, errors = asyncio.run(generate(docs, opts))
    if not paraphrases:
        print(f"\n✗ 一条改写都没生成。{errors[:1]}")
        return 1

    records, stats = build_records(docs, paraphrases)
    write_jsonl(records, opts.out)

    print(f"\n{'=' * 68}")
    print(f"写出 {len(records)} 条 → {opts.out}")
    print(f"  丢弃照抄原句      : {stats['verbatim']} 条")
    print(f"  丢弃重复改写      : {stats['dup_within']} 条")
    print(f"  失败的语料条目    : {len(errors)} 条")
    if stats["warnings"]:
        # ⚠ 这段**不是**装饰：新出现的限定条件会让那道题的 context_recall 莫名偏低，
        #   看起来像检索坏了。扫一眼就能判断，所以只警告不自动丢。
        print(f"\n⚠ {len(stats['warnings'])} 条改写里出现了原问题没有的限定条件（年份/金额/地名）：")
        for rid, text, quals in stats["warnings"][:10]:
            print(f"    {rid}  +{'/'.join(quals)}  {text[:44]}")
        print("  改完意思是另一回事了就手改这个文件（它本来就是给人改的），或删掉那几行。")
    if errors:
        print(f"\n失败的条目（改写只成功了一部分，文件里没有它们）：")
        for line in errors[:5]:
            print(f"    {line[:100]}")

    print(f"\n下一步：")
    print(f"  uv run python scripts/eval_rag.py --questions {opts.out}")
    print(f"  ⚠ 留出集不是独立评测集：改写题由同一个模型基于原问题生成，仍偏向原问法的词汇，")
    print(f"    会比真实用户提问**更容易召回**。拿它跟语料基线比，别拿绝对值当权威。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
