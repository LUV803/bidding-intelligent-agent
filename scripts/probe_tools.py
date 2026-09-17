"""最小验证：DeepSeek 流式工具调用 + reasoning_content 回传规则。

Agent 模式里唯一无法靠读代码确认的环节就是这条链路，所以先单独跑通再写业务代码。

验证四件事：
  1. stream_with_tools() 能把流式分片拼成完整的 tool_calls
  2. 带 tool_calls 的 assistant 消息**不回传** reasoning_content 是否真的 400
  3. 回传空字符串 reasoning_content 是否能通过（字段存在 vs 非空）
  4. 具名 tool_choice 强制调用某个工具，以及强制轮之后的消息该如何回传（⑤ / ⑤b）
     ——「有哪些采购企业」这类列举问题要确定性触发图谱查询，全靠这条链路

跑法：

    uv run python scripts/probe_tools.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidding_qa.config import settings  # noqa: E402
from bidding_qa.llm import LLMEvent, get_llm  # noqa: E402

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_bidding_knowledge",
        "description": "在招投标知识库中检索问答对。当已有资料不足以回答用户问题时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "改写后的检索关键词"},
            },
            "required": ["query"],
        },
    },
}

SYSTEM = "你是招投标领域的问答助手。资料不足时调用 search_bidding_knowledge 检索知识库。"
QUESTION = "履约保证金最高不得超过中标合同金额的多少？"


async def round_once(llm, messages: list[dict], **kwargs) -> LLMEvent:
    """跑一轮流式生成，打印增量，返回终态事件。"""
    terminal: LLMEvent | None = None
    async for ev in llm.stream_with_tools(messages, **kwargs):
        if ev.type == "reasoning" and kwargs.get("_show_reasoning", True):
            print(f"\033[90m[思考] {ev.text}\033[0m", end="", flush=True)
        elif ev.type == "content":
            print(ev.text, end="", flush=True)
        elif ev.type == "message":
            terminal = ev
    assert terminal is not None, "没有收到终态事件"
    return terminal


async def main() -> int:
    llm = get_llm()
    if not llm.ready:
        print("✗ 未配置 DEEPSEEK_API_KEY，无法验证")
        return 1

    print(f"模型: {llm.model}\n")

    # ⑤ / ⑤b 各有一条断言验的是 **DeepSeek 服务端特有的约束**，不是本项目代码的行为：
    #   ① 具名 tool_choice 与思考模式互斥（不关思考就 400）
    #   ② 带 tool_calls 的 assistant 消息丢了 reasoning_content，下一轮开思考就 400
    # **智谱 GLM 两条都不设**（2026-09-20 实测）——所以按实际供应商分叉。
    # 不分叉的话，「换个 LLM_PROVIDER 再跑一遍探针」这条验证步骤会稳定给出两条假 FAIL，
    # 把真正的问题淹掉。
    # ⚠ 判据取**实际客户端的类名**而不是 settings.llm_provider：工厂在拼错时会静默回退，
    #   那种情况下 settings 与实际用的是两回事，以实际为准。
    _is_deepseek = type(llm).__name__ == "DeepSeekLLM"

    # ---------- 1. 首轮：模型是否发起工具调用 ----------
    print("=" * 60)
    print("① 首轮（带 tools，tool_choice=auto）")
    print("=" * 60)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUESTION},
    ]
    try:
        first = await round_once(llm, messages, tools=[TOOL_SCHEMA], call_id_prefix="r1")
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 首轮请求失败: {type(exc).__name__}: {exc}")
        return 1

    print(f"\n\nfinish_reason = {first.finish_reason!r}")
    print(f"reasoning_seen = {first.reasoning_seen}  (reasoning 长度 {len(first.reasoning)})")
    print(f"content 长度   = {len(first.content)}")
    print(f"tool_calls     = {len(first.tool_calls)} 个")
    for call in first.tool_calls:
        print(f"  - id={call.id!r} name={call.name!r} args={call.arguments!r}")

    if not first.tool_calls:
        print("\n⚠ 模型没有发起工具调用，无法继续验证回传规则。")
        print("  （tool_choice=auto 下这是合法行为，可把问题改得更依赖知识库再试）")
        return 2

    # ---------- 2. 受控 A/B：reasoning_content 到底要不要回传 ----------
    print("\n" + "=" * 60)
    print("② 受控 A/B：带 tool_calls 的 assistant 消息回传规则")
    print("=" * 60)

    assistant_base = {
        "role": "assistant",
        "content": first.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in first.tool_calls
        ],
    }
    tool_msg = {
        "role": "tool",
        "tool_call_id": first.tool_calls[0].id,
        "content": "检索到 1 条资料：履约保证金不得超过中标合同金额的 10%。",
    }

    async def try_variant(label: str, assistant: dict) -> bool:
        """发一次非流式请求，看是否被拒。返回 True 表示成功。

        注意：请求与打印严格分开——否则打印出错会被误读成请求失败。
        """
        outcome: tuple[bool, str]
        try:
            await llm.client.chat.completions.create(
                model=llm.model,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": QUESTION},
                    assistant,
                    tool_msg,
                    {"role": "user", "content": "请据此回答。"},
                ],
                max_tokens=256,
                stream=False,
            )
            outcome = (True, "")
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).replace("\n", " ")
            outcome = (False, f"{type(exc).__name__}: {detail[:400]}")

        ok, detail = outcome
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {label}")
        if detail:
            print(f"         {detail}")
        return ok

    without = await try_variant("不回传 reasoning_content 字段", dict(assistant_base))
    with_empty = await try_variant(
        "回传 reasoning_content=''", {**assistant_base, "reasoning_content": ""}
    )
    with_text = await try_variant(
        "回传 reasoning_content=<实际内容>",
        {**assistant_base, "reasoning_content": first.reasoning},
    )
    with_null = await try_variant(
        "回传 reasoning_content=None", {**assistant_base, "reasoning_content": None}
    )

    # ---------- 3. 走 stream_with_tools 的完整第二轮 ----------
    print("\n" + "=" * 60)
    print("③ 第二轮（走 stream_with_tools，回传 reasoning_content）")
    print("=" * 60)
    second_messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUESTION},
        {**assistant_base, "reasoning_content": first.reasoning},
        tool_msg,
    ]
    try:
        second = await round_once(llm, second_messages, tools=[TOOL_SCHEMA], call_id_prefix="r2")
        print(f"\n\nfinish_reason = {second.finish_reason!r}")
        print(f"content 长度   = {len(second.content)}")
        print(f"tool_calls     = {len(second.tool_calls)} 个")
        second_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 第二轮失败: {type(exc).__name__}: {exc}")
        second_ok = False

    # ---------- 4. 压力测试：连续两轮工具调用，全程不回传 ----------
    # 上面②是单轮。若「必须回传」只在多轮场景下触发，单轮测试会漏掉。
    print("\n" + "=" * 60)
    print("④ 压力测试：连续两轮工具调用，全程不回传 reasoning_content")
    print("=" * 60)

    async def no_echo_round(messages: list[dict], tag: str) -> LLMEvent | None:
        terminal: LLMEvent | None = None
        try:
            async for ev in llm.stream_with_tools(messages, tools=[TOOL_SCHEMA]):
                if ev.type == "message":
                    terminal = ev
        except Exception as exc:  # noqa: BLE001
            mark = f"{type(exc).__name__}"
            print(f"  [{tag}] FAIL {mark}: {str(exc)[:300]}")
            return None
        calls = len(terminal.tool_calls) if terminal else -1
        print(f"  [{tag}] OK   finish_reason={terminal.finish_reason if terminal else None!r} "
              f"tool_calls={calls} content_len={len(terminal.content) if terminal else 0}")
        return terminal

    stress = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "履约保证金和投标保证金分别有什么限制？"},
    ]
    stress_ok = True
    for round_no in (1, 2):
        result = await no_echo_round(stress, f"round{round_no}")
        if result is None:
            stress_ok = False
            break
        if not result.tool_calls:
            print(f"  （第 {round_no} 轮未发起工具调用，测试提前结束）")
            break
        # 刻意不回传 reasoning_content
        stress.append({
            "role": "assistant",
            "content": result.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in result.tool_calls
            ],
        })
        for call in result.tool_calls:
            stress.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": "检索到 1 条资料：投标保证金不得超过招标项目估算价的 2%。",
            })

    # ---------- 5. 具名 tool_choice（agent.py 强制首轮调图谱的前提）----------
    # rag.is_enum_question() 命中时（「采购企业有哪些」这类列举问题），agent.py 会用
    # _stream_round_forced_graph() 强制模型首轮调用图谱工具。
    #
    # ⚠ 这条链路有个**实测出来的硬约束**：具名 tool_choice 与思考模式互斥——
    #   带 reasoning 的请求传具名 tool_choice 一律 400 "Thinking mode does not support
    #   this tool_choice"（"required" 也一样，只有 "none"/"auto" 能过）。
    #   所以强制那一轮必须同时关掉思考（extra_body={"thinking":{"type":"disabled"}}）。
    #   这三条下面都验：约束还在不在、关掉思考后能不能强制。
    print("\n" + "=" * 60)
    print("⑤ 具名 tool_choice：与思考模式互斥，需关掉思考才能强制")
    print("=" * 60)

    # 刻意挑一个**根本不需要工具**的问题：能把工具调出来，才说明生效的是「强制」，
    # 而不是问题本身就让模型想调工具
    FORCE_QUESTION = "你好，请用一句话介绍你自己。"

    async def force_once(choice, label: str, *, disable_thinking: bool = False):
        terminal: LLMEvent | None = None
        try:
            async for ev in llm.stream_with_tools(
                [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": FORCE_QUESTION},
                ],
                tools=[TOOL_SCHEMA],
                tool_choice=choice,
                disable_thinking=disable_thinking,
            ):
                if ev.type == "message":
                    terminal = ev
        except Exception as exc:  # noqa: BLE001
            print(f"  [{label}] 被拒 {type(exc).__name__}: {str(exc)[:200]}")
            return None, str(exc)
        names = [c.name for c in terminal.tool_calls] if terminal else []
        print(f"  [{label}] OK   finish_reason={terminal.finish_reason!r} tool_calls={names}")
        return terminal, ""

    named = {"type": "function", "function": {"name": "search_bidding_knowledge"}}

    baseline, _ = await force_once("none", "tool_choice=none（对照：本该不调工具）")
    rejected, reject_msg = await force_once(named, "具名强制（不关思考）")
    forced, _ = await force_once(named, "具名强制 + 关掉思考", disable_thinking=True)

    baseline_clean = bool(baseline is not None and not baseline.tool_calls)
    # 约束本身也要断言：哪天服务端放开了，这条会红——正好提醒去简化
    constraint_holds = rejected is None and "thinking mode" in reject_msg.lower()
    forced_names = [c.name for c in forced.tool_calls] if forced else []
    forced_ok = forced_names == ["search_bidding_knowledge"]

    print()
    print(f"  [{'OK  ' if baseline_clean else 'FAIL'}] 对照：不强制时模型不调工具")
    if _is_deepseek:
        print(f"  [{'OK  ' if constraint_holds else 'FAIL'}] 约束仍在：具名 tool_choice 单独用会被拒")
    else:
        # 非 DeepSeek：这条约束不存在。真被拒了反而要报出来（说明供应商行为变了）
        note = "未被拒（该约束不存在）" if rejected is not None else f"竟然被拒了：{reject_msg[:90]}"
        print(f"  [SKIP] 约束不适用（{type(llm).__name__}）：具名强制不关思考 → {note}")
    print(f"  [{'OK  ' if forced_ok else 'FAIL'}] 关掉思考后，具名强制确实调出了指定的工具")

    # ---------- 5b. 强制轮的产物怎么回传（回传字段缺失 = 下一轮 400）----------
    # 强制轮关掉了思考，所以它的 reasoning_seen=False。agent.py 的
    # _assistant_tool_message() 若按「这一轮出现过 reasoning 才回传」来判（曾经就是
    # 这么写的，是个真 bug），下一轮开着思考时服务端会因为历史里那条带 tool_calls 的
    # assistant 消息缺字段而直接 400：
    #     The `reasoning_content` in the thinking mode must be passed back to the API.
    # 实测**回传空串即可让下一轮照常开思考**，不必为此把整轮请求的思考都关掉。
    # 这条断言就是防它被改回去。
    print("\n" + "-" * 60)
    print("⑤b 强制轮之后：assistant 消息必须带上 reasoning_content（空串也行）")
    print("-" * 60)

    async def round2_after_forced(echo_reasoning: str | None, *, disable_thinking: bool) -> str:
        """拿强制轮的产物拼历史，试下一轮。返回空串表示通过，否则是错误摘要。"""
        if forced is None or not forced.tool_calls:
            return "强制轮没有产物，无法验证"
        echo: dict = {
            "role": "assistant",
            "content": forced.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in forced.tool_calls
            ],
        }
        if echo_reasoning is not None:
            echo["reasoning_content"] = echo_reasoning
        try:
            async for _ in llm.stream_with_tools(
                [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": FORCE_QUESTION},
                    echo,
                    {"role": "tool", "tool_call_id": forced.tool_calls[0].id,
                     "content": "（探针占位：工具执行结果）"},
                ],
                tools=[TOOL_SCHEMA],
                disable_thinking=disable_thinking,
            ):
                pass
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {str(exc)[:200]}"
        return ""

    # 强制轮的实际形状：关掉思考 → reasoning_seen 必为 False
    forced_no_reasoning = forced is not None and not forced.reasoning_seen
    absent_err = await round2_after_forced(None, disable_thinking=False)
    empty_err = await round2_after_forced("", disable_thinking=False)
    nothink_err = await round2_after_forced(None, disable_thinking=True)

    # 前提变了（强制轮竟然带了 reasoning）就说明整个 ⑤ 的前提失效，宁可报红
    echo_ok = empty_err == ""
    guard_ok = echo_ok and forced_no_reasoning

    print(f"  强制轮的 reasoning_seen = {forced.reasoning_seen if forced else None}")
    if _is_deepseek:
        print(f"  [{'OK  ' if absent_err else 'FAIL'}] 不回传字段 + 下一轮开思考 → "
              f"{absent_err or '竟然通过了（约束已变，可简化实现）'}")
    else:
        print(f"  [SKIP] 约束不适用（{type(llm).__name__}）：不回传字段 + 下一轮开思考 → "
              f"{absent_err or '未被拒'}")
    print(f"  [{'OK  ' if not empty_err else 'FAIL'}] 回传空串 + 下一轮开思考 → {empty_err or '通过'}")
    print(f"  [{'OK  ' if not nothink_err else 'FAIL'}] 不回传字段 + 下一轮也关思考 → "
          f"{nothink_err or '通过'}")
    # 结论：agent.py 必须**无条件**带上该字段（值可以是空串），实现见 _assistant_tool_message
    print(f"  [{'OK  ' if guard_ok else 'FAIL'}] 结论：带 tool_calls 的 assistant 消息"
          f"必须无条件回传该字段（空串即可）")

    # ---------- 结论 ----------
    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    print(f"  工具调用可拼装:          {bool(first.tool_calls)}")
    print(f"  不回传 reasoning_content: {'通过' if without else '被拒 → 必须回传'}")
    print(f"  回传空串 reasoning_content: {'通过' if with_empty else '被拒'}")
    print(f"  回传 None:                {'通过' if with_null else '被拒'}")
    print(f"  完整第二轮走通:          {second_ok and not second.tool_calls}")
    print(f"  多轮不回传压力测试:      {'全部通过' if stress_ok else '被拒 → 必须回传'}")
    _force_how = "可用（需关思考）" if _is_deepseek else "可用（关不关思考都行）"
    print(f"  具名 tool_choice 强制:    {_force_how if forced_ok else '不可用 → 依赖降级路径'}")
    print(f"  关思考参数已生效:        {forced_no_reasoning}（disable_thinking=True 那一轮没吐 reasoning）")
    print(f"  强制轮后接下一轮:        {'可（回传空串即可）' if guard_ok else '不通过 → 检查 _assistant_tool_message'}")
    print()
    if _is_deepseek:
        print("  注：**思考模式产出的**消息不回传该字段确实没被拒（单轮与多轮都试过）——")
        print("      「不回传就 400」这条广为流传的说法，在 2026-09 的 API 上只对下面这一种情形成立：")
        print("      历史里那条带 tool_calls 的 assistant 消息来自**关掉思考的强制轮**，")
        print("      而当前这一轮开着思考 → 缺字段必 400（见 ⑤b）。")
        print("      所以 _assistant_tool_message() 无条件带上该字段，空串也算。")
    else:
        print(f"  注：{type(llm).__name__} **没有** DeepSeek 那两条服务端约束（⑤ / ⑤b 已 SKIP），")
        print("      但 _assistant_tool_message() 仍然无条件回传该字段——那是为了**跨供应商统一**：")
        print("      多回传一个服务端不认的字段不会被拒（本页 ② 已验），少回传则可能换个供应商就 400。")

    return 0 if (first.tool_calls and with_empty and second_ok and forced_ok and guard_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
