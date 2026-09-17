"""验证模型会不会在正确的时机选 search_knowledge_graph。

这个功能成不成，全看模型拿到两个工具后能不能按问题性质分流：
问「激光打印机有哪些供应商」要挑图谱，问「评标委员会怎么组成」要挑知识库。
工具描述和提示词写得再好，模型不照做就是零分——所以必须真机问一遍。

关键点：**用的不是副本，是 agent.py 里真实的 TOOL_SCHEMAS / execute_tool / 提示词**。
知识库检索用一个空桩顶替（图谱问题本来就在知识库里检索不到），
因此这个脚本不需要 Qdrant / BM25 / embedding，只要 Neo4j 和 API Key。

跑法：

    uv run python scripts/probe_graph_tool.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa import graph_store  # noqa: E402
from bidding_qa.agent import (  # noqa: E402
    TOOL_GRAPH,
    TOOL_NAMES,
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
from bidding_qa.schemas import Source  # noqa: E402


class StubRetriever:
    """空检索桩。

    图谱类问题在法规知识库里本来就检索不到东西，返回空才是**真实**情形；
    法规类问题这里也给空，逼模型自己调 search_bidding_knowledge 换词重试
    （我们只看它选哪个工具，不看检索质量）。
    """

    last_latency: dict = {}

    def search(self, query, top_k, mode, rerank):  # noqa: ANN001, ARG002
        StubRetriever.last_latency = {"total_ms": 1.0}
        return []


async def one_round(llm, messages: list[dict], round_no: int):
    """跑一轮，返回终态 LLMEvent。"""
    terminal = None
    async for ev in llm.stream_with_tools(
        messages, tools=TOOL_SCHEMAS, call_id_prefix=f"r{round_no}"
    ):
        if ev.type == "message":
            terminal = ev
    return terminal


async def ask(llm, question: str, *, label: str, expect: str, max_rounds: int = 2) -> bool:
    """跑完整的多轮循环，返回「是否选了预期工具」。"""
    print("\n" + "=" * 68)
    print(f"{label}")
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
            print(f"  [第{round_no}轮] {call.name}  「{display}」")
            print(f"           → ok={outcome.ok} {outcome.elapsed_ms:.0f}ms")
            if outcome.graph:
                g = outcome.graph
                print(
                    f"           → 图谱: {g.query_label} resolved={g.resolved!r} "
                    f"命中 {len(g.rows)}/{g.total} 条"
                )
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
    print("  " + (answer[:600].replace("\n", "\n  ") if answer else "（空）"))

    hit = expect in called
    print(f"\n  → 调用序列 {called or ['（未调用任何工具）']}")
    print(f"  → 期望包含 {expect}：{'✓ 符合' if hit else '✗ 不符'}")
    return hit


async def main() -> int:
    llm = get_llm()
    if not llm.ready:
        print("✗ 未配置 DEEPSEEK_API_KEY，无法验证")
        return 1
    if not graph_store.ENABLED:
        print("✗ 未配置 NEO4J_URI，图谱工具没挂出去，无法验证")
        return 1

    graph_store.warmup()
    print(f"模型: {llm.model}")
    print(f"工具: {list(TOOL_NAMES)}")
    print(f"Neo4j: {graph_store.graph_status()}")

    # 覆盖需求第 2 条的两面：该调图谱的会调，该调检索的仍然走检索
    results = [
        await ask(
            llm,
            "激光打印机的中标供应商有哪些？金额最高的是谁？",
            label="① 图谱问题（明确实体）",
            expect=TOOL_GRAPH,
        ),
        await ask(
            llm,
            "打印机这个品类的中标情况怎么样？哪个地区采购得最多？",
            label="② 图谱问题（模糊关键词，需要先定位）",
            expect=TOOL_GRAPH,
        ),
        await ask(
            llm,
            "评标委员会的专家人数和构成有什么要求？",
            label="③ 法规问题（不应调图谱）",
            expect="search_bidding_knowledge",
        ),
    ]

    print("\n" + "=" * 68)
    print("结论")
    print("=" * 68)
    names = ["① 图谱问题（明确实体）", "② 图谱问题（模糊关键词）", "③ 法规问题"]
    for name, ok in zip(names, results, strict=True):
        print(f"  {'✓' if ok else '✗'} {name}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
