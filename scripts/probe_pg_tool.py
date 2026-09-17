"""验证模型会不会在正确的时机选 search_procurement_records。

用户需求第 2 条就是「当问题适合用数据库里的数据回答时，Agent 会自动调用数据库工具」，
这条靠提示词和工具描述保证，但**写得再好也得真机问一遍**——模型不照做就是零分。

关键点：**用的不是副本，是 agent.py 里真实的 TOOL_SCHEMAS / execute_tool / 提示词**。
知识库检索用一个空桩顶替（「无命中」分支正是数据库问题在真实链路里的起点，
74 条法规问答对里必然检索不到采购记录），因此这个脚本**不需要 Qdrant / BM25 / embedding**，
只要 PostgreSQL 和 API Key。**也不需要 Neo4j**（图谱没配就不挂那个工具，用例会自动跳过）。

最要紧的一组是**反向用例**：法规问题（「履约保证金最高限额」）绝不能调数据库工具，
「某单位采购过什么」也绝不能既查图谱又查数据库（两个口径不同的数字混着说必然对不上）。

跑法：

    uv run python scripts/probe_pg_tool.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import graph_store, pg_store  # noqa: E402
from bidding_qa.agent import (  # noqa: E402
    TOOL_GRAPH,
    TOOL_NAMES,
    TOOL_PG,
    TOOL_SEARCH,
    TOOL_SCHEMAS,
    SourceRegistry,
    ToolContext,
    _assistant_tool_message,
    _peek_display,
    execute_tool,
)
from bidding_qa.config import settings  # noqa: E402
from bidding_qa.llm import get_llm  # noqa: E402
from bidding_qa.rag import AGENT_NO_RESULT_PROMPT, AGENT_SYSTEM_PROMPT  # noqa: E402


class StubRetriever:
    """空检索桩。

    采购记录类问题在 74 条法规问答对里本来就检索不到，返回空才是**真实**情形，
    也正好逼模型走「检索无命中 → 按路由调工具」这条真实路径。
    """

    last_latency: dict = {}

    def search(self, query, top_k, mode, rerank):  # noqa: ANN001, ARG002
        StubRetriever.last_latency = {"total_ms": 1.0}
        return []


async def one_round(llm, messages: list[dict], round_no: int):
    terminal = None
    async for ev in llm.stream_with_tools(
        messages, tools=TOOL_SCHEMAS, call_id_prefix=f"r{round_no}"
    ):
        if ev.type == "message":
            terminal = ev
    return terminal


async def ask(
    llm,
    question: str,
    *,
    label: str,
    expect: str,
    forbid: tuple[str, ...] = (),
    max_rounds: int = 3,
) -> bool:
    print("\n" + "=" * 68)
    print(label)
    print(f"问题：{question}")
    print("=" * 68)

    ctx = ToolContext(
        registry=SourceRegistry(cap=settings.agent_max_sources),
        retriever=StubRetriever(),
        mode="hybrid",
        rerank=False,
    )
    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": AGENT_NO_RESULT_PROMPT.format(question=question)},
    ]

    called: list[str] = []
    final = None
    for round_no in range(1, max_rounds + 1):
        result = await one_round(llm, messages, round_no)
        if result is None or not result.tool_calls:
            final = result
            break
        messages.append(_assistant_tool_message(result, echo_reasoning=True))
        for call in result.tool_calls:
            display = _peek_display(call.name, call.arguments)
            outcome = await execute_tool(call, ctx)
            called.append(call.name)
            print(f"  [第{round_no}轮] {call.name}")
            print(f"           「{display}」")
            print(f"           → ok={outcome.ok} {outcome.elapsed_ms:.0f}ms")
            if outcome.pg:
                r = outcome.pg
                print(f"           → 数据库: {r.query_label} mode={r.mode} 命中 {len(r.rows)}/{r.total}")
                if r.note:
                    print(f"           → note: {r.note[:120]}")
            if outcome.graph:
                g = outcome.graph
                print(f"           → 图谱: {g.query_label} resolved={g.resolved!r} {len(g.rows)}/{g.total}")
            if not outcome.ok:
                print(f"           → error: {outcome.error}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": outcome.text})

    if final is None:
        print("  （轮数用尽，走强制收尾）")
        result = None
        async for ev in llm.stream_with_tools(
            messages, tools=TOOL_SCHEMAS, tool_choice="none", call_id_prefix="final"
        ):
            if ev.type == "message":
                result = ev
        final = result

    answer = (final.content or "").strip() if final else ""
    print(f"\n  最终回答（{len(answer)} 字）：")
    print("  " + (answer[:700].replace("\n", "\n  ") if answer else "（空）"))

    hit = expect in called
    violated = [t for t in forbid if t in called]
    print(f"\n  → 调用序列 {called or ['（未调用任何工具）']}")
    print(f"  → 期望包含 {expect}：{'✓ 符合' if hit else '✗ 不符'}")
    if forbid:
        print(f"  → 不该出现 {list(forbid)}：{'✓ 符合' if not violated else f'✗ 出现了 {violated}'}")
    return hit and not violated


async def main() -> int:
    llm = get_llm()
    if not llm.ready:
        print("✗ 未配置 DEEPSEEK_API_KEY，无法验证")
        return 1
    if not pg_store.ENABLED:
        print("✗ 未配置 POSTGRES_HOST，数据库工具没挂出去，无法验证")
        return 1

    pg_store.warmup()
    print(f"模型: {llm.model}")
    print(f"工具: {list(TOOL_NAMES)}")
    print(f"PostgreSQL: {pg_store.pg_status()}")
    if not pg_store.pg_status()["available"]:
        print("✗ 数据库连不上，无法验证")
        return 1

    cases = [
        # ① 带条件的筛选 —— 数据库的主场
        #
        # ⚠⚠ **这一条会偶发不通过，且不是回归**（2026-09-20 实测 5 次：3 次图谱 / 2 次数据库）。
        # 它落在 CLAUDE.md 写明的**重叠区**：「某单位采购过什么」图谱与数据库**都能答**——
        # 图谱按实体名精确匹配，数据库按 `contains` 跨 标题/项目名称/标的物 的**或**匹配。
        # 提示词里「一个问题不要两个工具都调」+「实体/名单类归图谱」把这条推给了图谱，
        # 但问句里的「采购过哪些项目」又确实是个带条件的筛选。模型在两者间摇摆。
        # ⚠ 别为了让它变绿去改提示词路由：那会把「实体/名单归图谱」这条更重要的约定一起动摇，
        #   而 `is_enum_question` 那条强制首轮兜底正是靠它成立的（实测这条问句**不**触发放行）。
        # 判断 ① 是否真的坏了，看的是它**长期**是否明显偏离 50/50，不是单次结果。
        (
            "① 单位名筛选（⚠ 重叠区，偶发）",
            "中国科学技术大学采购过哪些项目？",
            dict(expect=TOOL_PG),
        ),
        # ② 分组统计 —— 必须用 group_by，不是翻明细自己数
        (
            "② 分组统计",
            "哪个城市的采购项目最多？",
            dict(expect=TOOL_PG),
        ),
        # ③ 精确计数 + 金额合计
        (
            "③ 计数与合计",
            "学校类的采购项目有多少条？中标金额合计是多少？",
            dict(expect=TOOL_PG),
        ),
        # ④ 金额排行 —— 会撞上那笔 66 亿，回答里必须带存疑说明
        (
            "④ 金额排行（会撞上 66 亿异常记录）",
            "中标金额最高的采购项目是哪个？",
            dict(expect=TOOL_PG),
        ),
        # ⑤ 反向：法规问题绝不能调数据库工具
        (
            "⑤ 法规问题（不该调数据库）",
            "履约保证金最高限额是多少？",
            dict(expect=TOOL_SEARCH, forbid=(TOOL_PG,)),
        ),
    ]

    if graph_store.ENABLED:
        # ⑥ 反向：实体/名单类问题归图谱；顺带验「一个问题不要两个工具都调」
        cases.append(
            (
                "⑥ 名单类问题（归图谱，不该同时查数据库）",
                "有哪些采购企业？",
                dict(expect=TOOL_GRAPH, forbid=(TOOL_PG,)),
            )
        )
    else:
        print("\n（Neo4j 未启用，跳过图谱相关的两条用例）")

    results = []
    for label, question, kwargs in cases:
        results.append(await ask(llm, question, label=label, **kwargs))

    print("\n" + "=" * 68)
    print("结论")
    print("=" * 68)
    for (label, _q, _k), ok in zip(cases, results, strict=True):
        print(f"  {'✓' if ok else '✗'} {label}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
