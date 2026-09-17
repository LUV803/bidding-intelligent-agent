"""一个**刻意做小**的 MCP 客户端：连上托管端点 → 握手 → 调一次工具 → 把 text block 交回来。

⚠ **本模块只管 Exa 那个托管端点**（一个服务、一个工具）。自建的那个服务器
（`python -m bidding_qa.mcp_server`）走的是 `mcp_bridge.py` —— **两个模块刻意不合并**：
下面第 4 条拒绝 `structuredContent`、以及这里拒绝工具发现，对 Exa 是真话
（它确实只回 text block），对自建服务器是假话（那边的契约由我们定）。
把两套互斥的契约塞进一个模块，只能靠文档撒谎来收场。

不通用，也不打算通用：通用化在这里的代价是真实的 —— 传输方式（stdio / sse /
streamable HTTP）、工具发现、`structuredContent`、采样回调、根目录协商，
每一项都是一段**没人会读**的分支。真正需要通用客户端时，官方 SDK 就在依赖里。

四条设计决定，改之前先读：

1. ⚠⚠ **每次调用现连现断，不维护长会话**。streamable HTTP 是**有状态**的
   （`Mcp-Session-Id`），长会话要么接进 lifespan、要么自己写一个「断了就重连」的状态机，
   而那个状态机的**失败形态是整条问答挂住**。现连现断的代价只是每轮多一次 HTTP 握手
   （相对 Exa 自己那次搜索可以忽略），失败形态却是「这一轮搜不到」——
   两边的代价不对称，所以选便宜的。`MCP.md` §3.3 把这条列为待定项，这是结论。
2. **`call_tool()` 永不抛异常**（`CancelledError` 除外）。同 `web_search.search()` /
   `graph_store.query()`：工具执行漏掉 `role="tool"` 响应会让下一轮请求 400，
   所以失败一律转成 `(None, 错误文案)`。
3. ⚠⚠ **认证走 `Authorization: Bearer` 头，绝不进 URL**。托管端点也认
   `?exaApiKey=<key>` 那种写法，**那是不能用的那一种** —— 它会把密钥带进
   httpx 的请求日志、异常信息和任何一层代理的访问日志里（与 `web_search.py` 里
   「认证走 Header，绝不进 URL」是同一条纪律，那条注释在那儿）。
   实测过托管端点对 `Authorization: Bearer <错 key>` 会明确回 401，
   而 `x-api-key` 头**不生效**（给错的也照常返回结果）—— 所以这里用 Bearer。
4. **不读 `structuredContent`**：MCP 2025-06-18 加了它，但 Exa 的服务器仍在走
   text content block（`MCP.md` 附录记了这条核实结果）。要结构化数据得自己解析那段文本 ——
   那是 `web_search_exa.py` 的事，不是这一层。
   ⚠ **这条只对 Exa 成立**，别照着它去改 `mcp_bridge.py` —— 那边读的正是
   `structuredContent`，因为那是我们自己的服务器、契约由我们定。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, NamedTuple

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..config import settings

logger = logging.getLogger(__name__)

#: 认得 MCP 的人一眼能找到的「我们用的是哪个协议版本 / 哪个 SDK」。日志与探针用。
CLIENT_NAME = "bidding-qa"
CLIENT_VERSION = "0.1.0"


class McpResult(NamedTuple):
    """一次 `call_tool` 的结局。

    ⚠⚠ **`coolable` 单独一个字段，而不是让调用方去猜错误文案的前缀。**
    按字符串判「这是超时还是参数错」正是本仓库记过的那类**话术判据**
    （`/api/chat` 旧触发 ③ 死于它、`is_enum_question` 的反例词表为它写得那么舍得）：
    文案一改，判据静默失效。这里在**产生错误的那一行**就把它分好类。

    两个标志位互不覆盖：`text` 有值就是成功；`error` 有值就是失败；
    `coolable` 只在失败时有意义，表示「等 30 秒再试有意义」。
    """

    text: str | None = None
    error: str | None = None
    coolable: bool = False


def is_configured() -> bool:
    """配没配 Key。**只读配置，不发起任何网络调用。**

    ⚠ 托管端点其实**允许无 Key 匿名访问**（按 IP 限流，约 2 QPS / 50 次每天）。
    本项目**刻意不用那条路**：50 次/天在演示中途静默用光是典型的「看起来能用、
    到时候坏掉」，而本仓库对可选能力的一贯处置是「没配就如实说没配」。
    想试匿名模式的话，把 `exa_api_key` 留空**并把 `EXA_ENABLED` 那段判断改掉** ——
    现在它会把空 Key 当成「不可用」。
    """
    return bool(settings.exa_api_key.strip())


async def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float | None = None,
) -> McpResult:
    """调一次 MCP 工具。**永不抛异常**（`asyncio.CancelledError` 除外）。

    ⚠ `CancelledError` 原样抛出 —— 浏览器断开时它必须能传上来，
    把它吞掉会让「用户点了停止」变成一次静默的重试。

    ⚠ 多个 text block 会**按顺序拼起来**（MCP 允许一个结果带多个 block，Exa 目前只给一个）。
    非 text 的 block（图片等）直接跳过 —— 本项目的下游只认文本。
    """
    if not is_configured():
        return McpResult(error="未配置 EXA_API_KEY，Exa 联网搜索不可用。")

    limit = timeout if timeout is not None else settings.exa_timeout
    headers = {"Authorization": f"Bearer {settings.exa_api_key.strip()}"}
    try:
        # ⚠ `timeout` 是 HTTP 请求超时，`sse_read_timeout` 是「SSE 流上两次数据之间最多等多久」
        #   —— 工具调用的**结果本身是从 SSE 流回来的**，所以后者才是真正管着「一次搜索
        #   最多等多久」的那个。两者取同一个值：握手快、搜索慢，一个上限够用了。
        async with streamablehttp_client(
            settings.exa_mcp_url,
            headers=headers,
            timeout=limit,
            sse_read_timeout=limit,
        ) as (read, write, _get_session_id):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=limit)
            ) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 契约要求这里无论如何都不许抛
        # ⚠ Exa/anyio 的连接错误原文是**多行**的，还带着 URL。截断到一行再记，
        #   免得日志里一行日志占掉半屏；完整的异常栈由 logger 自己带。
        logger.warning("MCP 调用 %s 失败: %s", name, _one_line(exc))
        # ⚠⚠ **这一整支都是 coolable=True**，包括签名/参数不当导致的 4xx。
        #   与 Tavily 那侧（`web_search._is_coolable` 把 400 排除在外）**有意不同**：
        #   那边是把**模型给的查询词**放进请求体，400 可能是这一句查询的问法问题，
        #   冻住整个能力不合理；这边请求体里只有固定的工具名 + 我们自己拼的参数，
        #   4xx 一律意味着 URL / Key / 参数名配错了 —— 那是**持续**故障，
        #   冷却 30 秒正是它的正确处置（Tavily 的 401/403/429/5xx 也是这么处理的）。
        return McpResult(error=f"MCP 调用失败：{_one_line(exc)}", coolable=True)

    text = _join_text(result)
    if getattr(result, "isError", False):
        # ⚠ 服务端明确报错（参数不合法 / 配额用尽）走这里。**不冷却**：
        #   它是业务层判决，等 30 秒再来一次会得到同样一句话。
        return McpResult(error=f"Exa 返回错误：{text or '未说明原因'}")
    if not text.strip():
        # 也不算失败：连上了、也回了，只是这次是空的。该由调用方去重试。
        return McpResult(error="Exa 返回了空内容。")
    return McpResult(text=text)


def _join_text(result: Any) -> str:
    """把一个 `CallToolResult` 里的 text block 拼成字符串。**逐块容错**。"""
    blocks = getattr(result, "content", None)
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        # ⚠ 判据是 `type == "text"`（MCP 的判别字段），不是 `hasattr(block, "text")`：
        #   ImageContent / EmbeddedResource 各有各的载荷，混进来会污染解析。
        if getattr(block, "type", "") != "text":
            continue
        text = getattr(block, "text", "")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _one_line(exc: BaseException) -> str:
    """异常 → 单行短文案。空消息（anyio 的 `ExceptionGroup` 有时是空的）回类名。"""
    raw = str(exc).strip() or type(exc).__name__
    first = raw.splitlines()[0].strip()
    return first[:200] if first else type(exc).__name__
