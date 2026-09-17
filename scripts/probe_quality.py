"""验证「工具结果质量检查」：空手而归时给模型一条显式的下一步。

`tools/base.assess_outcome()` 在把 `role:"tool"` 消息塞进对话**之前**跑一次，
不达标就**追加**一段提示（点名本轮其他工具 + 允许如实说没查到）。

**不需要任何服务**（Qdrant / Neo4j / PostgreSQL / API Key 全都不用）：单元段只构造
`ToolOutcome`，端到端段用替身 LLM + 空检索桩打真实的 `run_agent_stream()`。

⚠⚠ 最要紧的一组是**「非空路径逐字节不变」**：本机制是**追加**，一个字符都不替换。
`_render_result` 的产出被 `probe_web_search.py` ⑥ 按字面量钉着，这里要是写成替换，
那边会红 —— 但那条红会指向「web 的字面量变了」，**指向不到真正的原因**。所以这里
自己再钉一遍，让它在最靠近故障点的地方失败。

跑法：

    uv run python scripts/probe_quality.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import agent  # noqa: E402
from bidding_qa.schemas import GraphResult, PgResult, Source  # noqa: E402
from bidding_qa.tools import (  # noqa: E402
    TOOL_EXA,
    TOOL_GRAPH,
    TOOL_PG,
    TOOL_SEARCH,
    TOOL_SKILL,
    TOOL_WEB,
    ToolOutcome,
    assess_outcome,
    get_tool,
)

QUALITY_TAG = "【结果质量】"
failures: list[str] = []
count = 0


def banner(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def check(label: str, got: object, want: object, detail: str = "") -> None:
    global count
    count += 1
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok or detail:
        extra = f"  [{detail}]" if detail else ""
        print(f"      got={got!r} want={want!r}{extra}")
    if not ok:
        failures.append(label)


def _source(index: int = 1) -> Source:
    return Source(index=index, doc_id=f"doc-{index}", question="q", answer="a", snippet="s", score=0.9)


def _pg(total: int, *, ok: bool = True, note: str = "") -> ToolOutcome:
    return ToolOutcome("x", ok=ok, pg=PgResult(query_label="查询", ok=ok, total=total, note=note))


def _graph(total: int, *, ok: bool = True, note: str = "") -> ToolOutcome:
    return ToolOutcome(
        "x",
        ok=ok,
        graph=GraphResult(query_type="e_related", query_label="查询", ok=ok, total=total, note=note),
    )


ALL3 = (TOOL_SEARCH, TOOL_GRAPH, TOOL_PG)


# ================================================================ ① 判据：结构性
def probe_structural() -> None:
    banner("① 判据一律是结构性的（行数 / 条数），不碰相似度分数")

    check("检索命中 → 不加提示", assess_outcome(TOOL_SEARCH, ToolOutcome("t", ok=True, new_sources=[_source()]), ALL3), None)
    check("检索空手 → 加提示", assess_outcome(TOOL_SEARCH, ToolOutcome("t", ok=True), ALL3) is not None, True)
    check("检索命中的全在【资料】里（known）→ 不加", assess_outcome(TOOL_SEARCH, ToolOutcome("t", ok=True, known_indices=[1]), ALL3), None)

    check("数据库 total=3 → 不加", assess_outcome(TOOL_PG, _pg(3), ALL3), None)
    check("数据库 total=0 → 加", assess_outcome(TOOL_PG, _pg(0), ALL3) is not None, True)

    check("图谱 total=5 → 不加", assess_outcome(TOOL_GRAPH, _graph(5), ALL3), None)
    check("图谱 total=0 → 加", assess_outcome(TOOL_GRAPH, _graph(0), ALL3) is not None, True)

    # ⚠ 这一条是「判据取 total 而不是 len(rows)」的**唯一**凭证：rows 被 graph_max_rows
    #   截断不代表没命中。反过来（total>0 而 rows 空）是真实存在的形状。
    truncated = ToolOutcome(
        "t", ok=True, graph=GraphResult(query_type="e_related", query_label="查询", ok=True, total=8751)
    )
    assert not truncated.graph.rows, "前提：本节构造的正是「total>0 但 rows 为空」"
    check("图谱 total=8751 而 rows 被截断成空 → 不加", assess_outcome(TOOL_GRAPH, truncated, ALL3), None)

    check("联网空手 → 加", assess_outcome(TOOL_WEB, ToolOutcome("t", ok=True), (TOOL_SEARCH, TOOL_WEB)) is not None, True)


# ================================================================ ② 该闭嘴的时候
def probe_silent() -> None:
    banner("② 参数写错时不该插话（各自的文案已带修正线索）")

    for err in ("unknown_tool", "bad_arguments", "bad_query_type", "empty_query", "duplicate_query"):
        got = assess_outcome(TOOL_SEARCH, ToolOutcome("t", ok=False, error=err), ALL3)
        check(f"error={err} → 不加提示", got, None, "它的文案里已经写了「可用工具/可用类型」")

    # 没注册的工具名：工具都查不到，无从判空
    check("工具名不认识 → 不加提示", assess_outcome("no_such_tool", ToolOutcome("t", ok=True), ALL3), None)


# ================================================================ ③ 真失败要说
def probe_failure() -> None:
    banner("③ 真失败（连不上 / handler 崩）必须说 —— 那才是该换工具的时候")

    timeout = assess_outcome(TOOL_PG, _pg(0, ok=False, note="连接超时"), ALL3)
    check("数据库连不上 → 加提示", timeout is not None, True)
    check("　　提示里带上 note 原文", "连接超时" in (timeout or ""), True)
    check("　　措辞是「失败」而非「没有结果」", "本次调用失败" in (timeout or ""), True)

    crashed = assess_outcome(TOOL_PG, ToolOutcome("t", ok=False, error="RuntimeError: boom"), ALL3)
    check("handler 崩了（structured 结果都没构造出来）→ 加提示", crashed is not None, True)

    gfail = assess_outcome(TOOL_GRAPH, _graph(0, ok=False, note="Neo4j 不可达"), ALL3)
    check("图谱查失败 → 加提示", gfail is not None and "Neo4j 不可达" in gfail, True)


# ================================================================ ④ 提示的内容
def probe_content() -> None:
    banner("④ 提示内容：只点名**本轮真挂着**的工具，且不含被调的那个")

    text = assess_outcome(TOOL_PG, _pg(0), ALL3) or ""
    check("点名了另外两个工具", TOOL_SEARCH in text and TOOL_GRAPH in text, True)
    check("不含被调工具自己", TOOL_PG not in text, True)
    check("含「如实告诉用户没有查到」", "没有查到" in text, True)
    check("带头部标记", text.startswith(QUALITY_TAG.rsplit("】", 1)[0] + "】"), True)

    # ⚠ 本轮没挂出去的工具**绝不能出现**。开着联网时进程级的 TOOL_SCHEMAS 里不含
    #   search_web，但 `available` 传的是本轮那批；反过来，联网关着时提 search_web
    #   就是让模型去调一个不存在的工具（CLAUDE.md 点名的自相矛盾）。
    only_search = assess_outcome(TOOL_SEARCH, ToolOutcome("t", ok=True), (TOOL_SEARCH,)) or ""
    check("只剩检索工具时不提「改调」", "请改调" in only_search, False)
    check("但仍允许如实说没查到", "没有查到" in only_search, True)
    check("绝不提本轮没挂的 search_web", TOOL_WEB in only_search, False)

    no_search_web = assess_outcome(TOOL_PG, _pg(0), (TOOL_PG, TOOL_GRAPH)) or ""
    check("本轮没挂检索工具时也不提它", TOOL_SEARCH in no_search_web, False)
    check("只提挂了的那个", TOOL_GRAPH in no_search_web, True)

    # 每个工具都得有 route_label，否则提示里只剩一个光秃秃的工具名。
    # ⚠ `registered_tools()` 回的是 **list**（不是 dict），别写成下标访问 —— 那样
    #   会在下面这一行抛 TypeError，把整节探针炸掉。
    # ⚠ 这里逐个列名**而不是遍历注册表**：注册表里多一个还没写完的工具（缺 route_label）
    #   会让探针变红，而那不是「结果质量提示坏了」—— 列名的话，新工具忘了配 route_label
    #   时**只会漏测**。取舍是：新工具落地时**要记得往这行加名字**（加 Exa 时就漏过）。
    for name in (TOOL_SEARCH, TOOL_GRAPH, TOOL_PG, TOOL_WEB, TOOL_EXA, TOOL_SKILL):
        tool = get_tool(name)
        check(f"{name} 配了 route_label", bool(tool and tool.route_label.strip()), True)


# ================================================================ ⑤ 端到端接线
def probe_wiring() -> None:
    banner("⑤ 端到端：run_agent_stream 真的把它接在 role=\"tool\" 消息后面（只追加）")

    QUESTION = "履约保证金最高限额是多少？"

    class _Retriever:
        """按脚本逐次应答的检索桩。

        ⚠ **必须能按次给不同的结果**：`run_agent_stream` 会先**预检索**一次
        （结果直接登记进 registry），之后工具内部才再检索一次。两次用同一份
        `hits` 的话，工具那条路拿到的永远是「与已提供资料重复」而不是新增 ——
        测出来的就不是「有新增」那条路径了。
        """

        last_latency: dict = {}
        script: list = []
        calls = 0

        def search(self, query, top_k, mode, rerank):  # noqa: ANN001, ARG002
            _Retriever.last_latency = {"total_ms": 1.0}
            idx = min(_Retriever.calls, len(_Retriever.script) - 1)
            _Retriever.calls += 1
            return list(_Retriever.script[idx])

    class _FakeLLM:
        """第一轮调检索工具，第二轮把收到的 role="tool" 原文抠出来。"""

        def __init__(self) -> None:
            self.round = 0
            self.tool_messages: list[str] = []

        @property
        def ready(self) -> bool:
            return True

        async def stream_with_tools(self, messages, *, tools=None, tool_choice=None, **kw):  # noqa: ANN001, ARG002
            self.round += 1
            if self.round == 1:
                from bidding_qa.llm import LLMEvent, ToolCall

                yield LLMEvent(
                    type="message",
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(id="c1", name=TOOL_SEARCH, arguments=json.dumps({"query": "履约保证金"}))
                    ],
                )
            else:
                self.tool_messages += [
                    str(m.get("content", "")) for m in messages if m.get("role") == "tool"
                ]
                from bidding_qa.llm import LLMEvent

                yield LLMEvent(type="content", text="好的。")
                yield LLMEvent(type="message", content="好的。", tool_calls=[], finish_reason="stop")

    def drive() -> str:
        llm = _FakeLLM()

        async def _go():
            async for _ in agent.run_agent_stream(
                llm,  # type: ignore[arg-type]
                _Retriever(),  # type: ignore[arg-type]
                question=QUESTION,
                session_id=None,
                history=[],
                top_k=None,
                mode="hybrid",
                rerank=False,
            ):
                pass

        asyncio.run(_go())
        assert llm.tool_messages, "模型没收到任何 role=\"tool\" 消息，本节的前提不成立"
        return llm.tool_messages[-1]

    def restage(script: list) -> None:
        _Retriever.script, _Retriever.calls = script, 0

    # ---- 有新增（预检索空 → 工具拿到 1 条）：逐字节等于 _render_result ----
    restage([[], [_source()]])
    fresh_text = drive()
    check("有新增：末条 tool 消息无质量提示", QUALITY_TAG in fresh_text, False)
    check(
        "有新增：逐字节等于 _render_result 的产出",
        fresh_text,
        agent._render_result("履约保证金", [_source()], [], 0),
        "本机制是**追加**；写成替换会让 probe_web_search ⑥ 的字面量断言变红",
    )

    # ---- 「命中但与已有资料重复」也是一条**非空**路径，同样不许插话 ----
    restage([[_source()]])
    known_text = drive()
    check("命中重复：无质量提示", QUALITY_TAG in known_text, False)
    check(
        "命中重复：逐字节等于 _render_result 的产出",
        known_text,
        agent._render_result("履约保证金", [], [_source()], 0),
    )

    # ---- 空：原来那段一个字不少，提示接在后面 ----
    restage([[]])
    empty_text = drive()
    check("空路径：接上了质量提示", QUALITY_TAG in empty_text, True)
    check(
        "空路径：提示是**接在**原文之后，不是替换",
        empty_text.startswith(agent._render_result("履约保证金", [], [], 0)),
        True,
    )


# ================================================================ ⑥ 撞顶
def probe_dropped() -> None:
    banner("⑥ 撞顶：**找到了**但来源额度满了 —— 不能当成「什么都没找到」")

    # 撞顶的形状与「真的一条没命中」**完全一样**（new_sources / known_indices 双双为空），
    # 唯一的区别是 `dropped`。少了这一节，这条路径没有任何东西钉着。
    from bidding_qa.tools import SourceRegistry, _render_result

    def src(i: int) -> Source:
        return Source(index=i, doc_id=f"d{i}", question="q", answer="a", snippet="s", score=0.9)

    reg = SourceRegistry(cap=12)
    reg.add([src(i) for i in range(1, 13)])
    fresh, known, dropped = reg.add([src(i) for i in range(20, 25)])
    assert not fresh and not known and dropped == 5, "前提：本节构造的正是撞顶形状"

    check("ToolOutcome 有 dropped 字段且默认为 0", ToolOutcome("x", ok=True).dropped, 0)

    full = ToolOutcome("x", ok=True, new_sources=fresh, known_indices=[], dropped=dropped)
    check("撞顶 → 不加质量提示", assess_outcome(TOOL_SEARCH, full, ALL3), None,
          "加了就会在「另有 N 条超出上限」的下一行说「没有拿到任何结果…如实说没有查到」")
    check("联网撞顶 → 也不加", assess_outcome(TOOL_WEB, full, (TOOL_SEARCH, TOOL_WEB)), None)

    # 对照：真的一条没命中**必须**照旧插话 —— 防「一刀切把 dropped 判据写成恒真」
    check("真没命中（dropped=0）→ 仍然加提示",
          assess_outcome(TOOL_SEARCH, ToolOutcome("x", ok=True, dropped=0), ALL3) is not None, True)

    kb = _render_result("履约保证金", fresh, known, dropped)
    check("撞顶文案：点明是额度满", "来源额度已满" in kb, True)
    check("撞顶文案：**不说**「没有新增资料」", "没有新增资料" in kb, False,
          "那句会被模型如实转述成「知识库中没有相关依据」")
    # ⚠ 不能拿裸子串「知识库中没有相关依据」判 —— 撞顶那段话里它**确实出现**，但是在
    #   「**不要断言**知识库中没有相关依据」里。要比的是**祈使句**那一整句，不是那个短语。
    check("撞顶文案：禁止模型诬赖知识库", "不要断言知识库中没有相关依据" in kb, True)
    check("撞顶文案：不含「请直接说明知识库中没有相关依据」那句祈使",
          "请直接说明知识库中没有相关依据" in kb, False)
    a = _render_result("履约保证金", [], [], 0)
    check("真没命中：逐字节等于冻结文案", a,
          "检索「履约保证金」没有新增资料。\n请基于已有资料作答；若仍无法回答，"
          "请直接说明知识库中没有相关依据，不要重复检索相同的词。",
          "dropped == 0 的那一支必须与改动前逐字相同")


# ================================================================ main
def main() -> int:
    probe_structural()
    probe_silent()
    probe_failure()
    probe_content()
    probe_wiring()
    probe_dropped()

    print("\n" + "=" * 68)
    if failures:
        print(f"结果：{count - len(failures)} 通过 / {len(failures)} 失败")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print(f"结果：{count} 通过 / 0 失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
