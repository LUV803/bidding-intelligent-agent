"""端到端验证：模型把工具调用漏进正文时，系统能捞回来真正执行。

**两组用例，都不需要 Qdrant/BM25**（预检索用桩，恒返回空）：

- **用例 A：普通工具轮泄漏**。复刻线上那次的真东西（正文里带标记、tool_calls 为空），
  其余全部是真的：真 graph_store、真 Neo4j、真 execute_tool 分派、真 SSE 事件序列。
  所以这一组**需要 Neo4j 起着**。
- **用例 B：收尾轮泄漏**（2026-09-30 加）。前 `agent_max_rounds` 轮正经调工具把轮数烧完，
  收尾轮再把调用漏进正文。泄漏的是 `search_bidding_knowledge`，配桩检索器就能跑完 ——
  这一组**不需要任何外部服务**，测的是 `agent.py` 的**收口逻辑**。

用例 B 为什么必须有两个子用例：模型在漏标记之前**可能已经吐过一句**「我再查一下」，
也可能一个字都没吐。两种形状在修好之前落进**不同的**坏分支——
有前导正文时那句声明会被当成正式回答端出去；没有时 `content` 为空、直接掉进
`fallback_answer()`，用户拿到的是一份**来自知识库、却回答着另一个问题**的原文
（2026-09-30 真机就是这一种）。两种都要钉。

消费事件时模拟了前端 useChat.ts 的 delta 追加 / answer_reset 清空语义——
直接拼接 delta 会把已被作废的前导正文也算进来，那个断言就永远过不了。

跑法：

    uv run python scripts/probe_recover_e2e.py
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

from bidding_qa.agent import run_agent_stream  # noqa: E402
from bidding_qa.clients import ToolCall  # noqa: E402
from bidding_qa.config import settings  # noqa: E402
from bidding_qa.llm import LLMEvent  # noqa: E402
from bidding_qa.tools import TOOL_SEARCH  # noqa: E402

BAR = chr(0xFF5C)
M = BAR * 2 + "DSML" + BAR * 2  # 标记 token

ANNOUNCE = "再为您补充搜索政府机关、学校等其他类型的采购人。"

# 用户报的那次泄漏原文
# ⚠ 名字用**改名前的旧名** `query_knowledge_graph`（同 probe_recover.py 用例 1）：
#   当年这里写的是 `search_knowledge_graph` —— 模型把两个工具名揉出来的幻觉名；
#   2026-09-20 改名后它**成了真名**，留着就会静默改走「名字优先」路径，
#   这条 e2e 就不再覆盖「名字对不上 → 参数投票 → 恢复」了。
LEAKED = (
    f"</{M}tool_calls> <{M}invoke name=\"query_knowledge_graph\"> "
    f"<{M}parameter name=\"keyword\" string=\"true\">管理局</{M}parameter> "
    f"<{M}parameter name=\"query_type\" string=\"true\">search_entity</{M}parameter> "
    f"</{M}invoke> "
    f"<{M}invoke name=\"query_knowledge_graph\"> "
    f"<{M}parameter name=\"keyword\" string=\"true\">中学</{M}parameter> "
    f"<{M}parameter name=\"query_type\" string=\"true\">search_entity</{M}parameter> "
    f"</{M}invoke> </{M}tool_calls>"
)

FINAL_TEXT = "图谱里登记有采购记录的采购人类型包括国家机关、事业单位等。"

# —— 用例 B ——

# 收尾轮的泄漏原文：名字与参数名都是**真的**（与用例 A 刻意相反，那边要验投票路径）。
# 这一组要验的是「收口逻辑看不看 leaked_markup」，归属判定越简单越好 —— 让它走
# `_schema_by_name` 名字精确命中那条路，被别的东西绊住就说明测的不是这件事。
RECOVER_QUERY = "投标保证金比例上限"
LEAKED_FINAL = (
    f"</{M}tool_calls> <{M}invoke name=\"{TOOL_SEARCH}\"> "
    f"<{M}parameter name=\"query\" string=\"true\">{RECOVER_QUERY}</{M}parameter> "
    f"</{M}invoke> </{M}tool_calls>"
)

ANNOUNCE_FINAL = "我再换个说法查一下。"
FINAL_TEXT_B = "投标保证金的上限比例在知识库里没有直接条款，建议以招标文件为准。"


class FakeLLM:
    """第一轮复刻泄漏，第二轮正常作答。"""

    def __init__(self) -> None:
        self.round = 0

    @property
    def ready(self) -> bool:
        return True

    async def stream_with_tools(self, messages, *, tools=None, tool_choice=None, call_id_prefix="", **kw):
        self.round += 1
        if self.round == 1:
            yield LLMEvent(type="content", text=ANNOUNCE)
            yield LLMEvent(
                type="message",
                content=ANNOUNCE,
                tool_calls=[],
                leaked_markup=LEAKED,
                finish_reason="stop",
            )
        else:
            # 把收到的 role=tool 消息回显出来，确认工具真的执行过
            tools_seen = [m for m in messages if m.get("role") == "tool"]
            assert tools_seen, "第二轮没收到 role=tool 消息——工具根本没执行"
            yield LLMEvent(type="content", text=FINAL_TEXT)
            yield LLMEvent(type="message", content=FINAL_TEXT, tool_calls=[], finish_reason="stop")


class StubRetriever:
    """预检索恒为空——图谱类问题在法规知识库里本来就检索不到。"""

    last_latency: dict = {}

    def search(self, query, top_k, mode, rerank):  # noqa: ANN001
        return []


class FakeLLMFinalLeak:
    """前 `agent_max_rounds` 轮正经调工具把轮数烧完，收尾轮才把调用漏进正文。

    ⚠ 每一轮的查询词都不同：同词同 top_k 会撞上 `KnowledgeBaseTool.run` 里的
      `duplicate_query` 去重，拿到的是「该调用已执行过」而不是一次真执行。
    """

    def __init__(self, *, announce: bool) -> None:
        self.round = 0
        #: 漏标记之前先说一句「我再查一下」——真机两种形状都出现过
        self.announce = announce

    @property
    def ready(self) -> bool:
        return True

    async def stream_with_tools(self, messages, *, tools=None, tool_choice=None, call_id_prefix="", **kw):
        self.round += 1
        if self.round <= settings.agent_max_rounds:
            # 正经的工具轮：内容为空、tool_calls 走对通道
            yield LLMEvent(
                type="message",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call_r{self.round}_0",
                        name=TOOL_SEARCH,
                        arguments=json.dumps(
                            {"query": f"占位检索词{self.round}"}, ensure_ascii=False
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
            return
        if self.round == settings.agent_max_rounds + 1:
            # 收尾轮：把调用漏成正文标记（`_MarkupSuppressor` 会吞掉它，content 只剩前导那句）
            if self.announce:
                yield LLMEvent(type="content", text=ANNOUNCE_FINAL)
            yield LLMEvent(
                type="message",
                content=ANNOUNCE_FINAL if self.announce else "",
                tool_calls=[],
                leaked_markup=LEAKED_FINAL,
                finish_reason="stop",
            )
            return
        # 第二轮收尾：恢复出来的调用必须**真的执行过**，否则这里断言就炸
        # ⚠ 断言写在假 LLM 里是刻意的：它在**生产路径上**跑（`_stream_round` 直接调它），
        #   比事后从事件序列里反推更贴近「下一轮模型到底看到了什么」。
        tools_seen = [m for m in messages if m.get("role") == "tool"]
        assert tools_seen, "第二轮收尾没收到 role=tool 消息——恢复出来的调用根本没执行"
        assert any(
            RECOVER_QUERY in str(m.get("content", "")) for m in tools_seen
        ), "恢复出来的那条 role=tool 消息里没有标记中的查询词"
        yield LLMEvent(type="content", text=FINAL_TEXT_B)
        yield LLMEvent(type="message", content=FINAL_TEXT_B, tool_calls=[], finish_reason="stop")


async def _collect(question: str, llm) -> list[tuple[str, dict]]:  # noqa: ANN001
    events: list[tuple[str, dict]] = []
    async for ev in run_agent_stream(
        llm,  # type: ignore[arg-type]
        StubRetriever(),  # type: ignore[arg-type]
        question=question,
        session_id=None,
        history=[],
        top_k=None,
        mode="hybrid",
        rerank=False,
    ):
        events.append(ev)
    return events


def _frontend_text(events: list[tuple[str, dict]]) -> str:
    """模拟前端 useChat.ts:172-179 的消费方式：delta 追加、answer_reset 清空。

    不能把 delta 直接拼起来了事——那样会把已被作废的前导正文也算进来。
    """
    text = ""
    for kind, data in events:
        if kind == "delta":
            text += data.get("content", "")
        elif kind == "answer_reset":
            text = ""
    return text


def _make_check(failures: list[str]):
    def check(label: str, got, want) -> None:
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failures.append(f"{label}\n   got ={got!r}\n   want={want!r}")

    return check


async def case_a(failures: list[str]) -> None:
    print("=" * 72)
    print("用例 A：普通工具轮泄漏（需要 Neo4j）")
    print("=" * 72)
    check = _make_check(failures)

    events = await _collect("采购企业有哪些？", FakeLLM())

    kinds = [k for k, _ in events]
    print("事件序列:", " → ".join(kinds), "\n")

    tool_calls = [d for k, d in events if k == "tool_call"]
    tool_results = [d for k, d in events if k == "tool_result"]
    deltas = _frontend_text(events)

    check("工具真的被调用了", len(tool_calls), 2)
    check(
        "两次都路由到 search_knowledge_graph",
        [c["name"] for c in tool_calls],
        ["search_knowledge_graph"] * 2,
    )
    check(
        "entity 落到了图谱工具该收的字段上",
        [c["query"] for c in tool_calls],
        ["实体名查找（拿不准登记名称时先探一下）：管理局", "实体名查找（拿不准登记名称时先探一下）：中学"],
    )
    check("两次都执行成功", [r["ok"] for r in tool_results], [True, True])
    check("带回了结构化图谱结果", [r["graph"] is not None for r in tool_results], [True, True])
    check(
        "图谱结果里有真实行",
        all((r["graph"] or {}).get("rows") is not None for r in tool_results),
        True,
    )
    check("声明正文被作废（answer_reset）", "answer_reset" in kinds, True)
    check("最终回答是模型第二次的正文", deltas.strip(), FINAL_TEXT)
    check("正文里不含标记 token", "DSML" in deltas, False)
    check("正文里不含那次声明", ANNOUNCE in deltas, False)

    print("\n各次图谱查询命中：")
    for r in tool_results:
        g = r["graph"]
        rows = g.get("rows") or []
        print(f"  {g['query_type']:14s} entity={g['entity']!r} 命中={len(rows)} total={g['total']} "
              f"resolved={g.get('resolved')!r}")
        for row in rows[:3]:
            print(f"      - {row['label']}")
    print()


async def case_b(failures: list[str], *, announce: bool) -> None:
    shape = "漏标记前先说了一句「我再查一下」" if announce else "漏标记前一个字都没吐（真机那次）"
    print("=" * 72)
    print(f"用例 B：收尾轮泄漏 —— {shape}（不需要任何外部服务）")
    print("=" * 72)
    check = _make_check(failures)

    events = await _collect("投标保证金比例上限是多少？", FakeLLMFinalLeak(announce=announce))

    kinds = [k for k, _ in events]
    print("事件序列:", " → ".join(kinds), "\n")

    tool_calls = [d for k, d in events if k == "tool_call"]
    tool_results = [d for k, d in events if k == "tool_result"]
    deltas = _frontend_text(events)
    final_round = settings.agent_max_rounds + 1

    # 前 5 轮 + 收尾轮恢复出来的那 1 次
    check("工具调用次数 = 轮数上限 + 1（收尾轮那次是恢复出来的）", len(tool_calls), final_round)
    check("全部走 search_bidding_knowledge", {c["name"] for c in tool_calls}, {TOOL_SEARCH})
    check(
        "恢复出来的那次带上了标记里的查询词",
        RECOVER_QUERY in (tool_calls[-1]["query"] if tool_calls else ""),
        True,
    )
    check("它的 round 标的是收尾轮（不是 0、也不是最后一轮工具轮）", tool_calls[-1]["round"], final_round)
    check("恢复出来的调用真的执行了", len(tool_results), final_round)
    # ⚠ 这一条是**整个用例的支点**：修好之前，两种形状都会给出一个不是模型答案的东西
    #   （有前导正文时端出那句「我再换个说法查一下」，没有时端出 fallback 的知识库原文），
    #   而 `deltas` 的断言两种都接得住。
    check("最终回答是模型第二轮收尾的正文", deltas.strip(), FINAL_TEXT_B)
    check("正文里不含标记 token", "DSML" in deltas, False)
    check("正文里不含那句前导声明", ANNOUNCE_FINAL in deltas, False)
    check("前导正文被作废（answer_reset）", ("answer_reset" in kinds), announce)
    if not announce:
        # 没有任何前导正文可作废时**不该**多发一个 answer_reset（会白清一次前端正文）
        check("没有前导正文时不发作废事件", "answer_reset" in kinds, False)
    print()


async def main() -> int:
    failures: list[str] = []

    await case_a(failures)
    await case_b(failures, announce=True)
    await case_b(failures, announce=False)

    if failures:
        print(f"{len(failures)} 项失败：")
        for f in failures:
            print(" -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
