"""自建 MCP 服务器：把三个工具挂在一个**独立进程**上，让 Agent 当调用方。

```
python -m bidding_qa.mcp_server            # 默认 127.0.0.1:8765/mcp
```

对应关系（一侧一个模块，互不替代）：

```
mcp_server.py      服务端半 —— 本模块
mcp/mcp_bridge.py  客户端半 —— 后端把工具调用经它发过来
```

⚠⚠ **本文件在包顶层，不在 `mcp/` 下**。`mcp/` 是「MCP **客户端**层」那三个模块的家
（`mcp_client` / `web_search_exa` / `mcp_bridge`），而本模块是**另一个方向**的服务端：
它不连任何外部服务，只把 `tools/` 里已有的三个 `run()` 暴露出去。

## 为什么只挂这三个

`MCP.md` §4.3 的结论：只有 `search_knowledge_graph` / `search_procurement_records` /
`load_skill` 不碰 Qdrant 的**排他锁**，所以能与后端**并存**（本地 Qdrant 是嵌入式文件模式，
同一目录起两个持有者会直接抛 `RuntimeError`）。

另外三个不在白名单里，各有各的理由：检索工具 `search_bidding_knowledge` 持有 retriever
（那正是 Qdrant 锁的持有者）；`search_web` / `search_web_exa` 是**每请求**挂的工具
（跟着用户那个联网开关走），而本服务器的工具表是**进程级**的。

## 四条硬约束，改之前先读

1. ⚠⚠ **白名单查在 `execute_tool()` 之前**（`_run_one()` 的第一行）。本进程一旦
   `import .tools`，`_REGISTRY` 里就躺着**六个**工具 —— 客户端传
   `search_bidding_knowledge` 过来，`execute_tool()` 会高高兴兴地执行它，而
   `ctx.retriever=None` ⇒ `AttributeError`，或者更糟：静默的空结果。
   不在白名单就直接回错误文本，并给 `ctx.tool_names` 设成 `_EXPOSED`，
   让那句「可用工具：…」列出**这里真有的三个**。
2. **`ctx.retriever = None` 是安全的，但只因为上一条。** `ToolContext.retriever` 是必填字段
   （类型 `Any`），三个挂出来的工具**都不读它**。这一条是白名单的推论，不是独立保证 ——
   往白名单里加工具时先确认它不碰 retriever。
3. ⚠⚠ **handler 必须返回 2-tuple `(正文, 载荷)`**。SDK 对「长度为 2 的 tuple」走
   `CombinationContent` 分支（`mcp/server/lowlevel/server.py` 的 `call_tool`）：
   `content=[正文]`、`structuredContent=载荷`。**只返回 dict 的话**，SDK 会把载荷
   `json.dumps(indent=2)` **美化打印进 content** —— 用 Claude Desktop 这类第三方客户端
   连上来时，看到的就是一屏 JSON，而不是我们那句中文正文。
4. **`@server.call_tool(validate_input=False)`，且不设 `outputSchema`**，两条都是刻意的：
   - `validate_input=True`（默认）会拿 `inputSchema` 跑 `jsonschema.validate`，把
     `graph_store` 那句**带可用值列表**的中文错误换成
     `Input validation error: 'e_related' is not one of [...]`。而「可用类型：…」正是模型
     在同一轮内自我纠正的唯一线索（`tools/rag_tools.py` 的 `GraphArgs` 注释里记着这条）。
     校验交给各工具的 `parse_args` + handler，那边本来就是为**宽容**设计的
     （`tolerant_int` 之类的容错夹紧）。
   - `outputSchema` 一旦声明，SDK 在「声明了却没返回」时会直接抛错。而我们的载荷是
     `ToolOutcome` 的 dict 形状，没有（也不想有）一份 JSON Schema —— 见
     `tools/base.py` 的 `outcome_to_payload()`。

## 其余几条设计决定

- **不 mount 进 FastAPI**，独立起一个 Starlette 应用。两个理由：① Starlette **不跑被 mount
  子应用的 lifespan**，而 `StreamableHTTPSessionManager` 的任务组正是靠 lifespan 起来的，
  mount 进去的表现是每个请求都炸 `Task group is not initialized`；② 本服务器要能与后端
  **并存**（那正是这三条白名单存在的理由），塞进后端进程里就把这一点抵消掉了。
- **不传 `security_settings`** —— DNS-rebinding 防护**默认就是关的**（逐层读源码核实：
  `StreamableHTTPSessionManager(None)` → `StreamableHTTPServerTransport(security_settings=None)`
  → `TransportSecurityMiddleware(None)` → `TransportSecuritySettings(enable_dns_rebinding_protection=False)`）。
  所以没有「服务起来了但每个请求 421 Invalid Host」那个坑。⚠ **别「顺手加固」**成
  `allowed_hosts=["127.0.0.1:*"]`：Host 是**精确匹配**（只有端口那一段支持通配），
  而客户端连 `localhost:8765` 时 Host 头就是 `localhost:8765` —— 一次不匹配就 421，
  而且只在真机上打得出来（`TestClient` 那条路根本走不到）。
- **本服务没有任何认证**，所以默认只绑回环。绑到别的网卡时 `main()` 会多打一行 WARNING
  —— 那是 `API_HOST=0.0.0.0` 那个既有教训（本仓库记过：局域网内谁都能问，还能白烧
  Tavily 额度）。这里更敏感：这个进程直接握着两个数据库的凭据。
- **启动时预热一次 Neo4j / PostgreSQL**（各自吞异常，同 `api._bootstrap()` 那三个内层
  try）。图谱新 driver 的首查有约 2 秒建连开销，不预热就会由**用户的第一次提问**来付。

## ⚠ 启动方式：输出别接进「没人读的管道」（实测，2026-09-30）

直接跑在终端里、或 `> 文件`，都是健康的。但**别把它接进一个不读的管道** ——
`Popen(..., stdout=PIPE)` 然后从不读，会把它冻住：

日志每条都 `flush()`，而管道写满后这个 `write` 会**阻塞事件循环线程**。一次完整
5 调用的运行总共只吐 **5542 字节**（69 行），所以冻点约在 3.3KB —— 与 Windows
匿名管道默认约 4KB 的缓冲吻合，正好落在第 3 次调用上。冻住之后的表现是
「前两次正常、第 3 次 5 秒超时、之后永久不可达」，与真的会话泄漏**长得一模一样**。

实测对照（同一份客户端脚本，只改子进程输出的去处）：

| 子进程输出 | 结果 |
| --- | --- |
| 文件 | 5/5 成功，0.34~0.36s |
| 管道，无人读 | 0.34 / 0.35 / **5.35** / 失败 / 失败 |
| 管道，**有线程读干** | 5/5 成功，0.34s（与写文件逐字节同等） |

⇒ `probe_mcp_server.py` ⑤ 拉起子进程时**必须写文件或派个线程读干**，否则那个探针
自己就会复现这个冻住。这条是**测试夹具的产物，不是服务器缺陷**（生产里没人这么起它）。

## 客户端中途断开（实测，2026-09-30）

**结论先行：`stateless` 不是解药，别再为这件事去翻那个开关。**

前端点「停止」时 `/api/agent` 的生成器被取消 → `mcp_bridge.call()` 的 `CancelledError`
穿透 → 连接在**响应写到一半**时被拽断。服务端因此拿到 `anyio.ClosedResourceError`，
而 SDK 对这个形状是**无条件打一整段 traceback**的 —— 两条路各有一份：

```
mcp/server/streamable_http_manager.py:248   logger.error(f"Session {id} crashed: {e}", exc_info=True)   # 有状态
mcp/server/streamable_http_manager.py:182   logger.exception("Stateless session crashed")              # 无状态
```

⇒ 翻成 `stateless=True` 只是换措辞（`Session X crashed` → `Stateless session crashed`），
**traceback 一行不少**。「会话泄漏」这个同样常见的翻开关理由也不成立：有状态那条路径在
`:252-264` 的 `finally` 里已经把崩掉的会话从 `_server_instances` 摘干净了。

实测（4 次不同时机的中途取消，`0.05 / 0.12 / 0.18 / 0.25s`）：**4/4 各留一段 traceback**，
而服务器**照常服务** —— 取消之后紧接着的那次调用 0.34s 正常返回，一次都没被打乱。
所以这是**日志噪声，不是故障**。但它是操作者每点一次「停止」就会看到的东西，
所以 `main()` 里挂了一个**窄过滤器**（`_quiet_aborted_sessions()`，见该函数）：
只丢弃「异常组里含 `ClosedResourceError`」这一种记录，其余任何崩溃照旧全量打出来。

⚠ 与它容易混的另一条（**服务器猝死**）实测是好的，不需要任何处理：调用方拿到的是
「连不上自建 MCP 服务器 http://… ：All connection attempts failed」（**端点串在文本里**），
第二发 0.00s 短路命中 30 秒冷却、**没有发起连接**。见 `probe_mcp_server.py` ⑤。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

import anyio
import mcp.types as types
import uvicorn
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route

from . import graph_store, pg_store
from .clients import ToolCall
from .config import settings
from .tools import (
    TOOL_GRAPH,
    TOOL_PG,
    TOOL_SCHEMAS,
    TOOL_SKILL,
    SourceRegistry,
    ToolContext,
    ToolOutcome,
    execute_tool,
    outcome_to_payload,
)

logger = logging.getLogger("bidding_qa.mcp_server")

#: 认得 MCP 的人一眼能找到的「这跑的是什么、哪个版本」。与 `mcp_client.CLIENT_NAME` 对称。
SERVER_NAME = "bidding-qa-tools"
SERVER_VERSION = "0.1.0"

#: 本服务器挂出去的三个工具。**顺序即 `list_tools` 的顺序**，也是
#: 「可用工具：…」那句里的顺序（两者取自同一处，不会漂移）。
_EXPOSED: tuple[str, ...] = (TOOL_GRAPH, TOOL_PG, TOOL_SKILL)

#: 回环地址的几种写法。别的都算「暴露到网卡上了」，`main()` 会 WARNING。
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}

_DEFAULT_PATH = "/mcp"


# ================================================================== 工具表
def _definition(name: str) -> dict[str, Any] | None:
    """从 `TOOL_SCHEMAS` 里取一个工具的 **OpenAI 形状** schema，取不到回 `None`。

    ⚠ 这里**逐字**交出 `TOOL_SCHEMAS` 里那一份，不重新生成 —— `to_openai_schema()`
    的产物是**标定过的**（`Any` + `json_schema_extra` 那套容错夹紧、`required` 无条件写进去、
    中文逐值说明写在 description 里），而 `FastMCP` 会从 Pydantic 走
    `model_json_schema(by_alias=True)` **重新推导**一遍，把这些全毁掉。本模块用 lowlevel
    `Server` 而不是 `FastMCP`，唯一的原因就是这一条。
    """
    for schema in TOOL_SCHEMAS:
        fn = schema.get("function") or {}
        if fn.get("name") == name:
            return fn
    return None


def exposed_tools() -> list[dict[str, Any]]:
    """本服务器**实际**挂出去的三个工具（OpenAI 形状的 `function` 那一层）。

    ⚠⚠ 判据是 `_EXPOSED ∩ TOOL_SCHEMAS`，**不是** `_EXPOSED` 本身。`TOOL_SCHEMAS` 按
    `graph_store.ENABLED` / `pg_store.ENABLED` 过滤（那两个是**进程级**常量，导入期按
    `.env` 算死），所以「图谱没配」的机器上这里只剩两个工具。

    这是**对的**：客户端那侧读的是**同一份 `.env`**（`config.py` 的 `env_file` 是绝对路径，
    与 CWD 无关），它那份 `TOOL_SCHEMAS` 同样少一个，模型压根不会去调。
    两边 `.env` 不一致时才会出现「客户端挂出去、服务器没有」——那种情况由
    `scripts/probe_mcp_server.py` ③ 的漂移比对抓，运行期也**如实报失败**（见 `_run_one`）。
    """
    out: list[dict[str, Any]] = []
    for name in _EXPOSED:
        fn = _definition(name)
        if fn is None:
            logger.warning(
                "工具 %s 不在本进程的 TOOL_SCHEMAS 里（对应的 ENABLED 为假），"
                "本服务器不挂它。若后端那边挂了出去，多半是两边的 .env 不一致。",
                name,
            )
            continue
        out.append(fn)
    return out


def _exposed_names() -> tuple[str, ...]:
    """实际挂出去的名字，供错误文案用（与 `list_tools` 取自同一处）。"""
    return tuple(fn["name"] for fn in exposed_tools())


# ================================================================== 执行
def _new_context() -> ToolContext:
    """一次调用的 ToolContext。

    ⚠ `retriever=None`：见模块 docstring 第 2 条（只有检索工具会读它，而它没挂出去）。
    ⚠ `mode` / `rerank` 同样是**检索工具才读**的字段，这里填的是中性值 —— 它们在本进程里
      **永远不会被读到**，写什么都不影响行为。别因为「看着像配置」就去把它接到别处。
    ⚠ `seen` 每次调用**新建**：去重是**客户端**的事（`ctx.seen` 存在客户端那个 ctx 上，
      跨轮累积）。服务器这边若也去重，模型换个措辞重查同一件事会被判成「已执行过」。
    ⚠ `registry` 每次调用新建、**且这三个工具都不会往里加东西**（`new_sources` /
      `new_indices` / `known_indices` 恒空）。引用编号是**客户端本地**的概念，
      跨进程没有意义 —— 将来若有 MCP 路由的工具真填了这三个字段，
      **绝不能**把它并进客户端那个 `SourceRegistry`。
    """
    return ToolContext(
        registry=SourceRegistry(cap=settings.agent_max_sources),
        retriever=None,
        mode="hybrid",
        rerank=False,
        seen=set(),
        tool_names=_EXPOSED,
    )


async def _run_one(name: str, arguments: dict[str, Any]) -> ToolOutcome:
    """白名单 → `execute_tool()` → `ToolOutcome`。**本函数不抛异常。**

    「不抛」是硬要求，不是防御性编程：SDK 的处理器外面包着
    `except Exception → 把 str(e) 塞进一个 `isError=True` 的空结果`，而客户端那侧见到
    `isError` 会**进 30 秒冷却**（`mcp_bridge.call()`）—— 一次参数写错的调用会变成
    「三个工具全锁死半分钟」。所以一切失败都必须转成 `ok=False` 的**正常载荷**。

    白名单**必须是第一步**（模块 docstring 第 1 条）。
    """
    if name not in _EXPOSED:
        logger.warning(
            "收到未挂出的工具调用 %r（本服务器只挂 %s）", name, "、".join(_EXPOSED)
        )
        return ToolOutcome(
            f"错误：本服务器没有名为 {name} 的工具。可用工具："
            + "、".join(_exposed_names())
            + "。",
            ok=False,
            error="unknown_tool",
        )

    try:
        # ⚠ 走 JSON 字符串是**必须**的：`execute_tool()` 的入口就是 `json.loads(call.arguments)`，
        #   那条路与模型正常调用时**逐字相同**（参数类型、缺省键、`exclude_unset` 的语义
        #   全都由此保持一致）。直接改成「传 dict」的捷径会让服务器侧的 parse_args
        #   看到一份来源不同的字典，而两个方向的分支会因此慢慢漂移。
        call = ToolCall(id="mcp", name=name, arguments=json.dumps(arguments, ensure_ascii=False))
        return await execute_tool(call, _new_context())
    except Exception as exc:  # noqa: BLE001 —— 契约要求这里无论如何都不许抛
        # 正常路径上到不了（`execute_tool` 自己已经兜住了一切），留在这里是因为上面那几行
        # 也在 try 里：`json.dumps` 撞上非 JSON 值、`_new_context()` 里某个配置炸了……
        # 那些都不该让一次工具调用升格成「服务器报错」。
        logger.exception("工具 %s 执行异常", name)
        return ToolOutcome(f"工具执行失败（{exc}）。", ok=False, error=str(exc))


# ================================================================== 服务器
def build_server() -> Server:
    """lowlevel `Server` + 三个 handler。**纯构造，不碰网络、不连接任何东西**。

    ⚠ 探针 ① 会把这个函数造出来的 server 丢进
    `mcp.shared.memory.create_connected_server_and_client_session()` 做**进程内**断言
    —— 那条路与真 HTTP 传输**共用同一套 handler**，所以离线也能验「挂出去的是哪三个、
    schema 一不一样、白名单拦不拦得住」。
    """
    server: Server = Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "招投标问答系统的三个内部工具：交易知识图谱（实体与关系）、"
            "采购记录数据库（带条件的项目筛选与精确统计）、技能说明书（按需加载的流程文档）。"
        ),
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=fn["name"],
                description=fn.get("description"),
                # ⚠ 键名就是 MCP 的 `inputSchema`（驼峰），别改成 `input_schema`：
                #   `types.Tool` 的字段名如此，而且这个名字会出现在发给客户端的 JSON 里。
                inputSchema=fn["parameters"],
                # outputSchema **刻意不填**（模块 docstring 第 4 条）。
            )
            for fn in exposed_tools()
        ]

    @server.call_tool(validate_input=False)
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> tuple[list[types.ContentBlock], dict[str, Any]]:
        outcome = await _run_one(name, arguments or {})
        # 2-tuple = (非结构化正文, 结构化载荷)（模块 docstring 第 3 条）。
        # 正文用 `outcome.text` 逐字，与进程内那条路给模型看的是**同一句话**。
        return [types.TextContent(type="text", text=outcome.text)], outcome_to_payload(outcome)

    return server


def build_app(server: Server, path: str = _DEFAULT_PATH) -> Starlette:
    """把 server 包成一个只有 `path` 一条路由的 ASGI 应用。

    ⚠ `manager.run()` 必须在 **lifespan** 里 —— 它起的是承载所有会话的任务组，
      不进入它的请求会炸 `Task group is not initialized`。
    """
    manager = StreamableHTTPSessionManager(server)
    # ⚠ `stateless` 留默认（False）—— **这是实测过的结论，别再翻它**。
    #   客户端每次调用现连现断（`mcp_bridge.call()`），`terminate_on_close=True`（SDK 默认）
    #   会补一发 DELETE 把会话收干净；实测 5 轮调用 = 5 建 5 销，无会话泄漏。
    #   中途取消（用户点停止）确实会留下一段 `ClosedResourceError` traceback，
    #   但**两条分支都打**（`:182` 的 `logger.exception` 与 `:248` 的 `logger.error`），
    #   翻成 True 只是换措辞、traceback 一行不少。处置是 `_quiet_aborted_sessions()`。
    #   完整实测见模块 docstring「客户端中途断开」那一节。

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with manager.run():
            yield

    return Starlette(
        routes=[Route(path, StreamableHTTPASGIApp(manager))],
        lifespan=lifespan,
    )


# ================================================================== 启动
def _warmup() -> None:
    """预热两个 store。**各自吞异常**（`warmup()` 自己已经吞过，这层纯属兜底）。

    ⚠ 绝不在这里记 `state.error` 之类的东西 —— 那是让**整个服务**不可用的开关
      （`api.py` 的既有教训）。连不上只该降级成「那个工具如实报失败」。
    """
    for label, fn in (("知识图谱", graph_store.warmup), ("采购记录库", pg_store.warmup)):
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.warning("%s预热异常（已忽略，问答链路不受影响）", label, exc_info=True)


def _has_closed_resource(exc: BaseException | None) -> bool:
    """异常组里有没有 `anyio.ClosedResourceError`（沿组与 `__cause__` / `__context__` 找）。

    ⚠ 三个来源都要走：SDK 抛的是 `ExceptionGroup`，而它在 `__context__` 链上还有一层
      —— 只看 `.exceptions` 会漏（`mcp_bridge._leaf()` 就是只走组、于是回了一句
      「unhandled errors in a TaskGroup」那种没用的话）。
    """
    stack: list[BaseException] = [exc] if exc is not None else []
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, anyio.ClosedResourceError):
            return True
        stack.extend(getattr(cur, "exceptions", None) or ())
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)
        if cur.__context__ is not None:
            stack.append(cur.__context__)
    return False


class _AbortedSessionFilter(logging.Filter):
    """丢掉「客户端中途断开」那一种记录。理由见模块 docstring 那一节。

    ⚠ 过滤**只认异常类型**，不认消息文本 —— 按措辞判正是本仓库反复记过的那类判据
      （旧联网触发 ③ 死于它）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not _has_closed_resource(record.exc_info[1] if record.exc_info else None)


def _quiet_aborted_sessions() -> None:
    """给 SDK 那个 logger 挂上窄过滤器。**只滤中途断开，其余崩溃照打**。

    挂在 `mcp.server.streamable_http_manager` 这个**发记录的 logger 自己**身上，
    所以与传播、与 uvicorn 的 handler 配置都无关（`Logger.handle()` 先过 `self.filter`）。
    """
    manager_logger = logging.getLogger("mcp.server.streamable_http_manager")
    if not any(isinstance(f, _AbortedSessionFilter) for f in manager_logger.filters):
        manager_logger.addFilter(_AbortedSessionFilter())   # 幂等：main() 可能被重复调用


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数。**默认值全部从 `settings.mcp_server_url` 反解**。

    ⚠ 默认值和后端用的是**同一个字段**（`settings.mcp_server_bind()`），所以
      「后端连哪」与「服务器听哪」不会漂移 —— 两个字段（URL + host/port）必然漂移，
      而漂移的表现是「三个工具全挂」。要改就改 `.env` 里的 `MCP_SERVER_URL`，
      命令行只是**临时覆盖**（测试用，别写进部署步骤）。
    """
    host, port, path = settings.mcp_server_bind()
    parser = argparse.ArgumentParser(
        prog="python -m bidding_qa.mcp_server",
        description="招投标问答系统的自建 MCP 服务器（图谱 / 采购库 / 技能说明书三个工具）",
    )
    parser.add_argument("--host", default=host, help=f"监听地址（默认 {host}）")
    parser.add_argument("--port", type=int, default=port, help=f"监听端口（默认 {port}）")
    parser.add_argument("--path", default=path, help=f"端点路径（默认 {path}）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    _quiet_aborted_sessions()
    args = _parse_args(argv)

    # ⚠ `exposed_tools()` 只调一次：它对「在 _EXPOSED 里但不在 TOOL_SCHEMAS 里」的每个工具
    #   都打一行 WARNING，多调一次就是把同一句话再打一遍。
    names = tuple(fn["name"] for fn in exposed_tools())
    if not names:
        # 三个全都没挂出去 = 这个进程没有任何用处。不退出（那会让「服务器与后端必须同时
        # 用同一份 .env」这条变成一条硬性启动顺序要求），但要让日志里一眼看到。
        logger.warning("没有挂出任何工具 —— 检查 .env 里 NEO4J_URI / POSTGRES_HOST。")

    _warmup()

    logger.info(
        "自建 MCP 服务器将在 http://%s:%d%s 上监听，挂出 %d 个工具：%s",
        args.host,
        args.port,
        args.path,
        len(names),
        "、".join(names) or "（无）",
    )
    if args.host not in _LOOPBACK:
        # 本服务**没有任何认证**，而它直接握着 Neo4j / PostgreSQL 的凭据。
        logger.warning(
            "⚠ 正在绑定非回环地址 %s —— 本 MCP 服务器没有任何认证，"
            "同网段任何人都能调这三个工具（图谱/采购库查询）。"
            "只在本机用就把 --host 换回 127.0.0.1，或把 .env 的 MCP_SERVER_URL 改成回环。",
            args.host,
        )

    uvicorn.run(build_app(build_server(), args.path), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
