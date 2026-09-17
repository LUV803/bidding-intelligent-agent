"""自建 MCP 服务器的**客户端半**：让三个工具经 MCP 走独立进程。

对应关系（一侧一个模块，互不替代）：

```
mcp_server.py      服务端半 —— `python -m bidding_qa.mcp_server` 起一个独立进程
mcp_bridge.py      客户端半 —— 本模块。后端把工具调用经它发过去
```

⚠⚠ **与 `mcp_client.py` 刻意分成两个模块，别合并、也别把本模块「参数化」成通用客户端。**
`mcp_client` 的 docstring 明写「刻意做小」并**点名拒绝**了两样东西 —— 工具发现
（`list_tools`）与 `structuredContent` —— 而这两样恰好是本模块要的。硬塞进那边会让
它那段**推理**（不只是那句清单）变成假话：它拒绝它们的理由是「本项目只有一个 MCP 服务、
只用它的一个工具」（Exa 托管端点的形状），而 Exa 确实只回 text block。
本模块面对的是**我们自己的**服务器，契约由我们定，所以两样都用。
代价是约 25 行连接样板重复，换 Exa 那条**本机无法端到端验证**的路径零风险 —— 划算。

⚠ **不得 import `..tools`**（`tools/base.py` 的 codec 也不得 import 本模块）。
`tools/__init__.py` 会把 `rag_tools` 拉进来，而 `rag_tools` 要 import 本模块 ——
反过来就成环，故障形态是**入口相关**的静默故障（服务端起来没事、某个脚本一跑就炸），
见 `mcp/__init__.py` 的 docstring。所以本模块只回**裸 dict**，
`outcome_from_payload()` 那一步在 `tools/rag_tools.py` 里做。

四条设计决定，改之前先读：

1. ⚠⚠ **失败一律抛 `BridgeError`，不返回结果对象。** 与 `mcp_client` 的
   「永不抛、返回 `McpResult`」**有意相反**，理由不是风格：三个 `run()` **本来就有**
   `except Exception` 兜底并产出各自那句已标定的中文失败文案，抛进去就是**零新增错误处理
   代码**；而返回结果对象意味着三个 `run()` 各自多一个 `if` 分支，那才是新增的、
   会漏改的东西。`CancelledError` 是 `BaseException`，原样穿透（同 `mcp_client`）。
2. ⚠⚠ **冷却只由传输层失败触发，业务失败绝不冷却。** 业务失败 = 拿到了
   `structuredContent`（哪怕里面 `ok=False`）—— 那说明两个进程都好着，只是这次查不出东西，
   **换个词再查正是模型该做的**。若把 `ok=False` 也算失败，模型连查两次空结果就把工具
   **锁死 30 秒**，而界面上完全看不出来。判据取「有没有拿到结构化结果」这个**结构**，
   不取 `ok` 的值，也不去解析错误文案（话术判据，本仓库记过多次）。
3. **`BridgeError` 的文案必须带上端点串。** 「工具突然全挂」时唯一的分诊线索是
   「服务器进程在不在」，而这句话会原样进 `role:"tool"` 消息、进模型上下文、
   进前端的工具调用块 —— 三处都看得到。
4. **两次调用现连现断**，不维护长会话。理由与 `mcp_client` 第 1 条逐字相同
   （streamable HTTP 有状态，长会话要么接进 lifespan、要么写一个「断了就重连」的状态机，
   而那个状态机的失败形态是整条问答挂住）。这里的代价同样是每轮多一次握手。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..config import settings

logger = logging.getLogger(__name__)


def _http_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """给 `streamablehttp_client` 用的 httpx 工厂：**关掉代理自动发现**。

    形状照 SDK 的 `create_mcp_http_client()`（`follow_redirects=True`、缺省 30s 超时），
    只多一条 `trust_env=False`。自己写而不是包一层，是因为 `trust_env` 在
    `Client.__init__` 里就决定了代理 mount 表，构造完成后再改属性**不会**重建它。

    ⚠⚠ **这不是洁癖，是本机实测出来的**：这台机器的 Windows Internet Settings 里配了系统
    代理（`http://127.0.0.1:57777`），httpx 默认 `trust_env=True`，经
    `urllib.request.getproxies()` 把它读了进来，而**没有** `no_proxy` —— 于是
    **连 127.0.0.1 的请求也绕去那个代理**，从不直连。两条实测：

    | 目标 | `trust_env=True` | 本模块工厂 |
    | --- | --- | --- |
    | `127.0.0.1:9`（死端口） | **502 Bad Gateway**（代理回的） | `All connection attempts failed`（真连不上） |
    | `127.0.0.1:8791`（真在跑） | 200（代理转发成功） | 200 |

    ⇒ 它**当前能用**，但那份「能用」是**那个代理的 loopback 策略给的**，不是自己挣来的：
    代理一关、规则一改、或者换成不转发回环的那种，MCP 这条路就跟着变。而**失败形态极具
    误导性** —— 死端口回的是 502（一个正常 HTTP 响应），看起来像「服务器起来了但握手
    不对」，**指向不到「你的流量绕了一圈出口代理」**（直连的话那句话会是连接被拒）。

    ⚠ 本模块**只连我们自己的服务器**（`mcp_server_url`，默认回环），所以关掉自动发现
    是无损的 —— 它是本仓库**唯一**一条「HTTP 打本机」的路径（Neo4j 走 bolt、PG 走
    psycopg，两者都不理会 HTTP 代理设置；其余出网调用全打外部主机，正需要这个代理）。

    ⚠⚠ **别顺手把这条搬到 `mcp_client.py`**：那条路要连 `mcp.exa.ai`，
    在这台机器上**正是靠这个系统代理出网**的 —— 照搬过去会把 Exa 打死。
    """
    kwargs: dict[str, Any] = {"follow_redirects": True, "trust_env": False}
    kwargs["timeout"] = timeout if timeout is not None else httpx.Timeout(30.0)
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)

#: 一次调用（含握手）的存活上限。**刻意不做成配置项**：真正的界限在服务器那头，
#: 各 store 自己都有超时（Neo4j 连接超时 / `statement_timeout`），这里只是防「服务器
#: 进程卡住但 socket 还开着」那种黑洞。12 秒对照的是实测值：图谱写查询 15~25ms、
#: 冷连接约 2s，本机 PG 毫秒级 —— 12 秒已经宽到不可能误杀。
#: ⚠ 别拿它跟 `exa_timeout`（20s）比大小：那个要装下 Exa 自己 6~10 秒的一次搜索，
#: 这个装的是本机回环上的两个数据库查询。两者没有可比性。
_TIMEOUT = 12.0

# 传输层/协议层失败后的冷却秒数。取值与理由照 `web_search._FAIL_COOLDOWN` /
# `graph_store._FAIL_COOLDOWN` / `web_search_exa._FAIL_COOLDOWN`（四处同值）。
# ⚠ 本模块的失败**特别**该冷却：它卡在 SSE 关键路径上，服务器没起时若不冷却，
#   此后每次提问都要白等一个 12 秒的连接超时，而每次的结果都是同一句「连不上」。
_FAIL_COOLDOWN = 30.0

# 冷却状态。命名与写法跟 `web_search.py` 那三个模块逐字同构。
_failed_at: float | None = None
_last_error: str | None = None
# 最近一次成功的时间，只给诊断用（探针 ④ 会读 `_failed_at`，别改这个名字）
_last_ok_at: float | None = None


class BridgeError(Exception):
    """**传输层/协议层**失败。业务失败（`ok=False` 的载荷）走正常返回，不是异常。

    ⚠ 调用方（`tools/rag_tools.py` 的三个 `run()`）**不该去分辨**它属于哪一类 ——
    两者对模型的处置是同一句「这次调用没成，别编」。分类在这里是为了**冷却**：
    只有异常这一支进冷却。
    """


def is_enabled() -> bool:
    """后端这次是不是要走 MCP。**只读配置，不做任何网络探测。**

    ⚠ 三个 `run()` 的判据就是它，所以它必须是**纯函数** —— 任何「顺便探一下服务器在不在」
    的写法都会让每轮问答多一次网络往返，而且是在**模型第一次工具调用之前**。
    服务器不可达由 `call()` 如实报失败，不需要提前知道。

    ⚠ 留空是**默认且健康**的状态（工具在本进程内执行），不是「没配好」——
    这条决定了它**不能**进 `_OPTIONAL_CAPS`（见 `config.mcp_server_url` 的注释）。
    """
    return bool(settings.mcp_server_url.strip())


def _endpoint() -> str:
    """日志与错误文案里用的端点串。原样取配置（不做归一化）。"""
    return settings.mcp_server_url.strip()


def _one_line(exc: BaseException) -> str:
    """异常 → 单行短文案。**必须钻进 `ExceptionGroup` 里去取真正那条**。

    ⚠⚠ 这不是锦上添花：anyio 会把连接失败包成 `ExceptionGroup`，而它的 `str()` 是
    「unhandled errors in a TaskGroup (1 sub-exception)」—— 那句话**指向不到任何东西**，
    分不出「连接被拒 / 超时 / 握手失败 / 端口上跑的是别的东西」。而本模块的文案会
    原样进 `role:"tool"` 消息、进模型上下文、进前端那个工具调用块 ——
    它是这条链路**唯一**的分诊线索（同 `agent.web_lookup()` 那行日志的地位）。
    实测：不挖的话，工具报给用户的就是那句「unhandled errors in a TaskGroup」。

    ⚠ `mcp_client._one_line` **刻意不做这一步**（那条路本机无法端到端验证，
    按原计划只订正 docstring）—— 这是**记在案的差异，不是漏改**。那边的失败文案
    还会被 `web_search_exa` 判重试/冷却，真原因另有 `logger.warning` 带着异常栈。
    """
    leaf = _leaf(exc)
    raw = str(leaf).strip() or type(leaf).__name__
    first = raw.splitlines()[0].strip()
    return first[:200] if first else type(leaf).__name__


def _leaf(exc: BaseException) -> BaseException:
    """沿异常组往下挖，回**第一个有消息的叶子**；一个都没有就回最外层。"""
    first: BaseException | None = None
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop(0)
        if first is None:
            first = cur
        if isinstance(cur, BaseExceptionGroup):
            stack[0:0] = list(cur.exceptions)
            continue
        if str(cur).strip():
            return cur
    return first or exc


def _mark_failure(error: str) -> None:
    """记一次传输层失败并进入冷却。"""
    global _failed_at, _last_error
    _last_error = error
    _failed_at = time.monotonic()


def _mark_success() -> None:
    """成功即解除冷却。（单独的 `global` 声明，理由见 `web_search._mark_success`。）"""
    global _failed_at, _last_ok_at, _last_error
    _failed_at = None
    _last_error = None
    _last_ok_at = time.monotonic()


def _cooling_down() -> str | None:
    """在冷却里就回一句剩余秒数文案，否则 `None`。"""
    if _failed_at is None:
        return None
    left = _FAIL_COOLDOWN - (time.monotonic() - _failed_at)
    if left <= 0:
        return None
    return (
        f"自建 MCP 服务器 {_endpoint()} 暂时不可用（{left:.0f} 秒后自动重试）：{_last_error}"
    )


async def list_tools(*, timeout: float | None = None) -> list[dict[str, Any]]:
    """服务器挂了哪些工具。**永不抛异常**，失败回 `[]` 并记一行 WARNING。

    只给漂移比对用（探针 ③：`list_tools` 的 `inputSchema` vs `TOOL_SCHEMAS`）。
    ⚠ **刻意不做分页**：我们自己的服务器只挂三个工具（`mcp_server._EXPOSED`），
    `cursor` 那条路是为「工具多到一页放不下」准备的，本仓库没有那个形状。
    真要分了（谁往白名单里堆到几十个），这里会静默少拿 —— 所以返回的是
    **服务器说了算**的那一份，别拿它当「全部工具」的凭证。

    返回 `[{"name":…, "description":…, "inputSchema":…}]` —— 原样是 MCP 的字段名，
    不转成 snake_case：这是**协议边界**，改名会让比对变成两套口径。
    """
    if not is_enabled():
        return []

    limit = timeout if timeout is not None else _TIMEOUT
    try:
        async with streamablehttp_client(
            _endpoint(),
            timeout=limit,
            sse_read_timeout=limit,
            httpx_client_factory=_http_client_factory,
        ) as (read, write, _get_session_id):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=limit)
            ) as session:
                await session.initialize()
                result = await session.list_tools()
    except Exception as exc:  # noqa: BLE001 —— 探测接口，失败即「问不出来」
        logger.warning("MCP list_tools 失败: %s", _one_line(exc))
        return []

    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": tool.inputSchema,
        }
        for tool in result.tools
    ]


async def call(name: str, arguments: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """经 MCP 调一次工具，回**结构化载荷**（`ToolOutcome` 的 dict 形状）。

    失败抛 `BridgeError`（`CancelledError` 原样穿透）。四类失败各自的文案都带端点串：

    - 开关不是开着的（理论上到不了，调用方已经判过 `is_enabled()`）
    - 正在冷却（**不发请求**，直接抛 —— 这是冷却的全部意义）
    - 连不上 / 超时 / 握手失败
    - 连上了但契约不对：`isError`、或者没有 `structuredContent`

    ⚠ 最后一类里，「没有结构化结果」是**服务器跑的是旧代码**的信号
    （老版本的 handler 只回文本）。它与「跑的是新代码但这次查了个空」形状完全不同，
    所以必须在这里就分开 —— 前者让三个工具**全部**报失败，后者只是一个正常的空结果。
    """
    if not is_enabled():
        raise BridgeError("未配置 MCP_SERVER_URL，自建 MCP 服务器不可用。")

    cooling = _cooling_down()
    if cooling is not None:
        # ⚠ 冷却期**一行日志都不打**：这次失败在上一次已经记过了，而它卡在 SSE 关键路径上
        #   且每次提问都会走到这里。文案本身会进 `role:"tool"` 消息，用户看得见。
        raise BridgeError(cooling)

    limit = timeout if timeout is not None else _TIMEOUT
    endpoint = _endpoint()
    started = time.perf_counter()
    try:
        async with streamablehttp_client(
            endpoint,
            timeout=limit,
            sse_read_timeout=limit,
            httpx_client_factory=_http_client_factory,
        ) as (read, write, _get_session_id):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=limit)
            ) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    except asyncio.CancelledError:
        # ⚠ 浏览器断开时它必须能传上来（同 `mcp_client` 那条）—— 吞掉会让「用户点了停止」
        #   变成一次静默的重试，而这次重试还会占着 SSE 连接。
        raise
    except Exception as exc:  # noqa: BLE001
        error = f"连不上自建 MCP 服务器 {endpoint}：{_one_line(exc)}"
        _mark_failure(error)
        logger.warning("MCP 调用 %s 失败: %s", name, error)
        raise BridgeError(error) from exc

    # ---- 以下都是「连上了」，所以一律**不冷却**（见模块 docstring 第 2 条）----
    if getattr(result, "isError", False):
        # ⚠ 正常路径上到不了这里：服务器侧的 handler 自己吞异常、回 `ok=False` 的载荷
        #   （`mcp_server.build_server`）。所以 isError 只可能是**协议层**的问题
        #   （工具名不在服务器上、SDK 行为变了）—— 那是持续故障，照常冷却。
        error = (
            f"自建 MCP 服务器 {endpoint} 返回错误：{_text_of(result) or '未说明原因'}"
        )
        _mark_failure(error)
        logger.warning("MCP 调用 %s 被拒: %s", name, error)
        raise BridgeError(error)

    payload = getattr(result, "structuredContent", None)
    if not isinstance(payload, dict):
        # ⚠ 这条**不是**「服务器没配好」，是**两边的契约对不上**：新客户端 + 旧服务器
        #   （旧 handler 只回文本）。文案要指向「服务器跑的是哪份代码」，否则会有人去查网络。
        error = (
            f"自建 MCP 服务器 {endpoint} 没有返回结构化结果"
            f"（多半是服务器进程跑的还是旧代码，重启它）：{_text_of(result)[:120]}"
        )
        _mark_failure(error)
        logger.warning("MCP 调用 %s 契约不匹配: %s", name, error)
        raise BridgeError(error)

    _mark_success()
    # ⚠ 传输耗时只记 debug，**不折进 `elapsed_ms`** —— 那个字段是各 store 自己的时钟，
    #   折进传输时间会让同一个查询在「进程内」与「经 MCP」两条路径上报出不同的数，
    #   而报告里那两行是要能互相印证的。
    logger.debug(
        "MCP 调用 %s 完成，传输耗时 %.0fms", name, (time.perf_counter() - started) * 1000
    )
    return payload


def _text_of(result: Any) -> str:
    """把 `CallToolResult` 的 text block 拼起来。只用于**失败时**的一句话诊断。"""
    blocks = getattr(result, "content", None)
    if not isinstance(blocks, list):
        return ""
    parts = [
        str(getattr(b, "text", ""))
        for b in blocks
        if getattr(b, "type", "") == "text" and getattr(b, "text", "")
    ]
    return "\n".join(parts).strip()
