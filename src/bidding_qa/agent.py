"""Agent 模式：预检索 + 多工具多轮循环。**只剩对话主循环了。**

⚠ **工具本身已经搬去 `bidding_qa/tools/` 包**（2026-09-20）。四个工具的 schema、参数
模型、处理器、展示逻辑、以及「模型把工具调用漏进正文」的整条恢复机制，现在都在那边：

- `tools/base.py` —— 引擎（`BaseTool`、注册表、`execute_tool`、`recover_tool_calls`）
- `tools/rag_tools.py` —— 四个具体工具（检索 / 图谱 / 采购记录 / 联网）

本模块现在只负责**一轮问答怎么走**：预检索 → 按请求拼工具表 → 多轮流式往返 → 收尾。

⚠⚠ 本模块**仍然 re-export** 工具层的一批名字（含 `_peek_display` / `_HANDLERS` /
`_match_schema` 这些下划线名）。那不是历史包袱，是**有意的**：`api.py` 与十几个探针
都按 `agent.XXX` 取用，re-export 让它们一行都不用改。**新代码请直接从
`bidding_qa.tools` import**，别在这儿添新的转发。

⚠⚠ 但 re-export 是**只读**的：`agent.TOOL_SCHEMAS = [...]` 只是给本模块的命名空间
加了一个新名字，`tools_for()` 读的是 `rag_tools` 的全局 —— 想换工具表要写
`rag_tools.TOOL_SCHEMAS`（`eval_rag.py --pipeline agent` 就是那么干的）。

两条设计底线（**搬迁不改**）：

1. **工具执行永不抛异常**。工具报错却漏掉 role="tool" 的响应消息，下一轮请求会因为
   tool_call_id 无人匹配直接 400。这条由 `tools.base.execute_tool()` 保证。
2. **循环必定收口**。轮数用尽时再跑一轮禁用工具的强制收尾，否则多轮问题会以
   「（未获得回答）」收场。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from openai import BadRequestError

from . import graph_store
from .clients import BaseLLMClient, LLMEvent, ToolCall
from .config import settings
from .rag import (
    build_agent_messages,
    fallback_answer,
    is_enum_question,
    with_image_context,
)
from .retriever import Retriever
from .schemas import ChatMessage
from .tools import (
    EXA_TOOL,
    GRAPH_TOOL,
    SEARCH_TOOL,
    TOOL_EXA,
    TOOL_GRAPH,
    TOOL_NAMES,
    TOOL_PG,
    TOOL_SCHEMAS,
    TOOL_SEARCH,
    TOOL_WEB,
    PG_TOOL,
    WEB_TOOL,
    SourceRegistry,
    ToolContext,
    ToolOutcome,
    assess_outcome,
    exa_lookup,
    execute_tool,
    recover_tool_calls,
    tools_for,
    web_lookup,
    _FORCE_GRAPH_CHOICE,
    _HANDLERS,
    _peek_display,
    _render_result,
)
from .tools.base import (
    _duplicate,
    _match_schema,
    _normalize_args,
    _resolve_enum,
    default_schemas,
    registered_tools,
)

logger = logging.getLogger(__name__)

# 工具名。**定义在 `tools/rag_tools.py`**，这里只是 re-export —— 探针与 `rag.py` 的
# 提示词片段按这些名字对齐，所以它们必须是同一批常量，不能各写一份字面量。
assert TOOL_SEARCH == SEARCH_TOOL.name and TOOL_GRAPH == GRAPH_TOOL.name
assert TOOL_PG == PG_TOOL.name and TOOL_WEB == WEB_TOOL.name
assert TOOL_EXA == EXA_TOOL.name
# 工具名 → 执行体。`_HANDLERS` 这个名字被 `probe_pg_store.py` 钉着，所以保留这个视图；
# 真正在跑的是 `tools.base.execute_tool()` 里的注册表查表。这里再 assert 一次两边同步，
# 免得日后有人只往注册表里加工具、忘了这个视图（它现在由 `register()` 一起维护）。
assert set(_HANDLERS) == {t.name for t in registered_tools()}, "注册表与 _HANDLERS 不同步"


def _assistant_tool_message(result: LLMEvent, *, echo_reasoning: bool) -> dict:
    """把一轮工具调用结果拼成回传用的 assistant 消息。

    reasoning_content 的处理是这里最容易踩的坑，请勿随手删除：

    - **带 tool_calls 的 assistant 消息一律回传该字段，空字符串也要带**，判据是
      「字段在不在」而不是「内容空不空」。模型在工具轮次里常返回空串，用
      ``if result.reasoning:`` 判断会漏掉这些轮次。
    - **尤其不能按「这一轮出现过 reasoning 才回传」来判**（曾经就是这么写的，是 bug）：
      强制轮关掉了思考（``reasoning_seen=False``），下一轮若开着思考，服务端会因为历史里
      那条带 tool_calls 的消息缺这个字段而直接 400
      "The reasoning_content in the thinking mode must be passed back to the API"。
      实测空串可以让下一轮照常开思考，见 scripts/probe_tools.py ⑤。
    - content 用 ""，**绝不能用 None**。
    - 服务端若哪天真收紧，_stream_round() 会自动去掉该字段重试一次。
    """
    msg: dict = {
        "role": "assistant",
        "content": result.content,
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments},
            }
            for c in result.tool_calls
        ],
    }
    if echo_reasoning:
        # 不看 reasoning_seen：字段缺失会让下一轮（开着思考）直接 400，
        # 而这一轮没思考时 result.reasoning 本就是空串，正好是服务端要的「字段在」。
        msg["reasoning_content"] = result.reasoning
    return msg


async def _stream_round(
    llm: BaseLLMClient,
    messages: list[dict],
    *,
    tools: list[dict] | None,
    tool_choice: str | dict | None,
    call_id_prefix: str,
    state: dict,
    disable_thinking: bool = False,
) -> AsyncIterator[LLMEvent]:
    """跑一轮，并在 reasoning_content 回传被拒时自动降级重试一次。"""
    try:
        async for ev in llm.stream_with_tools(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            disable_thinking=disable_thinking,
            call_id_prefix=call_id_prefix,
        ):
            yield ev
    except BadRequestError as exc:
        if state["echo_reasoning"] and state["allow_retry"] and "reasoning" in str(exc).lower():
            logger.warning("reasoning_content 回传被拒，去掉该字段重试一次: %s", exc)
            state["echo_reasoning"] = False
            state["allow_retry"] = False
            for msg in messages:
                if msg.get("role") == "assistant":
                    msg.pop("reasoning_content", None)
            async for ev in llm.stream_with_tools(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                disable_thinking=disable_thinking,
                call_id_prefix=call_id_prefix,
            ):
                yield ev
        else:
            raise


async def _stream_round_forced_graph(
    llm: BaseLLMClient,
    messages: list[dict],
    *,
    tools: list[dict] | None,
    call_id_prefix: str,
    state: dict,
) -> AsyncIterator[LLMEvent]:
    """首轮强制调用图谱工具，被拒则退回不强制。

    **必须同时关掉思考模式**：实测带 reasoning 的请求传具名 tool_choice 会被服务端直接拒
    （400 "Thinking mode does not support this tool_choice"；"required" 同样不行，
    只有 "none"/"auto" 能过）。所以强制这一轮走 disable_thinking。

    失败时（例如日后服务端改了这条规则）_stream_round() 会原样抛出——它的内部重试只认
    「reasoning」字样的报错——被 run_agent_stream() 外层的 except 接住就成了**一条 error
    事件**。所以这里自带降级：失败且尚未吐出任何内容时，以 tool_choice=None 重跑这一轮，
    退回纯提示词路径。本项目的「降级而非报错」原则，同 reranker / graph_store.warmup()。

    **只在没吐出内容时才重试**：`create()` 是在流式迭代之前调用的，正常情况下失败发生在
    任何 delta 之前；万一已经在吐字了还重试，正文会重复。
    """
    emitted = False
    try:
        async for ev in _stream_round(
            llm,
            messages,
            tools=tools,
            tool_choice=_FORCE_GRAPH_CHOICE,
            call_id_prefix=call_id_prefix,
            state=state,
            disable_thinking=True,
        ):
            emitted = True
            yield ev
    except Exception as exc:  # noqa: BLE001
        if emitted:
            raise
        logger.warning("强制调用图谱工具被拒（%s），本轮退回不强制，改走提示词路径", exc)
        async for ev in _stream_round(
            llm,
            messages,
            tools=tools,
            tool_choice=None,
            call_id_prefix=call_id_prefix,
            state=state,
        ):
            yield ev


async def run_agent_stream(
    llm: BaseLLMClient,
    retriever: Retriever,
    *,
    question: str,
    session_id: str | None,
    history: list[ChatMessage],
    top_k: int | None,
    mode: str,
    rerank: bool | None,
    use_llm: bool = True,
    # 图片解析结果（为空即没传图）。⚠ **只进提示词，不进检索**——见下面对
    # build_agent_messages 那一行的注释，`with_image_context` 的文档里写了为什么。
    image_context: str = "",
    # 前端「联网搜索」开关。⚠ **形参别改叫 web_search** —— 会遮蔽 `tools/rag_tools.py` 里
    # 导入的同名模块（见 tools_for）。它与 web_search.ENABLED 在 tools_for() 里合取，
    # 两者同时为真才真正挂工具。
    enable_web: bool = False,
) -> AsyncIterator[tuple[str, dict]]:
    """产出 (event_type, data)，由 api.py 转成 SSE。

    事件序列：start → sources → [(reasoning|delta|tool_call|tool_result|sources|answer_reset)*] → done
    """
    registry = SourceRegistry(cap=settings.agent_max_sources)
    started = time.perf_counter()
    state = {"echo_reasoning": settings.agent_echo_reasoning, "allow_retry": True}
    # 本轮的完整工具表（含按请求挂的 search_web）。**三个 tools= 调用点都用它**，
    # 漏掉任何一个都表现为「工具只在某些轮次存在」。
    #
    # ⚠⚠ 但**后端接不了工具时就整表剥空**（`supports_tools` 为假）。当前只有 Ollama
    #   会走到这里：官方 `deepseek-r1:8b` 的模板里没有 tools 段，带 tools 请求会被
    #   服务端**直接拒掉**——不是答得差，是一个字都出不来。剥空后本轮退化成
    #   「预检索 + 直接作答」，与 `/api/chat` 那条写死流水线的行为一致。
    # ⚠ 判据走 `getattr(..., True)` 兜底：探针里的假 LLM 是鸭子类型、不继承
    #   `BaseLLMClient`，硬取属性会把它们全挡在门外（见 clients/base_client.py）。
    tools = tools_for(enable_web) if getattr(llm, "supports_tools", True) else []
    # 从工具表**反推**提示词开关，而不是另算一遍 (enable_web and ENABLED)：这样
    # 「提示词提到工具 ⟺ 工具列表里有它」由构造成立，不必靠两处记得同步。
    #
    # ⚠⚠ 这条反推**只覆盖了 search_web 一个**：图谱与采购库那两段提示词在 rag.py 里
    #   直接读 `graph_store.ENABLED` / `pg_store.ENABLED`，看不见这里的 tools。
    #   所以还要把 `tools_ok`（= 工具表非空）传下去，改 `build_agent_messages` 那处。
    has_web = any(t["function"]["name"] == TOOL_WEB for t in tools)
    # ⚠ Exa 与 search_web 同源（`tools_for()` 里三个条件合取），所以这两个值要么同真
    #   要么同假。仍然各反推各的、**不写成 `has_web and ...`**：写法上多一个「谁依附谁」
    #   的判断，日后若有人让 Exa 单独挂，这里不会跟着静默错。多一次 O(n) 扫描而已。
    has_exa = any(t["function"]["name"] == TOOL_EXA for t in tools)
    tools_ok = bool(tools)

    yield ("start", {"session_id": session_id})

    # ---------- 预检索：让「来源」立刻可见，不必等模型 ----------
    try:
        initial = await asyncio.to_thread(retriever.search, question, top_k, mode, rerank)
    except Exception as exc:  # noqa: BLE001
        logger.exception("预检索失败")
        yield ("error", {"message": f"检索失败: {exc}"})
        yield ("done", {"total_ms": round((time.perf_counter() - started) * 1000, 2)})
        return

    latency: dict[str, float] = dict(retriever.last_latency)
    # ⚠ dropped 原先被丢弃不看：预检索的 top_k 也是调用方可传的，传大了会在这里被
    #   `agent_max_sources` 静默截断，而下面联网那几步又会再占掉 5 个编号 ——
    #   留给我们自己看一行，比日后靠猜强。
    _, _, dropped = registry.add(initial)
    if dropped:
        logger.warning(
            "预检索来源超出上限 %d，已丢弃 %d 条（调小 top_k 或调大 agent_max_sources）",
            settings.agent_max_sources,
            dropped,
        )
    yield ("sources", {"sources": registry.dump(), "latency_ms": dict(latency)})

    ctx = ToolContext(
        registry=registry,
        retriever=retriever,
        mode=mode,
        rerank=rerank,
        tool_names=tuple(t["function"]["name"] for t in tools),
    )

    # ---------- 联网搜索：开关打开就**必定**搜一次（原先只在本地 0 召回时兜底） ----------
    #
    # 需求原文：「选择了「联网搜索」后，**必需**检索本地知识库和联网搜索，并基于知识库检索
    # 结果和联网搜索结果回答用户问题。」—— 判据从 `not initial` 改成只看开关。`/api/chat`
    # 那边同步改了，两条链路现在是同一套语义。
    #
    # ⚠ 顺序有讲究，**别改**：
    #   ① 上面那个 sources 事件**照旧先发**（前端来源卡片秒出，约 50ms）。等联网完再发的话
    #      首屏要多等 1~2 秒，而「来源秒出」正是这条链路的手感来源。空 sources 不会闪空态
    #      —— ChatMessage.tsx 是 `message.sources.length > 0 &&`，空数组根本不渲染面板。
    #   ② 联网结果**必须登记进 registry 再推**（`web_lookup` 内部做了）：漏了登记的话，
    #      模型下一轮调一次检索工具，重发的 registry.dump() 里就没有这几条网页，
    #      面板里**静默消失**而正文还在引 [n]。
    #   ③ 也必须在下面 build_agent_messages 之**前**——否则提示词里根本没有这些网页资料。
    #
    # ⚠ 只在「模型真的会作答」时触发（use_llm and llm.ready）：没有大模型时搜回来的网页
    #   没人会读，只是白花一次 credit。`use_llm=False` 是「别调大模型」的显式请求，尊重它。
    #   这一轮下面会走 fallback_answer() 直接收尾，联网对它毫无意义。
    #
    # ⚠ 额度：本地预检索 + 这 5 条网页会在**任何工具轮之前**先占掉最多 len(本地)+5 个编号，
    #   而 registry 的上限是 settings.agent_max_sources（20）。本地顶格 5 条时是 5+5=10，
    #   留给后续工具轮次的还有 10 个。被丢弃的条数看 `registry.add()` 的返回值 `dropped`，
    #   探针里能直接断言，不用靠猜。
    #   ⚠⚠ 这个上限与 agent_max_rounds **是耦合的**：轮数越多、工具轮越多、越容易撞顶。
    #   撞顶曾经被读成「什么都没找到」（`fresh`/`known` 双双为空，与真没命中同形）——
    #   所以 ToolOutcome 现在带 `dropped`，`_render_result` 也为它单开一支。
    if has_web and use_llm and llm.ready:
        web_outcome = await web_lookup(question, registry=registry)
        if web_outcome.new_sources:
            latency["web_ms"] = web_outcome.elapsed_ms
            yield ("sources", {"sources": registry.dump(), "latency_ms": dict(latency)})
        # ⚠ 把这次预联网种进 ctx.seen：它是**绕过联网工具的去重分支**直接调的，不种键的话
        #   模型看到【资料】里已有网页结果仍可能自己再搜一次 —— 同一句话付两次 Tavily 费用，
        #   还白掉 agent_max_rounds 里的一轮。
        #   ⚠ 键是 `(TOOL_WEB, 原问题)`，**只挡同一句查询词**：模型换个更精确的词仍可调
        #   `search_web`，Agent 的自主性不受影响（`_AGENT_WEB_RULES` 里也是这么交代的）。
        #
        # ⚠⚠ **只在 `ok=True` 时种键**（`ok=True` 含「Tavily 正常应答但 0 条」——那种情况
        #   重搜同一句确实毫无意义）。`ok=False` 是**传输层/凭据层失败**（冷却、429、401、
        #   超时）。这时若也种了键，模型真去重搜同一句会撞上联网工具的去重分支，拿到一句
        #   「**该调用已执行过，结果与上次完全相同**」——而事实是**什么都没查到**。
        #   那句话会把 `_render_result(kind="web")` 特意保留的「未能完成 ≠ 没有新增资料」
        #   这条区分整个抹平，模型据此告诉用户「联网也没有搜到」。
        #   不种键反而更好：模型重搜时命中 `web_search` 的 30 秒冷却，**不发请求**就拿到真实的
        #   失败原因，既不花钱又能自我纠正。
        if web_outcome.ok:
            ctx.seen.add((TOOL_WEB, question.strip()))

    if not use_llm or not llm.ready:
        # 带上 `llm`：降级提示要点名**这一家**的 Key 没配（`rag._no_key_hint`）
        yield ("delta", {"content": fallback_answer(registry.dump(), llm.ready, llm)})
        yield ("done", {"total_ms": round((time.perf_counter() - started) * 1000, 2)})
        return

    # ⚠ 只有**这一行**用带图问题。上面那句预检索、以及工具内部的 retriever.search
    #   拿到的都是**原问题**——把图片内容拼进检索词会稀释向量、让 BM25 满屏乱命中，
    #   MIN_VECTOR_SCORE=0.72 是针对当前语料标定的，稀释后很可能直接返回 0 条。
    #   详见 rag.with_image_context 的 docstring。
    # ⚠ web=has_web 的语义是「**本轮真挂了 search_web**」而不是「用户开了开关」——
    #   提示词提一个没注册的工具就是 CLAUDE.md 点名的自相矛盾。
    # ⚠ tools_ok 是同一个道理的另一半：图谱与采购库那两段提示词读的是
    #   `graph_store.ENABLED` / `pg_store.ENABLED`（rag.py 里的三个 composer），
    #   看不见上面那张工具表。工具整表被剥空时必须一起压掉，否则提示词会让模型去调
    #   一个本轮**根本没挂出去**的工具。
    messages = build_agent_messages(
        with_image_context(question, image_context),
        registry.dump(),
        history,
        web=has_web,
        # ⚠ 同一个反推，同样不能写成「用户开了联网开关」：Exa 还压着
        #   `web_search_exa.ENABLED`（配没配 EXA_API_KEY）。多压一个进程级条件，
        #   提示词里就多一处可能提一个没挂出去的工具。
        exa=has_exa,
        tools_ok=tools_ok,
    )
    final: LLMEvent | None = None

    # 「采购企业有哪些」这种列举型问题：答案在法规知识库里必然检索不到，却一定会预检索到
    # 几篇字面相近的无关资料，模型会顺着它们编（实测）。首轮直接强制调图谱，把真实数据
    # 摆到模型面前，不指望它自己判断该不该调。**只在启用图谱时强制**——工具没挂出去还强制，
    # 等于让它调一个不存在的工具。
    # ⚠ `bool(tools)` 那截就是为「整表被剥空」加的（supports_tools 为假的后端）。
    force_graph = graph_store.ENABLED and is_enum_question(question) and tools_ok
    if force_graph:
        logger.info("列举型问句，首轮强制调用 %s", TOOL_GRAPH)

    # ---------- 工具执行体 ----------
    # 之所以抽成一个嵌套生成器，是因为它有**两个调用点**：普通工具轮（下面 for 循环里），
    # 以及收尾轮从泄漏标记里恢复出来的调用（再下面的「强制收尾」）。两处若各写一份，
    # `assess_outcome` 的追加语义 / `latency["tool_ms"]` 的记账 / pydantic 结果的
    # `.model_dump()` 会慢慢漂移，而漂移的表现是「某个块渲染成空白」——看不出少的是哪一步。
    async def _emit_calls(result: LLMEvent, round_no: int):
        """执行 `result.tool_calls`，依次产出 tool_call / tool_result（有新增来源时再发 sources）。"""
        # 串行执行：并行取消会留下半执行的兄弟任务，本场景也无并行收益
        for call in result.tool_calls:
            yield (
                "tool_call",
                {
                    "round": round_no,
                    "id": call.id,
                    "name": call.name,
                    "query": _peek_display(call.name, call.arguments),
                },
            )
            outcome = await execute_tool(call, ctx)
            # 结果质量检查：工具空手而归时**追加**一段显式的下一步（点名本轮其他工具
            # + 允许如实说没查到）。⚠ `outcome.text` 一个字都不动 —— 它是 `_render_result`
            # 的产出，字面量被 `probe_web_search.py` ⑥ 钉着；这里只在它**之后**接一段，
            # 结果合格时（返回 None）连分隔符都不多一个，非空路径逐字节不变。
            content = outcome.text
            quality = assess_outcome(call.name, outcome, ctx.tool_names)
            if quality:
                content = f"{content}\n\n{quality}"
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": content}
            )
            yield (
                "tool_result",
                {
                    "round": round_no,
                    "id": call.id,
                    "ok": outcome.ok,
                    "new_indices": outcome.new_indices,
                    "known_indices": outcome.known_indices,
                    "elapsed_ms": outcome.elapsed_ms,
                    "error": outcome.error,
                    # 图谱/数据库结果必须 .model_dump()：api.py 的 _sse 直接 json.dumps，
                    # 塞 pydantic 对象进去会在那一步炸 TypeError
                    "graph": outcome.graph.model_dump() if outcome.graph else None,
                    "pg": outcome.pg.model_dump() if outcome.pg else None,
                },
            )
            latency["tool_ms"] = round(latency.get("tool_ms", 0.0) + outcome.elapsed_ms, 2)
            # 图谱结果没有 new_sources，所以这里天然不发 sources —— 符合预期
            if outcome.new_sources:
                # 重新推送来源：前端收到 sources 是整体替换，必须带上完整 latency
                yield ("sources", {"sources": registry.dump(), "latency_ms": dict(latency)})

    try:
        for round_no in range(1, settings.agent_max_rounds + 1):
            result: LLMEvent | None = None
            if force_graph and round_no == 1:
                stream = _stream_round_forced_graph(
                    llm, messages, tools=tools, call_id_prefix="r1", state=state
                )
            else:
                stream = _stream_round(
                    llm,
                    messages,
                    tools=tools,
                    tool_choice=None,
                    call_id_prefix=f"r{round_no}",
                    state=state,
                )
            async for ev in stream:
                if ev.type == "reasoning":
                    yield ("reasoning", {"content": ev.text})
                elif ev.type == "content":
                    yield ("delta", {"content": ev.text})
                elif ev.type == "message":
                    result = ev

            if result is None:
                yield ("error", {"message": "模型响应流异常结束"})
                break
            if not result.tool_calls and result.leaked_markup:
                # 模型把工具调用写成了正文标记（走错通道）。这一轮若就此收尾，它刚说的
                # 「我再补充搜索一下」就成了空话——把调用捞回来，照常执行。
                # ⚠ 必须传本轮工具表：联网是按请求挂的，不传就看不见 search_web 的 schema，
                #   一次联网调用会因 0 票被整条丢弃（P0-B 那条线上 bug 的形状）。
                recovered = recover_tool_calls(
                    result.leaked_markup, prefix=f"r{round_no}", schemas=tools
                )
                if recovered:
                    logger.warning(
                        "第 %d 轮的工具调用漏进了正文，已从标记中恢复 %d 个: %s",
                        round_no,
                        len(recovered),
                        [c.name for c in recovered],
                    )
                    result.tool_calls = recovered
            if not result.tool_calls:
                final = result
                break
            if result.finish_reason == "length":
                logger.warning("第 %d 轮在 max_tokens 处截断，工具参数可能不完整", round_no)

            # 模型调工具前可能已经吐了一句「我先查一下」。这段话会被前端当成正式回答
            # 追加进 message.content，并随历史回传给下一轮 —— 必须让前端丢掉它。
            if result.content:
                yield ("answer_reset", {"round": round_no})

            messages.append(
                _assistant_tool_message(result, echo_reasoning=state["echo_reasoning"])
            )

            async for ev in _emit_calls(result, round_no):
                yield ev

        # ---------- 强制收尾：轮数用尽却仍在请求工具时，禁止再调，逼它作答 ----------
        #
        # ⚠⚠ 收尾轮**也要恢复泄漏标记**（2026-09-30 修）。原先这里只跑一轮、从不看
        #   `final.leaked_markup`，而 `_MarkupSuppressor` 一旦命中就把本轮 content 全部丢掉
        #   （只把标记**之前**那一截留在 content 里）——于是模型在收尾轮又把调用漏进正文时，
        #   拿到的是一个**空正文**，直接掉进下面那句 `fallback_answer()` 降级：用户看到的
        #   是一份来自知识库、却回答着**另一个问题**的原文，而它读起来完全像一次正常回答。
        #   2026-09-30 真机就是这么发生的（日志里「工具轮数用尽仍无回答」之后紧跟着
        #   「模型把工具调用标记写进了正文…共 0 个」），那个问题的答案一个字都没答上来。
        #   处置与普通轮（上面那个 `if not result.tool_calls and result.leaked_markup:`）
        #   **同构**：把调用捞回来执行，然后最多再跑一次收尾轮。
        #
        # ⚠⚠ **严格有界**（至多两轮收尾 + 至多一次恢复），绝不能改成 `while`：收尾轮是本链路
        #   唯一的收口保证 —— `agent_max_rounds` 用尽后必须**必定**落到一句回答或降级文案。
        #   恢复不出东西（0 票/并列）时直接收口，照 `recover_tool_calls` 那条「对不上就整条
        #   丢弃，绝不猜」的纪律，不在这里补第二套判据。
        #
        # ⚠ 首轮收尾**故意不追加 assistant 消息**的老规矩没变：那是为了「模型仍返回 tool_calls
        #   时没有 tool_call_id 悬空，直接丢弃即可」。而下面恢复出来之后**必须**追加 ——
        #   那些 tool_calls 会被真的执行，不追加的话对应的 role:"tool" 消息无人认领，
        #   下一轮请求直接 400（不变量 ①）。
        if final is None:
            final_round = settings.agent_max_rounds + 1
            for attempt in (1, 2):
                if attempt == 1:
                    logger.info("工具轮数用尽仍无回答，追加一轮禁用工具的收尾请求")
                else:
                    logger.info("收尾轮的标记已恢复执行，再追加最后一轮收尾请求")
                result = None
                async for ev in _stream_round(
                    llm,
                    messages,
                    tools=tools,
                    tool_choice="none",
                    # ⚠ 两轮的 prefix 必须不同：`_stream_round` 撞上 reasoning 400 时用
                    #   **同一个 prefix** 重试，而 agent_eval 的 `_tool_calls_of()` 是按 prefix
                    #   去重的（保留最后一次）。两轮共用一个 prefix 会让恢复出来的那次调用
                    #   与第二轮的记录互相覆盖。
                    call_id_prefix=f"final{attempt}",
                    state=state,
                ):
                    if ev.type == "reasoning":
                        yield ("reasoning", {"content": ev.text})
                    elif ev.type == "content":
                        yield ("delta", {"content": ev.text})
                    elif ev.type == "message":
                        result = ev
                final = result
                # 一个字都没吐（模型懵了 / 上游拒了）：没有标记可恢复，交给下面降级
                if final is None:
                    break
                if attempt == 2 or final.tool_calls or not final.leaked_markup:
                    break
                recovered = recover_tool_calls(
                    final.leaked_markup, prefix="final", schemas=tools
                )
                if not recovered:
                    logger.warning("收尾轮的标记没能判出归属，直接收口（不猜）")
                    break
                logger.warning(
                    "收尾轮的工具调用漏进了正文，已从标记中恢复 %d 个: %s",
                    len(recovered),
                    [c.name for c in recovered],
                )
                # 模型在这轮开头多半已经吐了一句「我再查一下」——与普通轮一样先作废它，
                # 否则它会被当成正式回答：渲染进气泡、写进 localStorage、作为历史回传
                if final.content:
                    yield ("answer_reset", {"round": final_round})
                final.tool_calls = recovered
                # ⚠ 必须**原样回传**这一轮的 content 与 reasoning（`_assistant_tool_message`
                #   无条件带 reasoning_content）——服务端要求历史里的工具调用消息带思考字段，
                #   少一个字段下一轮就 400，那条踩过两次
                messages.append(
                    _assistant_tool_message(final, echo_reasoning=state["echo_reasoning"])
                )
                async for ev in _emit_calls(final, final_round):
                    yield ev

        if final is None or not final.content:
            # `llm_ready=True` ⇒ 前缀整个为空，这里的 `llm` 传不传输出都一样；
            # 传着只为「降级提示一律点名对的那家」这条读起来一致
            yield ("delta", {"content": fallback_answer(registry.dump(), True, llm)})
        elif final.finish_reason == "length":
            logger.warning("最终回答在 max_tokens 处截断")
    except Exception as exc:  # noqa: BLE001 - 上游异常不应中断前端展示
        logger.exception("Agent 生成失败")
        yield ("error", {"message": f"生成回答失败: {exc}"})

    yield ("done", {"total_ms": round((time.perf_counter() - started) * 1000, 2)})


# ---------------------------------------------------------------- re-export
# 工具层的一批名字（`api.py` 与十几个探针按 `agent.XXX` 取用）。下划线开头的**必须显式
# 列出**：`from .tools import *` 会跳过它们，而 `probe_graph_tool.py` / `probe_pg_tool.py`
# 要用的正是 `_peek_display`，`probe_pg_store.py` 钉着 `_HANDLERS`。
#
# ⚠ 这些名字**只是转发**。`agent.TOOL_SCHEMAS = [...]` 之类的**赋值**不会影响工具层
#   （`tools_for()` 读的是 `rag_tools` 的全局），要改就改 `rag_tools.TOOL_SCHEMAS`。
__all__ = [
    "run_agent_stream",
    # --- 工具层 re-export（新代码请从 bidding_qa.tools import）---
    "TOOL_SEARCH",
    "TOOL_GRAPH",
    "TOOL_PG",
    "TOOL_WEB",
    "TOOL_EXA",
    "TOOL_SCHEMAS",
    "TOOL_NAMES",
    "SEARCH_TOOL",
    "GRAPH_TOOL",
    "PG_TOOL",
    "WEB_TOOL",
    "EXA_TOOL",
    "SourceRegistry",
    "ToolContext",
    "ToolOutcome",
    "ToolCall",
    "tools_for",
    "web_lookup",
    "exa_lookup",
    "assess_outcome",
    "execute_tool",
    "recover_tool_calls",
    "registered_tools",
    "default_schemas",
    "_HANDLERS",
    "_peek_display",
    "_render_result",
    "_duplicate",
    "_match_schema",
    "_normalize_args",
    "_resolve_enum",
]
