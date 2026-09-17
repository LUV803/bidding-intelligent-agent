#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Agent 综合评估：跑**全工具**链路，同时量「工具选得对不对」与「答得好不好」。

    uv run python eval/agent_eval.py                # 全量 10 题（需停掉后端）
    uv run python eval/agent_eval.py --selftest     # 离线断言，零 API、零服务
    uv run python eval/agent_eval.py --check-tools  # 只报四个工具的可用性，不发模型请求
    uv run python eval/agent_eval.py --limit 3      # 小样

⚠⚠ **必须 `uv run python`**（或用激活后的 venv）。PATH 上那个裸 `python` 是 3.8，
低于本项目要求的 3.12，`import eval_rag` 会在编译期炸成一句指向 `scripts/eval_rag.py`
的 `SyntaxError` —— 跟真正的原因（解释器不对）毫无关系。脚本开头拦了一道并直接告诉你
该用哪个命令；**在 Git Bash 里还要加 `PYTHONIOENCODING=utf-8`**，否则一屏乱码。

## 与 `scripts/eval_rag.py --pipeline agent` 的分野（最要紧的一条）

那个脚本**主动把三个可选工具摘掉**（`_isolate_agent_tools()`），只留检索 —— 不摘的话
图谱行与网页正文混进上下文，四项指标全失去意义。所以它测的是「一个只会检索的 Agent」，
**工具路由这个 Agent 最核心的行为根本没被测量**。本脚本补的就是那一层：四个工具全挂着。

⇒ ⚠⚠ **绝不要在这里调 `_isolate_agent_tools()`**（它还会把 `graph_store.ENABLED` /
`pg_store.ENABLED` 置假，连带改掉提示词片段）。

## 怎么看见 Agent 的内部状态（本脚本的设计核心）

SSE 事件是**冻结的前后端契约**，为评估去改它有真实回归风险。但事件里缺了三样评估要的东西：
`tool_call.query` 是 `peek_display()` 的**展示串**而非模型给的参数；`tool_result` 不带
工具返回的正文；并且**没有任何事件能告诉你哪一轮是被强制的**（`_FORCE_GRAPH_CHOICE`
与收尾轮）。⚠ 注意「强制轮不发事件」是**错的** —— `agent.py:359-362` 只换了流的来源，
那一轮照样走 :372-457 的公共后处理，`tool_call` / `tool_result` 正常发；看不见的是
*「这一轮被强制了」*这件事本身。

解法是用**依赖注入那条缝**：`run_agent_stream(llm, retriever, ...)` 的第一个参数就是 llm
（`agent.py:219`），于是传一个鸭子类型的 `RecordingLLM` 进去，它记下**每次**喂给模型的
`messages` 与 `tool_choice`。最后一轮的快照里同时有：
  ① 每条 `role:"tool"` 消息的正文 = **工具真正返回的证据**；
  ② 每条 assistant 的 `tool_calls[].function.arguments` = **模型的原始参数**。
⇒ **零改动 `agent.py`、零改动 SSE 契约。**

## ⚠⚠ faithfulness 判据：`--faith-context evidence`（默认）vs `sources`

Agent 链路里**图谱与采购库的结果不是 `Source`**、从不进 `sources`（CLAUDE.md「引用编号」
那条不变量：Source 是问答对形状，图谱是「实体+统计」，塞进去语义不对）。所以只拿
`sources` 当判据的话，**任何从图谱/数据库得来的答案 faithfulness 恒为 0** ——
而那恰恰是本脚本存在要测的行为，是**系统性低估且报告上看不出异常**。

| 取值 | 判据 | 什么时候用 |
| --- | --- | --- |
| `evidence`（默认） | sources 的答条 + 图谱/采购库/联网工具的返回正文 | 诚实地测「回答有没有超出 Agent 实际拿到的证据」 |
| `sources` | 只有 sources | 与 `eval_rag.py` 的既有数字**可比**（那边也是这个口径） |

## ⚠ 三个运维前提（跑之前必须知道）

1. **必须先停掉后端**。本地 Qdrant 是嵌入式文件模式，对 `data/qdrant_local/.lock` 加排他锁；
   后端在跑时 `get_retriever()` 直接抛 `RuntimeError`。本脚本默认 `--retrieval live`，
   撞上锁会明确报错退出，不静默降级。
2. **Neo4j / PostgreSQL 得在跑**，否则对应工具的用例会如实拿到 `ok=False`。
   工具**没配置**时该类题**跳过**（分母缩小并点名原因）；**配置了但调用失败**是**真问题**，
   如实计失败 —— 两者刻意分开。
3. **Tavily 额度**：只有 `web: true` 的用例会联网，每次 1 个 credit（空结果重试到 2~3 个）。

## 已知局限（别误读数字）

1. **10 条样本太小**：工具准确率每错一题就是 10 个百分点。**只用来看趋势**。
   用例文件可随时加题，加题不用改代码。
2. **与 `eval_rag.py` 的数字不可直接比**（那边摘了工具，且默认口径也不同）。
   只有同一份用例上的相对变化有意义。判分噪声底沿用实测值：均值 ±0.005、单题 ±0.28。
3. **裁判与被评模型是同一个**（本机没有第二个可用文本大模型）—— 自评偏高。
4. **预检索不是工具调用**：kb 类题的 `allow` 因此把「不调工具」也放行（见下）。
5. 不测多轮对话（每题独立、`history=[]`）、不测图片。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

# 照 probe_pg_store.py:26-36 的成例：中文不走 GBK 管道
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ⚠⚠ **裸 `python eval/agent_eval.py` 会失败，而且失败得很难看**（实测）：
#   这台机器 PATH 上的 `python` 不是本项目的 venv，版本低于 3.12，于是 `import eval_rag`
#   在**编译期**就抛 `SyntaxError: f-string expression part cannot include a backslash`
#   —— 报错指向 `scripts/eval_rag.py` 的一个行号，而那个文件用户根本没碰过，
#   完全看不出真正的原因是「用错了解释器」。
#   ⇒ 在这里先拦一道，把话说清楚（同本仓库「明确报错退出、别静默降级」的老规矩）。
#   本项目的 venv 是 3.12，`eval_rag.py` 里用到了 3.12 才允许的 f-string 写法。
if sys.version_info < (3, 12):
    print(f"✗ 需要 Python ≥ 3.12，当前是 {sys.version.split()[0]}。")
    print("  这个解释器不是本项目的 .venv —— 请改用：")
    print("      uv run python eval/agent_eval.py ...")
    raise SystemExit(2)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))  # 为了 import eval_rag

try:
    import eval_rag as E  # noqa: E402
except (ImportError, SyntaxError) as _exc:  # pragma: no cover - 环境问题，不是逻辑分支
    print(f"✗ 无法 import eval_rag（{type(_exc).__name__}: {_exc}）")
    print("  它要么没装在本解释器里，要么版本不对。请用：")
    print("      uv run python eval/agent_eval.py ...")
    print("  （本脚本复用 scripts/eval_rag.py 的三个 metric_* 函数，不抄第二份。）")
    raise SystemExit(2)

# ============================================================================
# 常量
# ============================================================================

OUR_METRICS: tuple[str, ...] = (E.M_FAITH, E.M_RELEV, E.M_RECALL)
"""只要三项 —— 用户给定的报告里没有 context_precision。"""

#: 报告上的显示名（`E.M_*` 是内部名，别拿来直接打）
METRIC_LABELS: dict[str, str] = {
    E.M_FAITH: "Faithfulness",
    E.M_RELEV: "Relevancy",
    E.M_RECALL: "Recall",
}

RAG_WEIGHT = 0.6
TOOL_WEIGHT = 0.4
"""⚠⚠ 这两个权重与「综合评分 = 三项等权平均」是从用户给的示例数字**反推验证**过的：
`(0.31+0.41+0.23)/3 = 0.3167 → 0.32`、`0.6×0.32 + 0.4×1.00 = 0.592 → 0.59`。
`--selftest` ⓬ 把那组数字逐位钉死 —— 改公式会当场变红。"""

TOOL_CONTEXT_CHARS = 4000
"""⚠⚠ **别改用 `E.CONTEXT_CHARS`（1200）**。那个值是配着语料里 500 字左右的答条标定的，
而 `graph_store.render_for_model` / `pg_store.render_for_model` 会打出 `graph_max_rows` /
`pg_max_rows` 那么多明细行。用 1200 会把**答案引用了的那几行直接切掉**，
faithfulness 随之被压低 —— 恰好压的就是工具类题，也就是本脚本存在要测的那批。
"""

_DEFAULT_CASES = Path(__file__).resolve().parent / "agent_cases.jsonl"

_TOOL_LABELS = {
    "search_bidding_knowledge": "检索",
    "search_knowledge_graph": "图谱",
    "search_procurement_records": "采购库",
    "search_web": "联网",
}


def _tlabel(name: str) -> str:
    return _TOOL_LABELS.get(name, name)


# ============================================================================
# RecordingLLM —— 问题 1 的解法
# ============================================================================


class RecordingLLM:
    """鸭子类型的 LLM 包装器：透传一切，顺手记下每次模型请求的 messages 与 tool_choice。

    ⚠⚠ **刻意不继承 `BaseLLMClient`**。CLAUDE.md 明写那个 ABC 只做契约声明、
    「唯一消费方是类型标注」，仓库里的假 LLM 全是鸭子类型、**不继承它**；
    在那个基类上加 `isinstance` 关卡会把替身挡在门外。反过来，继承它反而要覆盖
    `OpenAICompatLLM.__init__` / `ready` / 两个默认值抽象属性一堆东西。

    而 `agent.py` 全路径只读 llm 的**三处**（已核实）：
      - `llm.ready`                       （:309 / :331 / :333）
      - `llm.stream_with_tools(...)`      （:140 / :156）
      - `llm._NOT_CONFIGURED`             （经 `fallback_answer` → `rag._no_key_hint`，
                                            用的是 `getattr(..., "")`，安全）
    所以 `__getattr__` 转发就够了。
    """

    def __init__(self, inner: Any) -> None:
        # ⚠ 用 `__dict__` 直写，见 __getattr__ 里那条注释
        self.__dict__["_inner"] = inner
        self.rounds: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        # ⚠⚠ **必须走 `self.__dict__.get`，不能写 `self._inner`**：构造期与
        #    `copy.deepcopy` 都会探测属性名（`__deepcopy__` / `__getstate__` / `__setstate__`），
        #    走 `self._inner` 会在 `_inner` 还没落地时再次进入 `__getattr__` →
        #    **无限递归 RecursionError**，而且栈里全是 `__getattr__`，看不出真正的原因。
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
        tool_choice: Any = None,
        disable_thinking: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        call_id_prefix: str = "",
    ) -> Any:
        """记一笔再把活派给真客户端。

        ⚠⚠ **snapshot 必须在这里 deepcopy，绝不能存引用**：`messages` 是**同一个 list
        对象**，`run_agent_stream` 每一轮都往它 append assistant / `role:"tool"` 消息
        （`agent.py:410` / `:434`）。存引用的话五轮记下来的是**同一份终态** ——
        前几轮的工具证据全丢，而且丢得**看不出来**：证据反而更全、faithfulness 更高、
        报告一切正常。`--selftest` ① 就是钉这条的。

        ⚠ **必须是普通 `def`，不能写成 `async def`**：调用方的形状是
        `async for ev in llm.stream_with_tools(...)`（**从不 await**）。写成
        async generator 的话记录动作要拖到第一次 `__anext__` 才跑；普通 def 在
        **调用那一刻**同步记录，时机最确定，且返回的本来就是真客户端给的异步迭代器。
        """
        self.rounds.append({
            "messages": copy.deepcopy(messages),
            # ⚠ `tool_choice` 是**唯一能识别「这一轮被强制了」的地方** —— SSE 不带它。
            "tool_choice": tool_choice,
            "disable_thinking": disable_thinking,
            "call_id_prefix": call_id_prefix,
            "tools": [t["function"]["name"] for t in (tools or [])],
        })
        return self._inner.stream_with_tools(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            disable_thinking=disable_thinking,
            temperature=temperature,
            max_tokens=max_tokens,
            call_id_prefix=call_id_prefix,
        )


def _tool_calls_of(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从记录的轮次里抠出**模型发出的全部工具调用**（含强制轮）。

    ⚠ 按 `call_id_prefix` 去重后**保留最后一次**：`_stream_round` 在撞上
    `reasoning_content` 400 时会用**同一个 prefix** 重试（`agent.py:148-165`），
    `_stream_round_forced_graph` 也一样（`:203-215`）。同一个 prefix 可能出现两次，
    而**产生最终事件的是后一次**。
    """
    last_per_prefix: dict[str, dict[str, Any]] = {}
    for rnd in rounds:
        last_per_prefix[rnd["call_id_prefix"]] = rnd

    calls: list[dict[str, Any]] = []
    for prefix, rnd in last_per_prefix.items():
        for msg in rnd["messages"]:
            if msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                calls.append({
                    "round": prefix,
                    "id": call.get("id", ""),
                    "name": fn.get("name", ""),
                    "args": fn.get("arguments", ""),
                    "forced": rnd["tool_choice"] is not None,
                })
    return calls


def _tool_names_of(rounds: list[dict[str, Any]]) -> list[str]:
    """去重后的工具名（**有序**，便于报告里照实打出来）。"""
    seen: list[str] = []
    for call in _tool_calls_of(rounds):
        name = call["name"]
        if name and name not in seen:
            seen.append(name)
    return seen


# ============================================================================
# 证据拼装 —— 问题 2 的解法
# ============================================================================


def collect_evidence(
    rounds: list[dict[str, Any]],
    sources: list[Any],
    *,
    mode: str = "evidence",
) -> dict[str, Any]:
    """把「模型真正看过的资料」拼成裁判用的上下文。返回两个口径，供开关切换。

    两个来源缺一不可：
      ① `sources`（预检索 + 网页）—— 它们**不在**任何 `role:"tool"` 消息里，
         是 `build_agent_messages` 直接拼进提示词的；
      ② `role:"tool"` 消息的正文 —— 图谱/采购库/联网的结果**只**在这里
         （图谱与采购库不进 `SourceRegistry`，理由见模块文档字符串）。

    ⚠ **检索工具的 `role:"tool"` 正文要跳过**：它是 `_render_result` 里
    `build_context(fresh)` 的产出，与 `sources` 里的答条**逐字重复**。并进去只是把同样的
    资料读两遍，白烧裁判预算，还会把 `TOOL_CONTEXT_CHARS` 的截断预算挤掉。
    """
    source_text = E._contexts_text([getattr(s, "answer", "") or "" for s in sources])

    # 工具消息在**每一轮更长的快照**里都会重现，按 tool_call_id 去重
    by_call_id: dict[str, str] = {}
    for rnd in rounds:
        for msg in rnd["messages"]:
            if msg.get("role") != "tool":
                continue
            cid = msg.get("tool_call_id") or ""
            if cid and cid not in by_call_id:
                by_call_id[cid] = msg.get("content") or ""

    # 归属：拿 tool_call_id 回到 assistant 那条消息里查名字
    id_to_name: dict[str, str] = {}
    for call in _tool_calls_of(rounds):
        if call["id"]:
            id_to_name[call["id"]] = call["name"]

    tool_chunks: list[str] = []
    for cid, text in by_call_id.items():
        name = id_to_name.get(cid, "")
        if name in ("", "search_bidding_knowledge"):
            continue  # 检索的正文与 sources 重复，见 docstring
        if not text.strip():
            continue
        if len(text) > TOOL_CONTEXT_CHARS:
            text = text[:TOOL_CONTEXT_CHARS] + "…（已截断）"
        tool_chunks.append(f"【{_tlabel(name)}工具返回】\n{text}")

    evidence = source_text
    if tool_chunks:
        evidence = (evidence + "\n\n" + "\n\n".join(tool_chunks)).strip()

    return {
        "evidence": evidence,
        "sources": source_text,
        "tool_chars": sum(len(c) for c in tool_chunks),
        "tool_chunk_count": len(tool_chunks),
    }


def judge_context(evidence: dict[str, Any], mode: str) -> str:
    return evidence["evidence"] if mode == "evidence" else evidence["sources"]


# ============================================================================
# 用例
# ============================================================================


def load_cases(path: Path) -> list[dict[str, Any]]:
    """读用例。宽容跳过（空行 / `#` 注释 / 坏 JSON / 缺 question / id 重复）。

    ⚠ **刻意不复用 `E.load_questions`**：它返回固定 5 键的 dict，会把 `allow` /
    `require` / `web` 这些**静默丢掉** —— 那样每条用例都会退化成「不许调任何工具」
    的负例，工具准确率恒等于「模型从来不调工具」的比例，看起来还挺合理。
    """
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not path.exists():
        raise SystemExit(f"✗ 找不到用例文件：{path}")
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"⚠ 第 {lineno} 行不是合法 JSON，跳过：{exc}")
            continue
        if not item.get("question"):
            print(f"⚠ 第 {lineno} 行没有 question，跳过")
            continue
        cid = item.get("id") or f"line-{lineno}"
        if cid in seen:
            print(f"⚠ 第 {lineno} 行 id 重复（{cid}），跳过")
            continue
        seen.add(cid)
        item["id"] = cid
        item.setdefault("reference", "")
        item.setdefault("doc_id", "")
        item.setdefault("allow", [])
        item.setdefault("require", [])
        item.setdefault("web", False)
        # ⚠⚠ `rag: false` = **这题只测路由、不参与三项 RAG 指标**。
        #   存在的理由是「指标在该题上没有定义」，而**不是**「它得分低、想把它去掉」。
        #   判据：题目本身没有可被忠实/相关/召回的东西 —— 目前只有寒暄负例（ag-10）。
        #   真机实测放进去的后果：faithfulness 恒为 0.00（「不客气」当然不被任何资料
        #   『支持』），而那是**定义使然**，不是质量信号，混进均值就是给综合评分压分。
        #   ⚠ 别拿它当「这题跑砸了、把它摘出去」的开关 —— 那样报告就成了自证清白。
        item.setdefault("rag", True)
        item.setdefault("why", "")
        cases.append(item)
    return cases


def grade_routing(case: dict[str, Any], called: list[str]) -> bool | None:
    """工具路由判分。返回 `None` 表示这题**跳过**（期望的工具压根没配置）。

    ```
    hit ⟺ require ⊆ called ⊆ allow
    ```

    ⚠⚠ **为什么 `allow` 而不是「期望一个工具名」**：Agent 链路里**预检索与预联网是
    无条件的**（`run_agent_stream` 在模型第一轮之前就跑完），而那两步**不是工具调用**。
    于是对 kb / web 类题，「模型直接作答」与「模型调了对应工具」**都是路由正确** ——
    前者说明预检索已经够用。写成单个期望值会把这种正确行为判成失败。
    ⚠ 但图谱与采购库**没有任何东西替它们预跑**，所以那两类题的 `require` 是非空的 ——
    不调工具就答对统计题是不可能的。
    """
    allowed = set(case.get("allow") or [])
    required = set(case.get("require") or [])
    got = set(called)

    missing = required - got
    if missing and not missing <= _configured_tools():
        # 期望的工具压根没配（图谱/采购库关着）—— 这题**跳过**，别算成模型选错了。
        # ⚠ 与「配了但模型没调」是两件事：后者是真失败，见下。
        return None
    if missing:
        return False
    return got <= allowed


_CONFIGURED: set[str] | None = None


def _configured_tools() -> set[str]:
    """本轮**配得上**的工具集合（进程级开关 + 有没有 Key）。只读，不建连接。"""
    global _CONFIGURED
    if _CONFIGURED is None:
        from bidding_qa import graph_store, pg_store, web_search
        from bidding_qa.tools import rag_tools

        names = {s["function"]["name"] for s in rag_tools.TOOL_SCHEMAS}
        if web_search.ENABLED:
            names.add(rag_tools.TOOL_WEB)
        _CONFIGURED = names
        _ = (graph_store, pg_store)  # 仅供可读性；ENABLED 已体现在 TOOL_SCHEMAS 里
    return _CONFIGURED


def _warm_and_report() -> dict[str, dict]:
    """真去连一次图谱/采购库，把「配了但连不上」和「配了且能用」分开。

    ⚠⚠ **必须真的 `warmup()`**：这两个 store 都是**懒连接**，不热身的话
    `graph_status()` / `pg_status()` 一律回 `{'configured': True, 'available': False,
    'detail': '尚未建立连接'}` —— 那说的是「还没试过」，**不是「连不上」**。
    拿它当预检结果会得出「两个工具都不可用」的假警报，而它其实可能好好的。

    ⚠ 两者的 `warmup()` 都是**吞异常**的（「降级而非报错」），所以这里不会抛，
    只会体现在 `available` / `detail` 上。
    """
    from bidding_qa import graph_store, pg_store

    for store in (graph_store, pg_store):
        try:
            store.warmup()
        except Exception as exc:  # noqa: BLE001  理论上到不了，兜一手
            pass
    return {"graph": graph_store.graph_status(), "pg": pg_store.pg_status()}


def _skip_reason(case: dict[str, Any]) -> str:
    """这题期望的工具里，哪些没配置。"""
    missing = sorted(set(case.get("require") or []) - _configured_tools())
    return "、".join(f"{m}（未配置）" for m in missing)


# ============================================================================
# 跑一题
# ============================================================================


async def run_case(
    llm: Any,
    retriever: Any,
    case: dict[str, Any],
    opts: argparse.Namespace,
) -> dict[str, Any]:
    """跑一题：真 Agent 链路（全工具）+ 三项 RAG 指标。

    **任何异常都转成字段里的 error，绝不向上抛** —— 与 `E.evaluate_one` 同一条
    「降级而非报错」的老规矩：一题挂掉不该让跑了一半的整轮全废。
    """
    from bidding_qa.agent import run_agent_stream

    record: dict[str, Any] = {
        "id": case["id"],
        "question": case["question"],
        "reference": case.get("reference") or "",
        "doc_id": case.get("doc_id") or "",
        "why": case.get("why") or "",
        "rag": bool(case.get("rag", True)),
        "expect_allow": list(case.get("allow") or []),
        "expect_require": list(case.get("require") or []),
        "web": bool(case.get("web")),
        "answer": "",
        "latency_ms": None,
        "elapsed_ms": None,
        "error": None,
        "sources_count": 0,
        "source_doc_ids": [],
        "tool_calls": [],
        "tools_called": [],
        "forced_rounds": [],
        "tool_ok": 0,
        "tool_fail": 0,
        "tool_notes": [],
        "context_chars": 0,
        "context_tool_chars": 0,
        "empty_evidence": False,
        "routing_hit": None,
        "routing_skipped": None,
        "metrics": {m: {"value": None, "coverage": None, "n": 0, "error": None} for m in OUR_METRICS},
    }

    skipped = _skip_reason(case)
    if skipped:
        record["routing_skipped"] = skipped
        return record

    wrapper = RecordingLLM(llm)
    parts: list[str] = []
    sources: list[Any] = []
    started = time.perf_counter()

    try:
        async for etype, data in run_agent_stream(
            wrapper,
            retriever,
            question=case["question"],
            session_id=None,
            history=[],
            top_k=opts.top_k,
            mode=opts.mode,
            rerank=opts.rerank,
            use_llm=True,
            enable_web=bool(case.get("web")),
        ):
            if etype == "sources":
                sources = data.get("sources") or sources
            elif etype == "delta":
                parts.append(data.get("content", ""))
            elif etype == "answer_reset":
                # 模型调工具前先吐的那句「我先查一下」会作废，别把它算进答案
                parts = []
            elif etype == "tool_result":
                if data.get("ok"):
                    record["tool_ok"] += 1
                else:
                    record["tool_fail"] += 1
                    note = (data.get("error") or "").strip()
                    if note:
                        record["tool_notes"].append(note.splitlines()[0][:120])
            elif etype == "done":
                # ⚠ 延迟取自 `done.total_ms`（服务端自己量的 Agent 全程），
                #   **不是**包在外面的墙钟 —— 那会把后面三次裁判调用也算进去，
                #   出来一个「平均 40 秒」的假数，而报告上写着「平均延迟」。
                got = data.get("total_ms")
                if isinstance(got, (int, float)):
                    record["latency_ms"] = int(got)
            elif etype == "error":
                record["error"] = str(data.get("message") or data.get("error") or "未知错误")[:200]
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"{type(exc).__name__}: {exc}"[:200]

    record["answer"] = "".join(parts)
    record["answer_chars"] = len(record["answer"])
    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    if record["latency_ms"] is None:
        record["latency_ms"] = record["elapsed_ms"]

    record["sources_count"] = len(sources)
    record["source_doc_ids"] = [getattr(s, "doc_id", "") for s in sources]

    # ---- 工具路由 ----
    calls = _tool_calls_of(wrapper.rounds)
    record["tool_calls"] = calls
    record["tools_called"] = _tool_names_of(wrapper.rounds)
    record["forced_rounds"] = sorted({
        r["call_id_prefix"] for r in wrapper.rounds if r["tool_choice"] is not None
    })

    hit = grade_routing(case, record["tools_called"])
    record["routing_hit"] = hit
    if hit is None:
        record["routing_skipped"] = "期望的工具未配置"

    # ---- 证据 ----
    evidence = collect_evidence(wrapper.rounds, sources, mode=opts.faith_context)
    record["evidence"] = evidence        # 供缓存比对；落盘时剔掉（见 _strip）
    record["context_chars"] = len(judge_context(evidence, opts.faith_context))
    record["context_tool_chars"] = evidence["tool_chars"]
    # ⚠ 判据是**结构**（sources 与工具正文双双为空）而非匹配话术 —— 与
    #   `E.summarize` 同一条纪律。只有两边都空才排除出均值：模型那句「暂未找到依据」
    #   是唯一陈述、又恰好被空上下文「支持」，faithfulness 会算成 1.0 满分。
    record["empty_evidence"] = not sources and not evidence["tool_chunk_count"]

    if opts.no_llm or not record["rag"]:
        # ⚠ `rag: false` 的题**路由照判**（上面已经判完），只是不算三项指标 ——
        #   指标在这类题上没有定义，见 `load_cases` 里那段注释。
        return record

    context_text = judge_context(evidence, opts.faith_context)

    async def one(name: str):
        try:
            if name == E.M_FAITH:
                return name, await E.metric_faithfulness(
                    llm, record["answer"], context_text, thinking=opts.judge_thinking)
            if name == E.M_RELEV:
                return name, await E.metric_answer_relevancy(
                    llm, case["question"], record["answer"], thinking=opts.judge_thinking)
            if name == E.M_RECALL:
                if not record["reference"].strip():
                    return name, {"value": None, "coverage": None, "n": 0,
                                  "error": "该题没有参考答案，跳过"}
                return name, await E.metric_context_recall(
                    llm, record["reference"], context_text, thinking=opts.judge_thinking)
        except Exception as exc:  # noqa: BLE001
            return name, {"value": None, "coverage": None, "n": 0,
                          "error": f"{type(exc).__name__}: {exc}"[:150]}
        return name, {"value": None, "coverage": None, "n": 0, "error": "未知指标"}

    for name, cell in await asyncio.gather(*(one(m) for m in OUR_METRICS)):
        record["metrics"][name] = cell
    return record


# ============================================================================
# 汇总与两个综合分
# ============================================================================


def _in_rag_pool(record: dict[str, Any]) -> bool:
    """这题进不进三项 RAG 指标的均值。**两个排除条件，理由完全不同，别合并**：

    1. `empty_evidence` —— Agent 真的什么都没拿到（sources 与工具返回双双为空）。
       不排除的话模型那句「暂未找到依据」是唯一陈述、又恰好被空上下文「支持」，
       faithfulness 会算成 **1.0 满分**（同 `eval_rag.summarize()` 那条）。
    2. `rag == False` —— **指标在这题上没有定义**（寒暄负例）。不排除的话
       faithfulness 恒为 **0.00**，给综合评分**系统性压分**。
       ⚠ 与第 1 条方向相反、性质相同：都是「指标在那儿没意义」，都得拿掉。
    """
    return not record.get("empty_evidence") and record.get("rag", True)


def _metric_mean(records: list[dict[str, Any]], name: str) -> float | None:
    values = [
        (r["metrics"].get(name) or {}).get("value")
        for r in records if _in_rag_pool(r)
    ]
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.fmean(values), 4) if values else None


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """三项均值 → 综合评分 → Agent 评分。

    ⚠ 只在**有效值**上算均值；某个指标整列为空（比如所有题的 reference 都是空串）
    时**把它从分子分母里一起拿掉并点名** —— 少算一项却仍按三项报出去，正是本仓库
    反复记的那类「看起来完全合理的错误答案」。
    """
    scored = [r for r in records if _in_rag_pool(r)]
    routing_only = [r for r in records if not r.get("rag", True)]
    means = {m: _metric_mean(records, m) for m in OUR_METRICS}
    dropped = [m for m, v in means.items() if v is None]
    used = [v for v in means.values() if v is not None]
    rag_score = round(statistics.fmean(used), 4) if used else None

    graded = [r for r in records if r["routing_hit"] is not None]
    ok = sum(1 for r in graded if r["routing_hit"])
    skipped = [r for r in records if r.get("routing_skipped")]
    tool_acc = round(ok / len(graded), 4) if graded else None

    agent_score = None
    if rag_score is not None and tool_acc is not None:
        agent_score = round(RAG_WEIGHT * rag_score + TOOL_WEIGHT * tool_acc, 4)

    lat = [r["latency_ms"] for r in records if isinstance(r.get("latency_ms"), (int, float))]
    latency = {
        "mean": round(statistics.fmean(lat)) if lat else None,
        "min": min(lat) if lat else None,
        "max": max(lat) if lat else None,
    }

    return {
        "counts": {
            "total": len(records),
            "scored": len(scored),
            "empty_evidence": sum(1 for r in records if r.get("empty_evidence")),
            "routing_only": len(routing_only),
            "error": sum(1 for r in records if r.get("error")),
            "routing_graded": len(graded),
            "routing_hit": ok,
            "routing_skipped": len(skipped),
        },
        "means": means,
        "metrics_dropped": dropped,
        "rag_score": rag_score,
        "tool_accuracy": tool_acc,
        "agent_score": agent_score,
        "latency": latency,
        "records": records,
    }


# ============================================================================
# 报告
# ============================================================================


def _dwidth(text: str) -> int:
    """终端显示宽度：CJK 算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _line(label: str, value: str) -> str:
    """`  标签<pad>: 值` —— 冒号按**显示宽度**对齐到第 16 列。

    ⚠ 用户给的示例里 `测试用例` 那行比其余行多一个空格、冒号反而左一格，那是手打的。
    这里一律对齐 —— 冒号列不齐看起来像脚本有 bug。
    ⚠ 填充是 `max(0, …)` 不是 `max(1, …)`：宽度**正好 14** 的标签（`工具选择准确率`）
    在 `max(1,…)` 下会多顶一格，冒号比所有别的行右移一列。
    """
    return f"  {label}{' ' * max(0, 14 - _dwidth(label))}: {value}"


def _secs(ms: int | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f}s"


def print_report(summary: dict[str, Any], opts: argparse.Namespace) -> None:
    counts, means, lat = summary["counts"], summary["means"], summary["latency"]

    print("=" * 60)
    print("  Agent 综合评估报告")
    print("=" * 60)

    print(_line("测试用例", str(counts["total"])))

    if counts["routing_graded"]:
        n, total = counts["routing_hit"], counts["routing_graded"]
        tail = f"（另有 {counts['routing_skipped']} 题因工具未配置跳过）" if counts["routing_skipped"] else ""
        print(_line("工具选择准确率", f"{n}/{total} ({n / total:.0%}){tail}"))
    else:
        print(_line("工具选择准确率", f"—（{counts['routing_skipped']} 题全部跳过）"))

    for name in OUR_METRICS:
        value = means[name]
        print(_line(METRIC_LABELS[name], "—" if value is None else f"{value:.2f}"))

    print(_line("平均延迟", f"{_secs(lat['mean'])} (min {_secs(lat['min'])}, max {_secs(lat['max'])})"))

    rag_score = summary["rag_score"]
    print(_line("综合评分", "—" if rag_score is None else f"{rag_score:.2f} / 1.00"))
    print(_line("Agent评分", "—" if summary["agent_score"] is None
                else f"{summary['agent_score']:.2f} (RAG权重{RAG_WEIGHT} + 工具权重{TOOL_WEIGHT})"))

    # ---- 口径与前提（没有这几行，上面的数字会被误读）----
    print()
    print(f"口径: faithfulness/recall 判据 = {opts.faith_context}"
          + ("（sources + 工具返回；与 eval_rag 的数字不可直接比）" if opts.faith_context == "evidence"
             else "（只有 sources；与 eval_rag 的既有数字可比，但工具类答案会被系统性低估）"))
    # ⚠ **联网是逐题的**（用例里的 `web` 字段）—— 不能写成 `opts.web`，没有那个属性。
    #   逐题开关的理由：全局开则另外九题的证据被网页污染、延迟也被抬高；全局关则
    #   `search_web` 压根不在 `tools_for()` 里，ag-08 必然失败。
    web_cases = sum(1 for r in summary["records"] if r.get("web"))
    print(f"链路: agent（四工具全挂）  模式={opts.mode}  top_k={opts.top_k}  "
          f"重排={'开' if opts.rerank else '关'}  联网=逐题（{web_cases}/{counts['total']} 题开）")
    print(f"工具可用: {', '.join(sorted(_configured_tools())) or '（无）'}")
    if summary.get("tools_down"):
        print(f"⚠⚠ 这些工具配了但**连不上**：{'、'.join(_tlabel(m) for m in summary['tools_down'])}")
        print(f"   ⇒ 工具选择准确率**照样有效**（模型确实调了），但依赖它们的题**证据是空的**，")
        print(f"     三项 RAG 指标会被拖低 —— 那是环境问题，不是 Agent 的锅。")
    if summary["metrics_dropped"]:
        names = "、".join(METRIC_LABELS[m] for m in summary["metrics_dropped"])
        print(f"⚠ 这些指标整列没有有效值，**已从综合评分里拿掉**：{names}")
    if counts["empty_evidence"]:
        print(f"⚠ 空证据（sources 与工具返回双双为空）: {counts['empty_evidence']} 题 —— "
              f"已排除出三项均值（否则模型那句「暂未找到依据」会被算成满分）")
    if counts.get("routing_only"):
        print(f"⚠ 只测路由、不进三项均值: {counts['routing_only']} 题（用例里标了 `rag:false`）—— "
              f"指标在那类题上没有定义（寒暄没有可问之题），混进去是给综合评分压分")
    if counts["error"]:
        print(f"⚠ 生成失败: {counts['error']} 题")

    # ---- 逐题明细（只给均值无法定位到题）----
    print()
    print("-" * 60)
    print("逐题明细：")
    for r in summary["records"]:
        if r.get("routing_skipped"):
            print(f"  [跳过] {r['id']}  {r['question'][:38]}  —— {r['routing_skipped']}")
            continue
        called = "、".join(_tlabel(t) for t in r["tools_called"]) or "未调工具"
        mark = "✓" if r["routing_hit"] else "✗"
        exp = "、".join(_tlabel(t) for t in (r["expect_require"] or r["expect_allow"])) or "不许调"
        cells = " ".join(
            f"{METRIC_LABELS[m][:4]}={v:.2f}" if isinstance(
                (v := (r["metrics"].get(m) or {}).get("value")), (int, float)) else f"{METRIC_LABELS[m][:4]}=—"
            for m in OUR_METRICS
        )
        forced = f"  强制轮={','.join(r['forced_rounds'])}" if r["forced_rounds"] else ""
        if not r.get("rag", True):
            cells += "  （只测路由）"
        print(f"  {mark} {r['id']}  {_secs(r['latency_ms']):>7}  期望[{exp}] 实调[{called}]  {cells}{forced}")
        if r["error"]:
            print(f"        ⚠ {r['error']}")
        if r["tool_notes"]:
            print(f"        ⚠ 工具失败: {'; '.join(r['tool_notes'])}")

    guessed = [r for r in summary["records"] if r["routing_hit"] and not r["tools_called"]]
    if guessed:
        print()
        # ⚠⚠ 这句不是注解，是**读数字的前提**：那几题的「命中」是**空过**的。
        #   不写出来，100% 会被读成「模型每次都正确地选对了工具」，而实际上一半的题
        #   压根没调工具 —— 那是 allow 语义允许的**正确**行为（预检索是无条件的），
        #   但它确实把工具准确率抬高了。要让它有区分力，用例就得偏向
        #   **没有东西替它们预跑**的那三类（图谱 / 采购库 / 联网）。
        print(f"⚠ {len(guessed)} 题的『命中』是**未调用任何工具**（空过）—— 预检索/预联网已经够用，"
              f"这是 allow 语义允许的正确行为，但它**抬高了工具准确率**。")
        print(f"   ⇒ 想让它有区分力，用例要偏向图谱/采购库/联网那三类（没有东西替它们预跑）。"
              f"见脚本 docstring 里 allow 的语义。")

    # ⚠ 第 1 题背着全部冷启动成本（向量/重排模型加载、连接建立），而默认 --concurrency 1
    #   意味着它一定排在第一个。min/max 拉开到十倍以上多半就是这个，不是题目难。
    if len(summary["records"]) > 1 and lat["max"] and lat["min"] and lat["max"] > 8 * lat["min"]:
        print(f"⚠ 延迟极差很大（{_secs(lat['min'])} ~ {_secs(lat['max'])}）：第一题通常背着冷启动"
              f"（模型加载/建连），**别把它当成题目难度**。")


# ============================================================================
# --selftest
# ============================================================================


class _StubInner:
    """替身真客户端（鸭子类型，**不继承** BaseLLMClient —— 同仓库里的既有做法）。"""

    ready = True
    _NOT_CONFIGURED = "未配置 STUB_API_KEY，本次回答未经大模型生成。"

    def __init__(self) -> None:
        self.seen: list[list[dict[str, Any]]] = []

    async def _gen(self, messages):
        yield ("content", "第一片")
        yield ("content", "第二片")

    def stream_with_tools(self, messages, **kwargs):
        self.seen.append(messages)
        return self._gen(messages)


def selftest() -> int:
    E._pass = 0
    E._fail = 0

    def ck(label: str, ok: bool, detail: str = "") -> None:
        E.check(label, ok, detail)

    # ① ⚠⚠ 最高价值的一条：快照而非引用
    inner = _StubInner()
    rec = RecordingLLM(inner)
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    rec.stream_with_tools(msgs, tools=None)
    msgs.append({"role": "assistant", "content": "后来才 append 的"})
    snap = rec.rounds[0]["messages"]
    ck("① 包装器存的是**快照**而不是引用（存引用会让每轮都记成同一份终态）",
       len(snap) == 1 and snap[0]["content"] == "hi", f"快照长度={len(snap)}")

    # ② __getattr__ 转发
    ck("② __getattr__ 转发 ready / _NOT_CONFIGURED 到真客户端",
       rec.ready is True and rec._NOT_CONFIGURED.startswith("未配置 STUB"))
    try:
        rec.根本不存在的属性  # noqa: B018
        raised = False
    except AttributeError:
        raised = True
    except RecursionError:
        raised = False
    ck("②b 未知属性抛 AttributeError（不是 RecursionError）", raised)

    # ③ 返回异步迭代器且不需要 await
    import inspect
    inner3 = _StubInner()
    rec3 = RecordingLLM(inner3)
    got = rec3.stream_with_tools([{"role": "user", "content": "x"}])
    ck("③ stream_with_tools 返回异步迭代器（普通 def，调用即记录）", inspect.isasyncgen(got))

    # ④ deepcopy 不递归
    try:
        copy.deepcopy(rec3)
        ck("④ copy.deepcopy(包装器) 不触发 __getattr__ 无限递归", True)
    except RecursionError:
        ck("④ copy.deepcopy(包装器) 不触发 __getattr__ 无限递归", False, "RecursionError")

    # ⑤ 工具名抠取
    rounds = [
        {"call_id_prefix": "r1", "tool_choice": None, "messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "search_knowledge_graph", "arguments": '{"query_type":"top_suppliers"}'}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "【知识图谱查询】\n1. 甲 59 笔"},
        ]},
        {"call_id_prefix": "r2", "tool_choice": "search_bidding_knowledge", "messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "c2", "function": {"name": "search_bidding_knowledge", "arguments": '{"query":"x"}'}},
            ]},
        ]},
    ]
    names = _tool_names_of(rounds)
    ck("⑤ 抠出全部工具名（含强制轮）",
       names == ["search_knowledge_graph", "search_bidding_knowledge"], str(names))
    calls = _tool_calls_of(rounds)
    ck("⑤b 能识别强制轮（tool_choice 非空 —— SSE 不携带这个信息）",
       [c["forced"] for c in calls] == [False, True])
    ck("⑤c 原始参数被记下来（SSE 的 tool_call.query 只是展示串）",
       '"top_suppliers"' in calls[0]["args"])
    ck("⑤d 空轮次抠出空集合", _tool_names_of([]) == [])

    # ⑥ 证据拼装
    class _S:
        answer = "知识库答条"

    ev = collect_evidence(rounds, [_S()])
    ck("⑥ evidence 含工具返回正文（图谱那行）", "甲 59 笔" in ev["evidence"])
    ck("⑥b evidence 含 sources 答条", "知识库答条" in ev["evidence"])
    ck("⑥c sources 口径**不含**工具返回", "甲 59 笔" not in ev["sources"])
    ck("⑥d 检索工具的 tool 正文被跳过（与 sources 逐字重复，白烧裁判预算）",
       ev["evidence"].count("知识库答条") == 1, f"出现 {ev['evidence'].count('知识库答条')} 次")
    ck("⑥e judge_context 按开关分派",
       "甲 59 笔" in judge_context(ev, "evidence") and "甲 59 笔" not in judge_context(ev, "sources"))

    long_rounds = [{"call_id_prefix": "r1", "tool_choice": None, "messages": [
        {"role": "assistant", "tool_calls": [{"id": "z", "function": {"name": "search_procurement_records", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "z", "content": "字" * (TOOL_CONTEXT_CHARS + 500)},
    ]}]
    ev2 = collect_evidence(long_rounds, [])
    ck("⑥f 超长工具正文被截断并带标记",
       "…（已截断）" in ev2["evidence"] and len(ev2["evidence"]) < TOOL_CONTEXT_CHARS + 200)

    # ⑦ 路由判分
    ck("⑦ require ⊆ called ⊆ allow 命中",
       grade_routing({"allow": ["search_knowledge_graph"], "require": ["search_knowledge_graph"]},
                     ["search_knowledge_graph"]) is True)
    ck("⑦b 调了 allow 之外的工具有 → 不命中（多调一个别的工具是真错）",
       grade_routing({"allow": ["search_bidding_knowledge"], "require": []},
                     ["search_bidding_knowledge", "search_web"]) is False)
    ck("⑦c kb 类题「不调工具」算命中（预检索是无条件的，不是工具调用）",
       grade_routing({"allow": ["search_bidding_knowledge"], "require": []}, []) is True)
    ck("⑦d allow 为空（负例）时调任何工具都不命中",
       grade_routing({"allow": [], "require": []}, ["search_bidding_knowledge"]) is False
       and grade_routing({"allow": [], "require": []}, []) is True)
    ck("⑦e require 没满足 → 不命中（不调工具答不出统计题）",
       grade_routing({"allow": ["search_procurement_records"], "require": ["search_procurement_records"]}, []) is False)

    # ⑧⚠⚠ 两个公式：用**用户给的那组示例数字逐位钉死**。
    #   这是「报告口径」唯一的凭证 —— 公式被顺手改掉（比如改成加权平均、
    #   或把工具权重也塞进综合评分）会当场变红，而不是等到数字看着不对劲才有人怀疑。
    ck("⑧ 综合评分 = 三项**等权**均值（示例：0.31/0.41/0.23 → 0.32）",
       round(statistics.fmean([0.31, 0.41, 0.23]), 2) == 0.32)
    ck("⑧b Agent评分 = 0.6×综合 + 0.4×工具（示例：0.32/1.00 → 0.59）",
       round(RAG_WEIGHT * 0.32 + TOOL_WEIGHT * 1.0, 2) == 0.59)
    ck("⑧c 权重常数没被改", (RAG_WEIGHT, TOOL_WEIGHT) == (0.6, 0.4))
    ck("⑧d 三项指标里没有 context_precision（用户给定的报告只有三项）",
       E.M_PRECISION not in OUR_METRICS and len(OUR_METRICS) == 3)

    # ⑨ 整列无有效值时，从综合评分里拿掉并点名
    recs = [
        {"empty_evidence": False, "routing_hit": True, "latency_ms": 100,
         "metrics": {E.M_FAITH: {"value": 0.5}, E.M_RELEV: {"value": 0.5}, E.M_RECALL: {"value": None}}},
    ]
    agg = aggregate(recs)
    ck("⑨ recall 整列空 → 从综合评分拿掉并点名（0.5，不是 (0.5+0.5+0)/3）",
       agg["rag_score"] == 0.5 and agg["metrics_dropped"] == [E.M_RECALL], str(agg["rag_score"]))
    ck("⑨b 空证据的题被排除出均值",
       aggregate([dict(recs[0], empty_evidence=True)])["rag_score"] is None)

    # ⑨c `rag:false` 的题**不进 RAG 均值、但照算路由** —— 这是「方向相反、性质相同」的那条
    cheer = {"empty_evidence": False, "rag": False, "routing_hit": True, "latency_ms": 100,
             "metrics": {E.M_FAITH: {"value": 0.0}, E.M_RELEV: {"value": 0.49},
                         E.M_RECALL: {"value": None}}}
    rag_case = {"empty_evidence": False, "rag": True, "routing_hit": True, "latency_ms": 100,
                "metrics": {E.M_FAITH: {"value": 1.0}, E.M_RELEV: {"value": 1.0},
                            E.M_RECALL: {"value": None}}}
    both = aggregate([cheer, rag_case])
    ck("⑨c 寒暄题的 faithfulness=0.00 不混进均值（1.00，不是 0.50）",
       both["rag_score"] == 1.0, str(both["rag_score"]))
    ck("⑨d 它照样算进工具准确率的分母", both["counts"]["routing_graded"] == 2)
    ck("⑨e 报告里点名题数", both["counts"]["routing_only"] == 1)

    # ⑩ 空证据判据是结构（sources 与工具正文双双为空）
    ev3 = collect_evidence([], [])
    ck("⑩ 无 sources 且无工具返回 → 空证据", not ev3["evidence"].strip())

    # ⑪ 用例文件：schema 与工具名
    try:
        cases = load_cases(_DEFAULT_CASES)
    except SystemExit as exc:
        ck("⑪ 用例文件可读", False, str(exc))
        cases = []
    known = set(_configured_tools()) | {"search_web"}
    ck("⑪ 用例文件可读且非空", len(cases) >= 10, f"{len(cases)} 条")
    bad = [c["id"] for c in cases
           for t in list(c["allow"]) + list(c["require"]) if t not in known]
    ck("⑪b 用例里的工具名都是真实存在的", not bad, f"可疑: {bad}")
    ck("⑪c id 不重复", len({c["id"] for c in cases}) == len(cases))
    ck("⑪d 至少有一条负例（allow 为空）", any(not c["allow"] for c in cases))
    ck("⑪e 至少有一条 web=true（否则 search_web 压根不在工具表里）",
       any(c["web"] for c in cases))

    # ⑫ 报告格式：用户给的那 8 行标签必须逐行出现
    fake = {
        "counts": {"total": 10, "routing_graded": 10, "routing_hit": 10, "routing_skipped": 0,
                   "empty_evidence": 0, "error": 0},
        "means": {E.M_FAITH: 0.31, E.M_RELEV: 0.41, E.M_RECALL: 0.23},
        "metrics_dropped": [], "rag_score": 0.32, "tool_accuracy": 1.0, "agent_score": 0.59,
        "latency": {"mean": 16500, "min": 3300, "max": 37700}, "records": [],
    }
    buf = []
    with contextlib.redirect_stdout(_ListSink(buf)):
        print_report(fake, argparse.Namespace(faith_context="evidence", mode="hybrid", top_k=5,
                                              rerank=False, web=True))
    out = "".join(buf)
    for want in ["Agent 综合评估报告", "测试用例", "工具选择准确率", "Faithfulness",
                 "Relevancy", "Recall", "平均延迟", "综合评分", "Agent评分"]:
        ck(f"⑫ 报告里有「{want}」", want in out)
    ck("⑫b 数值按用户示例渲染", "10/10 (100%)" in out and "0.31" in out
       and "16.5s (min 3.3s, max 37.7s)" in out and "0.32 / 1.00" in out
       and "0.59 (RAG权重0.6 + 工具权重0.4)" in out)

    # ⑫c 冒号对齐：八个标签行的 `:` 必须落在同一列。
    #   最容易跑偏的是宽度**正好 14** 的「工具选择准确率」（它在 `max(1,…)` 下会右移一格）。
    cols = {_dwidth(ln.split(":")[0]) for ln in out.splitlines()
            if ln.startswith("  ") and ": " in ln and not ln.lstrip().startswith(("⇒", "口径", "链路", "工具可用"))}
    ck("⑫c 报告里所有标签的冒号在同一列", len(cols) == 1 and cols == {16}, f"列位置={sorted(cols)}")

    # ⑬ eval_rag 复用面真的可用（这条只能在运行期验）
    ck("⑬ eval_rag 的三个指标函数可 import 且可调用",
       all(callable(getattr(E, n, None))
           for n in ("metric_faithfulness", "metric_answer_relevancy", "metric_context_recall")))
    ck("⑬b OUR_METRICS 与 eval_rag 的指标名一致（拼错会静默拿不到分）",
       set(OUR_METRICS) <= set(E.ALL_METRICS))

    print()
    print(f"{'=' * 60}")
    print(f"  selftest: {E._pass} 通过, {E._fail} 失败")
    print(f"{'=' * 60}")
    return 1 if E._fail else 0


class _ListSink:
    def __init__(self, buf: list[str]) -> None:
        self.buf = buf

    def write(self, text: str) -> int:
        self.buf.append(text)
        return len(text)

    def flush(self) -> None:
        return


# ============================================================================
# 预检与主流程
# ============================================================================


def check_tools() -> int:
    """只读探问，不建连接、不发模型请求。跑之前先看这个。"""
    from bidding_qa import graph_store, pg_store, web_search
    from bidding_qa.tools import rag_tools

    print("四个工具的可用性（不发任何模型请求）：")
    for name in (rag_tools.TOOL_SEARCH, rag_tools.TOOL_GRAPH, rag_tools.TOOL_PG, rag_tools.TOOL_WEB):
        mark = "✓" if name in _configured_tools() else "✗"
        print(f"  {mark} {name:30} {_tlabel(name)}")

    print()
    for label, status in (("图谱", graph_store.graph_status),
                          ("采购库", pg_store.pg_status),
                          ("联网", web_search.web_status)):
        try:
            print(f"  {label}: {status()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label}: 状态查询失败 {type(exc).__name__}: {exc}")

    # ⚠ **真的连一次**再把状态重报一遍 —— 上面那三个都是「还没试过」的懒状态
    print("\n真连一次（warmup，两个 store 都吞异常）：")
    for label, st in _warm_and_report().items():
        mark = "✓" if st.get("available") else "✗"
        print(f"  {mark} {label}: {st.get('detail')}")

    try:
        from bidding_qa.retriever import get_retriever
        get_retriever()
        print("\n✓ 本地 Qdrant 可用（后端没在跑）")
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 本地 Qdrant 不可用：{type(exc).__name__}: {exc}")
        print("  后端多半正在跑（本地 Qdrant 是排他锁）。**先停掉后端再跑评估。**")
    return 0


def _precheck_capabilities(cases: list[dict[str, Any]]) -> list[str]:
    """跑之前把「这次哪些工具不可用」说清楚 —— 别让人把环境问题误读成模型不行。

    返回**真连不上**的工具名列表（配了、但 warmup 失败）。⚠ 与「没配置」分开：
    没配置的题跳过（见 `grade_routing` 返回 None），配了而连不上的**照跑但会判错** ——
    这一点必须在这里明说，否则报告出来的低分会被当成 Agent 的锅。
    """
    configured = _configured_tools()
    missing = [n for n in ("search_knowledge_graph", "search_procurement_records", "search_web")
               if n not in configured]
    if missing:
        print(f"⚠ 这些工具本次**没配置**：{'、'.join(_tlabel(m) for m in missing)}")
        skip = [c["id"] for c in cases if set(c["require"] or []) & set(missing)]
        if skip:
            print(f"  ⇒ 这些用例会被**跳过**（不计入工具准确率的分母）：{', '.join(skip)}")
        loose = [c["id"] for c in cases
                 if not (set(c["require"] or []) & set(missing))
                 and (set(c["allow"] or []) & set(missing))]
        if loose:
            print(f"  ⇒ 这些用例仍会跑，但期望的工具不在场、路由多半判错：{', '.join(loose)}")

    # ⚠ 只热身**配了的**；联网没有 warmup（CLAUDE.md 明写别给它补）。
    from bidding_qa import graph_store, pg_store

    down: list[str] = []
    if "search_knowledge_graph" in configured:
        graph_store.warmup()
        if not graph_store.graph_status().get("available"):
            down.append("search_knowledge_graph")
    if "search_procurement_records" in configured:
        pg_store.warmup()
        if not pg_store.pg_status().get("available"):
            down.append("search_procurement_records")

    if down:
        print(f"⚠⚠ 这些工具**配了但真连不上**：{'、'.join(_tlabel(m) for m in down)}")
        # ⚠⚠ 注意两者的**失效方式不同**，别写成一句笼统的「必然判错」：
        #   · **路由分照样有效** —— 模型仍然会去调它（那是模型的判断），只是工具返回 ok=False；
        #   · 坏的是**证据**：工具结果是空的，答案只能靠模型自己编或干脆说查不到，
        #     三项 RAG 指标被拖低。所以这是**环境问题**，别记到 Agent 头上。
        print("   ⇒ 工具选择准确率**照样有效**（模型确实调了），但依赖它们的题**证据是空的**，")
        print("     三项 RAG 指标会被拖低 —— 那是环境问题，不是 Agent 的锅。")
        for c in cases:
            if set(c["allow"] or []) & set(down):
                print(f"   ⚠ 受影响: {c['id']} {c['question'][:30]}")

    return down


async def run(opts: argparse.Namespace) -> int:
    cases = load_cases(Path(opts.cases))
    if opts.limit:
        cases = cases[: opts.limit]

    print(f"用例: {len(cases)} 条（{opts.cases}）")
    tools_down = _precheck_capabilities(cases)

    if opts.check_tools:
        return check_tools()

    # ⚠ 本地 Qdrant 排他锁 —— 后端在跑就直说，别静默降级成 HTTP。
    #   `run_agent_stream` 需要**真的 retriever 对象**（它内部自己发起检索），
    #   `POST /api/search` 给不了，所以 agent 链路**没有** HTTP 这条路。
    if opts.retrieval == "http":
        print("✗ Agent 链路不支持 --retrieval http：run_agent_stream 需要真的 retriever 对象。")
        print("  请停掉后端，用 --retrieval live。")
        return 1
    from bidding_qa.retriever import get_retriever

    try:
        retriever = get_retriever()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 本地检索不可用：{type(exc).__name__}: {exc}")
        print("  后端多半正在跑（本地 Qdrant 是排他锁）。**先停掉后端再跑。**")
        return 1

    from bidding_qa.llm import get_llm

    llm = get_llm(opts.llm_provider or None)
    if not llm.ready:
        print(f"✗ 文本大模型不可用（{type(llm).__name__}）—— 没有它就量不出指标。")
        return 1

    if not opts.no_llm:
        # ⚠ 裁判调用要走 eval_rag 的并发闸门（`_gate()` / `_embed_lock()` 读的就是这两个
        #   模块全局）。不设的话本脚本的裁判调用**完全绕过 `--llm-concurrency`**，
        #   10 题 × 3 指标会一股脑打出去撞限流 —— 而 429 的表现（指标静默变 null）
        #   看起来像模型不老实。
        E._LLM_GATE = asyncio.Semaphore(max(1, opts.llm_concurrency))
        E._EMBED_LOCK = asyncio.Lock()
        print(f"裁判: {type(llm).__name__}  并发 {opts.concurrency} 题 / {opts.llm_concurrency} 请求")
        print(f"口径: faithfulness/recall 判据 = {opts.faith_context}")

    records: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(max(1, opts.concurrency))

    async def guarded(case):
        async with sem:
            return await run_case(llm, retriever, case, opts)

    # ⚠ `--concurrency` 默认 1：`run_agent_stream` 内部直接 `asyncio.to_thread(retriever.search)`
    #   （`agent.py:255`），**不经过** `LocalRetrieval.search` 那把串行化锁 ——
    #   并发跑 Agent 等于多线程同时打嵌入式 Qdrant，不是有文档保证的用法。
    started = time.perf_counter()
    for i, record in enumerate(await asyncio.gather(*(guarded(c) for c in cases)), 1):
        records.append(record)
        tag = "跳过" if record.get("routing_skipped") else ("✓" if record["routing_hit"] else "✗")
        print(f"  [{i}/{len(cases)}] {tag} {record['id']}  {_secs(record['latency_ms'])}")

    summary = aggregate(records)
    summary["elapsed_s"] = round(time.perf_counter() - started, 1)
    summary["tools_down"] = tools_down
    print()
    print_report(summary, opts)

    if opts.out:
        out = Path(opts.out)
        out.mkdir(parents=True, exist_ok=True)
        stem = opts.out_name or f"agent-{opts.mode}-fc{opts.faith_context}"
        path = out / f"{stem}.summary.json"
        path.write_text(
            json.dumps({k: v for k, v in summary.items() if k != "records"},
                       ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        per_case = out / f"{stem}.jsonl"
        with per_case.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps({k: v for k, v in r.items() if k != "evidence"},
                                    ensure_ascii=False, default=str) + "\n")
        print(f"\n产物: {path}")
        print(f"      {per_case}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent 综合评估（四个工具全挂）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--cases", default=str(_DEFAULT_CASES), help="用例 jsonl 路径")
    parser.add_argument("--selftest", action="store_true", help="离线断言，不需要任何服务/Key")
    parser.add_argument("--check-tools", action="store_true", help="只报四个工具的可用性")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument("--mode", default="hybrid", choices=["hybrid", "vector", "bm25"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--faith-context", choices=["evidence", "sources"], default="evidence",
                        help="faithfulness/recall 的判据（默认 evidence = sources + 工具返回）")
    parser.add_argument("--concurrency", type=int, default=1, help="并发题数（默认 1，见 docstring）")
    parser.add_argument("--llm-concurrency", type=int, default=8, help="裁判并发请求数")
    parser.add_argument("--judge-thinking", action="store_true", help="裁判开启思维链")
    parser.add_argument("--llm-provider", default="", help="文本供应商（空 = 服务端默认）")
    parser.add_argument("--retrieval", default="live", choices=["live", "auto", "http"])
    parser.add_argument("--no-llm", action="store_true", help="只跑 Agent 链路，不算指标（零裁判成本）")
    parser.add_argument("--out", default="", help="产物目录（如 data/eval）")
    parser.add_argument("--out-name", default="", help="产物文件名前缀")
    opts = parser.parse_args()

    if opts.selftest:
        return selftest()
    return asyncio.run(run(opts))


if __name__ == "__main__":
    raise SystemExit(main())
