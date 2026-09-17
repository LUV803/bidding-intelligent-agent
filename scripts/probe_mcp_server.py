"""验证**自建 MCP 服务器**这一整套：`mcp_server.py` + `mcp/mcp_bridge.py` +
`tools/base.py` 的 `ToolOutcome` codec。

它钉的是一条**跨进程的契约**。三个工具搬进独立进程之后，最怕的不是「跑不起来」，
而是几种**形状完全正常**的坏：codec 漏了一个字段（前端那个块渲染成空白）、
白名单被绕过（服务器上以 `retriever=None` 执行检索工具）、`validate_input` 被后人改回默认
（中文自我纠正线索变成英文）、冷却被业务失败触发（模型查两次空结果就把工具锁死 30 秒）。
四种都没有日志、都没有异常，所以只能靠断言挡。

跑法：

    uv run python scripts/probe_mcp_server.py --offline   # ①②③④⑥关，不需要任何服务/Key
    uv run python scripts/probe_mcp_server.py             # 再加 ⑤ 与 ⑥开：起一个服务器子进程

⚠ **服务器不并入问答链路**（`mcp_server_url` 留空是默认且健康的状态），所以本脚本
**不会**去连 `settings.mcp_server_url` 指向的东西 —— ⑤ 自己挑一个空闲端口起一个子进程，
用完 kill 掉，跑完进程与文件都不留。想验「后端经 MCP 调工具」得走 `MCP_SERVER_URL=…`
+ 真打 `/api/agent`（见 CLAUDE.md 的「自建 MCP 服务器」一节）。

⚠⚠ ⑤ **必须**把子进程的输出写进**文件**（或派个线程读干管道）。原因不是洁癖：子进程的
logging 每条都 `flush()`，接进一个**没人读的管道**时写满约 4KB 缓冲后，那个 `write` 会
**阻塞它的事件循环线程**，整个服务器冻住 —— 表现是「前两次正常、第 3 次超时、之后永久
不可达」，与真的会话泄漏**长得一模一样**。这是**测试夹具的产物，不是服务器缺陷**
（实测：写文件 5/5、无人读的管道第 3 次起退化、有人读的管道 5/5）。完整实测见
`mcp_server.py` 的模块 docstring。

分组：

    ① 内存内服务器面   create_connected_server_and_client_session（与真传输**共用同一套
                       handler**）：工具名与顺序、inputSchema 逐字等于 TOOL_SCHEMAS、
                       白名单外的工具被拒、中文报错没被 English 化、正文走 content、
                       载荷走 structuredContent
    ② codec 往返       六种形状 + **字段集断言**（整个 codec 的支点）
    ③ 冷却             死端口：抛 BridgeError 不崩 / 文案带端点串 / 第二次**一次连接都不发**
    ④ 断流过滤器       纯离线真假两半：中途断开的记录被丢，其余崩溃照旧打出来
    ⑤ 真机             自己起进程 → 经 HTTP 真调三个工具（真数据：107 笔 / 973172.0 元）
                       → 漂移比对 → kill 掉看失败形态
    ⑥ 路由             **三个 `run()` 里那个 `if mcp_bridge.is_enabled():` 分支**。
                       关（URL 留空）：存储层替身被调到**恰好一次**、桥零次 —— 证明开关
                       关着时进程内路径仍然完整；开（真服务器）：数据来自远端、本地存储层
                       **一次都没被调到**。用替身而不断言数据，是因为「路由坏了却悄悄走回
                       进程内」的结果与正常**完全一样**，只有「谁被调用了」能把它分开。

⚠ 计划里的「③ 漂移」并进了 ⑤ —— 它比的是**跑着的那个服务器**，没有真进程就无从比。
⚠ ⑥ 的「开」那半**必须在 ⑤ 杀掉服务器之前**跑（它要的是活着的服务器），见 `_run()`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import fields
from pathlib import Path

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import anyio  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from bidding_qa import graph_store, mcp_server, pg_store, skill_store  # noqa: E402
from bidding_qa.clients import ToolCall  # noqa: E402
from bidding_qa.config import settings  # noqa: E402
from bidding_qa.mcp import mcp_bridge  # noqa: E402
from bidding_qa.schemas import GraphResult, GraphRow, PgResult, PgRow, Source  # noqa: E402
from bidding_qa.tools import base as base  # noqa: E402
from bidding_qa.tools import rag_tools as R  # noqa: E402

#: 死端口夹具。端口 9 是 TCP discard，本机没人监听 ⇒ 秒级 ECONNREFUSED。
#: ⚠ 别换成「随便一个高位端口」：那可能真有东西在听，而失败形态会变成协议错误
#:   （看着像「握手不对」），指向不到「服务器没起」。
DEAD_URL = "http://127.0.0.1:9/mcp"

#: 与 probe_pg_store.py / probe_graph_store.py 里那两个常量**互相印证**（同一笔账，
#: 两套独立技术栈各算一遍）。激光打印机：107 笔，中标金额合计 973172.0 元。
LASER_COUNT = 107
LASER_TOTAL = 973172.0

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
    if not ok:
        print(f"      got={got!r} want={want!r}")
    if detail:
        print(f"      {detail}")
    if not ok:
        failures.append(label)


def note(text: str) -> None:
    print(f"      {text}")


# ================================================================ ① 内存内服务器面
async def probe_inmemory() -> None:
    banner("① 内存内服务器面：三个工具、schema 逐字相同、白名单挡得住")

    present = tuple(fn["name"] for fn in mcp_server.exposed_tools())
    print(f"   _EXPOSED      = {mcp_server._EXPOSED}")
    print(f"   exposed_tools = {present}")
    print(f"   graph_store.ENABLED = {graph_store.ENABLED} / pg_store.ENABLED = {pg_store.ENABLED}")

    server = mcp_server.build_server()
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        listed = await session.list_tools()

        names = [t.name for t in listed.tools]
        check("①a list_tools 的工具名与顺序 == _EXPOSED", names, list(present))
        both_enabled = graph_store.ENABLED and pg_store.ENABLED
        if both_enabled:
            check("①b 两库都启用 ⇒ 三个工具全在", len(names), len(mcp_server._EXPOSED))
        else:
            note(
                f"⚠ 跳过 ①b：本机没开全（graph={graph_store.ENABLED} / pg={pg_store.ENABLED}），"
                f"只有 {len(names)} 个工具在场"
            )

        # schema 必须**逐字**等于 TOOL_SCHEMAS 里那一份 —— 这是 lowlevel Server 而不是
        # FastMCP 的全部理由（FastMCP 会从 Pydantic 重新推导，毁掉 Any + json_schema_extra
        # 那套容错夹紧与中文枚举报错）。
        # ⚠ TOOL_SCHEMAS 是 **OpenAI 形状**（`{"type":"function","function":{…}}`），
        #   不是 MCP 形状 —— 取 `function` 那一层才是 name/description/parameters。
        by_name = {fn["function"]["name"]: fn["function"] for fn in R.TOOL_SCHEMAS}
        for tool in listed.tools:
            want = by_name[tool.name]["parameters"]
            check(f"①c {tool.name}.inputSchema 逐字等于 TOOL_SCHEMAS", tool.inputSchema, want)
            check(f"①c {tool.name} 没有声明 outputSchema", tool.outputSchema, None)

        # ---- 白名单：不在 _EXPOSED 里的工具必须被拒 ----
        # 服务器进程一旦 import 了 rag_tools，注册表里就躺着**六个**工具。
        # `search_bidding_knowledge` 真被执行的话会拿到 `ctx.retriever=None`。
        pick = R.TOOL_SEARCH
        res = await session.call_tool(pick, {"query": "履约保证金"})
        payload = res.structuredContent or {}
        check("①d 白名单外的工具被拒（ok=False）", payload.get("ok"), False)
        check("①d 错误码是 unknown_tool", payload.get("error"), "unknown_tool")
        check("①d 正文点名它不存在", "本服务器没有名为" in payload.get("text", ""), True)
        check("①d 正文列出了可用工具", "可用工具：" in payload.get("text", ""), True)
        for n in mcp_server._EXPOSED:
            check(f"①d 可用工具里有 {n}", n in payload.get("text", ""), True)

        # ---- validate_input=False：中文自我纠正线索必须活着 ----
        # 真被 English 化的话，模型拿到的是 `'e_related' is not one of [...]`，
        # 「可用类型」那句带可用值列表的线索就没了。
        res = await session.call_tool(
            R.TOOL_GRAPH, {"query_type": "e_related", "entity": "x"}
        )
        payload = res.structuredContent or {}
        text = payload.get("text", "")
        check("①e 非法 query_type ⇒ 中文报错（没被 English 化）", "没有名为" in text, True)
        check("①e 报错里带可用类型列表", "可用类型：" in text, True)
        check("①e 报错里没有 pydantic 字样", "is not one of" in text, False)
        # ⚠ 这一条不需要 Neo4j：`graph_store.query()` 第一件事就是查 `_HANDLERS`，
        #   未知类型直接 `_unknown()` 返回，**压根不建连**。
        check("①e 未知查询类型不进冷却（不是传输层失败）", mcp_bridge._failed_at, None)

        # ---- 2-tuple 归一化：正文走 content、载荷走 structuredContent ----
        check("①f 载荷里带正文", payload.get("text", "") != "", True)
        blocks = [getattr(b, "text", "") for b in res.content]
        check("①f content[0].text == payload['text']", blocks[0] if blocks else "", payload.get("text", ""))


# ================================================================ ② codec 往返
def _shapes() -> list[tuple[str, base.ToolOutcome]]:
    graph = GraphResult(
        query_type="entity_search",
        query_label="实体检索",
        ok=True,
        elapsed_ms=18.4,
        entity="激光打印机",
        resolved="激光打印机",
        candidates=["打印机"],
        total=2,
        rows=[GraphRow(label="激光打印机", count=107, amount=973172.0)],
        extra={"笔数": 107.0},
    )
    pg = PgResult(
        query_label="采购记录（明细）",
        ok=True,
        elapsed_ms=7.9,
        mode="detail",
        filters={"标的物": "激光打印机"},
        total=107,
        rows=[
            PgRow(
                title="某项目",
                project="P-1",
                purchaser="某采购人",
                supplier="某中标人",
                published="2025-03-01",
                region="安徽省合肥市",
                subject="激光打印机",
                category="货物",
                source="政府采购",
            )
        ],
        truncated=True,
        extra={"命中记录数": 107.0, "中标金额合计": 973172.0},
    )
    source = Source(
        index=1,
        doc_id="qa-0001",
        question="履约保证金最高限额是多少？",
        answer="不得超过中标合同金额的 10%。",
        snippet="不得超过中标合同金额的 10%。",
        score=0.98,
        rrf_score=0.5,
        vector_score=0.9,
        bm25_score=42.0,
        matched_by=["vector", "bm25"],
    )
    return [
        ("纯文本成功（load_skill 那种，graph/pg 皆 None）", base.ToolOutcome("技能正文…", ok=True)),
        ("失败（ok=False + error）", base.ToolOutcome("错误：没有名为 x 的技能。", ok=False, error="bad_arguments")),
        ("图谱结果", base.ToolOutcome("图谱正文", ok=True, elapsed_ms=18.4, graph=graph)),
        ("采购库结果（Decimal/date 已转掉）", base.ToolOutcome("采购正文", ok=True, elapsed_ms=7.9, pg=pg)),
        (
            "撞上来源额度（dropped>0 + 编号）",
            base.ToolOutcome("匹配到 5 条，但额度已满。", ok=True, dropped=3, new_indices=[1, 2], known_indices=[4]),
        ),
        ("带 Source（三个工具恒空，但 codec 要能编）", base.ToolOutcome("检索正文", ok=True, new_sources=[source])),
    ]


def probe_codec() -> None:
    banner("② codec 往返：六种形状 + 字段集断言（整个 codec 的支点）")

    # ⚠⚠ 这一条是整个 codec 的支点，别删也别放宽成「除这三个以外」：
    #   日后往 ToolOutcome 加字段而忘了 codec，红在这里，而不是红在
    #   「前端那个块莫名其妙渲染成一片空白」。
    payload = base.outcome_to_payload(base.ToolOutcome("x", ok=True))
    check("②a 载荷字段集 == dataclass 字段集", set(payload), {f.name for f in fields(base.ToolOutcome)})

    for label, outcome in _shapes():
        wire = base.outcome_to_payload(outcome)
        # 真过一遍 JSON：MCP 协议上是文本，Decimal / date 会在这里炸（api.py 那个老坑）
        try:
            json.dumps(wire, ensure_ascii=False)
            jsonable = True
        except TypeError:
            jsonable = False
        check(f"②b 可 JSON 化：{label}", jsonable, True)
        check(f"②c 往返相等：{label}", base.outcome_from_payload(wire), outcome)

    # ---- 契约破坏时抛、缺可选字段时降级（两半都要钉） ----
    for missing in ("text", "ok"):
        wire = base.outcome_to_payload(base.ToolOutcome("x", ok=True))
        wire.pop(missing)
        try:
            base.outcome_from_payload(wire)
            raised = False
        except ValueError:
            raised = True
        check(f"②d 缺 {missing} ⇒ 抛 ValueError（不是空正文的假成功）", raised, True)

    lean = base.outcome_from_payload({"text": "只有两个必需字段", "ok": True})
    check("②e 缺可选字段 ⇒ 降级成默认值", (lean.error, lean.dropped, lean.elapsed_ms), (None, 0, 0.0))
    check("②e 降级后 graph/pg 是 None", (lean.graph, lean.pg), (None, None))

    # ⚠ 嵌套形状**故意不吞**：形状不对要抛 ValidationError，由 execute_tool 最后一道闸兜住。
    #   「解不出来」与「没这个字段」是两件事。
    try:
        base.outcome_from_payload({"text": "x", "ok": True, "pg": {"mode": "不存在的模式"}})
        raised = False
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__
    check("②f 嵌套形状不对 ⇒ 抛（不静默吞）", raised, "ValidationError")


# ================================================================ ③ 冷却
async def probe_cooldown() -> None:
    banner("③ 冷却：死端口三问（抛不崩 / 文案带端点串 / 第二次不再建连）")

    original_url = settings.mcp_server_url
    original_factory = mcp_bridge._http_client_factory
    attempts: list[float] = []

    def counting_factory(*args: object, **kwargs: object):  # noqa: ANN002, ANN003
        attempts.append(time.perf_counter())
        return original_factory(*args, **kwargs)  # type: ignore[arg-type]

    settings.mcp_server_url = DEAD_URL
    # ⚠ `call()` 经模块全局取它，所以换模块属性就足够了（不是实例方法）
    mcp_bridge._http_client_factory = counting_factory  # type: ignore[assignment]
    mcp_bridge._failed_at = None
    try:
        check("③a is_enabled() 为真（是传输失败，不是没配）", mcp_bridge.is_enabled(), True)

        t0 = time.perf_counter()
        try:
            await mcp_bridge.call(R.TOOL_PG, {"contains": "激光打印机", "max_rows": 1})
            first: BaseException | None = None
        except BaseException as exc:  # noqa: BLE001
            first = exc
        check("③b 失败抛的是 BridgeError（不是崩）", type(first).__name__, "BridgeError")
        text = str(first)
        check("③b 文案带端点串", DEAD_URL in text, True, detail=f"文案：{text}")
        check("③b 文案点名连不上", "连不上自建 MCP 服务器" in text, True)
        check("③b 没被那句「unhandled errors in a TaskGroup」噎住", "unhandled errors" in text, False)
        check("③c 第一次真的发起了一次连接", len(attempts), 1, detail=f"{time.perf_counter() - t0:.2f}s")
        check("③c 传输失败进了冷却", mcp_bridge._failed_at is not None, True)

        t0 = time.perf_counter()
        try:
            await mcp_bridge.call(R.TOOL_PG, {"contains": "激光打印机", "max_rows": 1})
            second: BaseException | None = None
        except BaseException as exc:  # noqa: BLE001
            second = exc
        elapsed = time.perf_counter() - t0
        check("③d 冷却期内照常抛 BridgeError", type(second).__name__, "BridgeError")
        check("③d 文案说明多少秒后重试", "秒后自动重试" in str(second), True)
        # ⚠ 这条是冷却的**全部意义**：不发请求。服务器没起时若不冷却，
        #   此后每次提问都要白等一个 12 秒连接超时，而结果永远是同一句「连不上」。
        check("③d 第二次一次连接都没发", len(attempts), 1, detail=f"用时 {elapsed * 1000:.1f}ms")
        check("③d 且是短路（不是又连了一次失败）", elapsed < 0.1, True)
    finally:
        settings.mcp_server_url = original_url
        mcp_bridge._http_client_factory = original_factory  # type: ignore[assignment]
        mcp_bridge._failed_at = None


# ================================================================ ④ 断流过滤器
def _record(exc: BaseException | None) -> logging.LogRecord:
    rec = logging.LogRecord(
        "mcp.server.streamable_http_manager", logging.ERROR, __file__, 1, "Session x crashed", None, None
    )
    if exc is not None:
        rec.exc_info = (type(exc), exc, exc.__traceback__)
    return rec


def probe_abort_filter() -> None:
    banner("④ 断流过滤器：中途断开被丢掉，其余崩溃照打（真假两半）")

    mcp_server._quiet_aborted_sessions()
    manager_logger = logging.getLogger("mcp.server.streamable_http_manager")
    attached = any(isinstance(f, mcp_server._AbortedSessionFilter) for f in manager_logger.filters)
    check("④a 过滤器已挂在 SDK 那个 logger 上", attached, True)
    mcp_server._quiet_aborted_sessions()  # 幂等（main() 可能被重复调用）
    again = sum(isinstance(f, mcp_server._AbortedSessionFilter) for f in manager_logger.filters)
    check("④a 重复挂载不会叠加", again, 1)

    # ⚠ 判据**只认异常类型，不认消息文本** —— 按措辞判是本仓库反复记过的那类判据
    #   （旧联网触发 ③ 死于它）。下面这六条把「认类型」这件事钉住。
    async def _aborted() -> None:
        raise anyio.ClosedResourceError()

    filt = mcp_server._AbortedSessionFilter()

    def dropped(exc: BaseException | None) -> bool:
        return not filt.filter(_record(exc))

    # ---- 该丢的那一半 ----
    check("④b 直接是 ClosedResourceError ⇒ 丢", dropped(anyio.ClosedResourceError()), True)
    check(
        "④b 异常组里包着 ⇒ 丢",
        dropped(ExceptionGroup("unhandled errors in a TaskGroup", [anyio.ClosedResourceError()])),
        True,
    )
    check(
        "④b 组里再套组 ⇒ 丢",
        dropped(
            ExceptionGroup(
                "外层",
                [ExceptionGroup("内层", [ValueError("x"), anyio.ClosedResourceError()])],
            )
        ),
        True,
    )
    # `__context__` 这条链**必须走**：SDK 抛的组在它上面还挂着一层。
    # （`mcp_bridge._leaf()` 就是只走组、于是回了一句「unhandled errors in a TaskGroup」
    #   —— 那是个记在案的缺口，处置见 mcp_bridge 的 docstring。）
    try:
        try:
            raise anyio.ClosedResourceError()
        except anyio.ClosedResourceError:
            raise RuntimeError("包装一层") from None
    except RuntimeError as wrapped:
        check("④b 经 __context__ 链够得着 ⇒ 丢", dropped(wrapped), True)

    # ---- 不该丢的那一半（防「过滤得太宽」）----
    check("④c 普通异常 ⇒ 照打", dropped(RuntimeError("数据库炸了")), False)
    check("④c 组里没有它 ⇒ 照打", dropped(ExceptionGroup("boom", [ValueError("x")])), False)
    check("④c 没有 exc_info 的记录 ⇒ 照打", dropped(None), False)
    check("④c 非异常记录（纯文本 ERROR）⇒ 照打", filt.filter(_record(None)), True)


# ================================================================ ⑤ 真机
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_ready(timeout: float = 60.0) -> bool:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        mcp_bridge._failed_at = None  # 探活要绕过冷却，否则第一次失败就把自己锁 30 秒
        if await mcp_bridge.list_tools(timeout=3.0):
            return True
        await asyncio.sleep(0.5)
    return False


async def probe_live() -> None:
    banner("⑤ 真机：自己起进程 → 经 HTTP 真调 → kill 掉看失败形态")

    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    original_url = settings.mcp_server_url
    original_factory = mcp_bridge._http_client_factory
    attempts: list[float] = []

    def counting_factory(*args: object, **kwargs: object):  # noqa: ANN002, ANN003
        attempts.append(time.perf_counter())
        return original_factory(*args, **kwargs)  # type: ignore[arg-type]

    settings.mcp_server_url = url
    mcp_bridge._http_client_factory = counting_factory  # type: ignore[assignment]
    mcp_bridge._failed_at = None

    with tempfile.TemporaryDirectory(prefix="probe_mcp_") as tmp:
        log_path = Path(tmp) / "server.log"
        logf = open(log_path, "w", encoding="utf-8")
        # ⚠⚠ 输出**必须**写文件（或派线程读干）。接进没人读的管道会冻住子进程 ——
        #   模块 docstring 里那一节就是这件事；写错的探针会自己复现那个假故障。
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "bidding_qa.mcp_server",
                "--host", "127.0.0.1", "--port", str(port),
            ],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=logf, stderr=subprocess.STDOUT,
        )
        try:
            await _probe_live_body(proc, port, url, attempts)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            logf.close()
            text = log_path.read_text(encoding="utf-8", errors="replace")
            _check_server_log(text)
            settings.mcp_server_url = original_url
            mcp_bridge._http_client_factory = original_factory  # type: ignore[assignment]
            mcp_bridge._failed_at = None


async def _probe_live_body(
    proc: subprocess.Popen, port: int, url: str, attempts: list[float]
) -> None:
    t0 = time.perf_counter()
    ready = await _wait_ready()
    if not ready:
        check(f"⑤a 服务器在 {port} 上就绪", False, True, detail="60 秒内没起来 —— 见下面那段子进程日志")
        return
    check(f"⑤a 服务器在 {port} 上就绪", True, True, detail=f"{time.perf_counter() - t0:.1f}s")

    # ---- ③ 漂移：跑着的那个服务器 vs 本进程的 TOOL_SCHEMAS ----
    live = await mcp_bridge.list_tools(timeout=5.0)
    live_names = [t["name"] for t in live]
    check("⑤b 漂移：线上工具名 == exposed_tools()", live_names, [fn["name"] for fn in mcp_server.exposed_tools()])
    by_name = {fn["function"]["name"]: fn["function"] for fn in R.TOOL_SCHEMAS}
    for tool in live:
        check(f"⑤b 漂移：{tool['name']}.inputSchema 与本地逐字相同", tool["inputSchema"], by_name[tool["name"]]["parameters"])

    # ---- 真数据：三个工具各调一次 ----
    if not pg_store.ENABLED:
        note("⚠ 跳过 ⑤c/⑤d：POSTGRES_HOST 为空，采购库未启用")
    else:
        payload = await mcp_bridge.call(
            R.TOOL_PG, {"contains": "激光打印机", "max_rows": 1}, timeout=20.0
        )
        pg = payload.get("pg") or {}
        check("⑤c 采购库经 MCP 查得 ok", payload.get("ok"), True, detail=str(payload.get("text", ""))[:120])
        check("⑤c total == 107（与 probe_pg_store/probe_graph_store 互印）", pg.get("total"), LASER_COUNT)
        check("⑤c 中标金额合计 == 973172.0", (pg.get("extra") or {}).get("中标金额合计"), LASER_TOTAL)
        # elapsed_ms 是**服务器侧**各 store 的时钟，不折传输耗时 —— 折进去会让同一个查询
        # 在「进程内」与「经 MCP」两条路径上报出不同的数，报告里那两行就没法互印了。
        check("⑤c elapsed_ms 来自载荷（服务器侧时钟）", payload.get("elapsed_ms", 0) > 0, True)

        # ---- 业务失败**绝不进冷却**：模型连查两次空结果后还得能换个词再查 ----
        mcp_bridge._failed_at = None
        payload = await mcp_bridge.call(
            R.TOOL_PG, {"contains": "这个词肯定查不到", "max_rows": 1}, timeout=20.0
        )
        check("⑤c 查不到 ≠ 传输失败（payload 正常返回）", payload.get("ok"), True)
        check("⑤d 业务失败不进冷却", mcp_bridge._failed_at, None)
        payload = await mcp_bridge.call(R.TOOL_PG, {"group_by": "星座"}, timeout=20.0)
        check("⑤d 非法筛选也是文本失败（ok=False）", payload.get("ok"), False)
        check("⑤d 非法筛选同样不进冷却", mcp_bridge._failed_at, None)

    if not graph_store.ENABLED:
        note("⚠ 跳过 ⑤e：NEO4J_URI 为空，图谱未启用")
    else:
        payload = await mcp_bridge.call(
            R.TOOL_GRAPH, {"query_type": "entity_search", "entity": "激光打印机"}, timeout=20.0
        )
        rows = ((payload.get("graph") or {}).get("rows") or [])
        check("⑤e 图谱经 MCP 查得 ok", payload.get("ok"), True, detail=str(payload.get("text", ""))[:120])
        # ⚠ 标签带**类型前缀**（`标的物·激光打印机`），不是光秃秃一个名字 —— 按 `==` 断会假红
        hit = [r for r in rows if "激光打印机" in r.get("label", "")]
        check("⑤e 命中「激光打印机」", len(hit), 1)
        # 两套独立技术栈算出同一个 107（Neo4j 这条 vs ⑤c 那条 PostgreSQL）——
        # 与 probe_pg_store.py 里那条「跨栈对账」是同一条账。
        check("⑤e 笔数 107，与采购库那条互印", hit[0].get("count") if hit else None, LASER_COUNT)
        check("⑤e graph 载荷经 JSON 往返完好", (payload.get("graph") or {}).get("query_type"), "entity_search")

    names = skill_store.available_names()
    if not names:
        note("⚠ 跳过 ⑤f：技能目录下没有 <名字>/SKILL.md")
    else:
        payload = await mcp_bridge.call(R.TOOL_SKILL, {"skill": names[0]}, timeout=20.0)
        check("⑤f 技能经 MCP 读得到", payload.get("ok"), True)
        check("⑤f 正文非空且被完整带回", len(payload.get("text", "")) > 200, True, detail=f"{len(payload.get('text', ''))} 字")

    # ---- 用户点「停止」：CancelledError 必须穿透，服务器必须照常服务 ----
    before = len(attempts)
    task = asyncio.create_task(
        mcp_bridge.call(R.TOOL_PG, {"contains": "激光打印机", "max_rows": 1}, timeout=10.0)
    )
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
        cancelled = False
    except asyncio.CancelledError:
        cancelled = True
    check("⑤g 中途取消 ⇒ CancelledError 原样穿透（没被吞成一次重试）", cancelled, True)
    await asyncio.sleep(0.4)

    mcp_bridge._failed_at = None
    aborted_attempts = len(attempts) - before
    payload = await mcp_bridge.call(R.TOOL_PG, {"contains": "激光打印机", "max_rows": 1}, timeout=20.0)
    check("⑤g 取消之后服务器照常服务", payload.get("ok"), True)
    note(f"（这一轮发起了 {aborted_attempts} 次连接，其中一次被取消）")

    # ---- ⑥ 路由（开）—— 必须排在杀服务器**之前**，它要的是活着的服务器 ----
    await _probe_routing_on()

    # ---- 服务器猝死：如实报失败 + 30 秒冷却，绝不静默走回进程内 ----
    proc.kill()
    proc.wait()
    await asyncio.sleep(0.3)
    mcp_bridge._failed_at = None
    sent = len(attempts)
    try:
        await mcp_bridge.call(R.TOOL_PG, {"contains": "激光打印机", "max_rows": 1}, timeout=5.0)
        died: BaseException | None = None
    except BaseException as exc:  # noqa: BLE001
        died = exc
    check("⑤h 服务器猝死 ⇒ 抛 BridgeError", type(died).__name__, "BridgeError")
    check("⑤h 文案里带端点串（唯一定位线索）", url in str(died), True, detail=str(died)[:160])
    check("⑤h 真的试过连接", len(attempts) > sent, True)

    t0 = time.perf_counter()
    try:
        await mcp_bridge.call(R.TOOL_PG, {"contains": "激光打印机", "max_rows": 1}, timeout=5.0)
    except BaseException:  # noqa: BLE001
        pass
    short = time.perf_counter() - t0
    check("⑤h 第二发命中冷却、不再建连", len(attempts), sent + 1, detail=f"用时 {short * 1000:.1f}ms")


def _check_server_log(text: str) -> None:
    """子进程日志：断流留下的 traceback 必须被过滤器吃掉，其余照旧。"""
    lines = text.splitlines()
    bad = [ln for ln in lines if "Traceback" in ln or "ClosedResourceError" in ln or "crashed" in ln]
    check("⑤i 服务器日志里没有断流 traceback（过滤器生效）", len(bad), 0, detail="\n      | ".join(bad[:6]))
    check("⑤i 日志没有被过滤干净（生命周期行还在）", "Created new transport" in text, True)
    check("⑤i 日志里 POST /mcp 有记录", "POST /mcp" in text, True)
    for line in lines[-8:]:
        note(f"| {line}")


# ================================================================ ⑥ 路由：三个 run() 的分支
class _Sentinel(Exception):
    """存储层替身抛出的哨兵。**不该逃出 `run()`** —— 三个工具（或它们外面那层
    `execute_tool`）都有 `except Exception`，这正是「工具执行永不抛」那条不变量。"""


class _Spy:
    """记录一次调用的替身。

    ⚠ 两条路用**两种**形态，别统一：
      - 「关」那半 `explode=True`：调用即抛。这样做是为了让断言**不需要任何服务** ——
        替身不返回真结果，就不必在探针里抄一份 `GraphResult` / `PgResult` 的构造
        （抄了就会在字段改动时假红），而且「进程内路径**确实被走到**」因此成为硬事实。
      - 「开」那半 `explode=False`：记录后**照常转调真函数**。此时若路由坏了、
        悄悄退回进程内，结果是**一个干净的『存储层被调用了 1 次』失败**，
        而不是一串「ok 怎么是 False」的次生混乱 —— 断言要指得准。
    """

    def __init__(self, name: str, *, explode: bool) -> None:
        self.name = name
        self.explode = explode
        self.calls: list[tuple] = []

    def __call__(self, *a: object, **kw: object) -> object:
        self.calls.append((a, kw))
        if self.explode:
            raise _Sentinel(f"{self.name} 在「URL 为空」时被直接调用了")
        return self.inner(*a, **kw)

    def bind(self, inner: object) -> _Spy:
        self.inner = inner  # type: ignore[attr-defined]
        return self


def _client_ctx() -> base.ToolContext:
    """客户端侧的 `ToolContext`。

    这三个工具**都不读** registry / retriever / mode / rerank，但它们是**必填字段**
    （没有默认值，见 `base.ToolContext`），所以照实填一份中性值 —— 与
    `mcp_server._new_context()` 那份是同一个形状，不共用是因为它们分属两侧。
    """
    return base.ToolContext(
        registry=base.SourceRegistry(cap=settings.agent_max_sources),
        retriever=None,
        mode="hybrid",
        rerank=False,
    )


async def _call(
    name: str, args: dict, ctx: base.ToolContext | None = None
) -> base.ToolOutcome:
    """走**生产入口** `execute_tool()`（不是直接 `tool.run()`）—— 去重、参数解析、
    错误兜底全在里面，而「校验与去重留在分支前面」正是要验的结论之一。"""
    call = ToolCall(id="probe", name=name, arguments=json.dumps(args, ensure_ascii=False))
    return await base.execute_tool(call, ctx or _client_ctx())


def _patch_stores(spies: dict[str, _Spy]) -> tuple[object, ...]:
    originals = (graph_store.query, pg_store.query, skill_store.load)
    graph_store.query = spies["graph"]        # type: ignore[assignment]
    pg_store.query = spies["pg"]              # type: ignore[assignment]
    skill_store.load = spies["skill"]         # type: ignore[assignment]
    return originals


def _restore_stores(originals: tuple[object, ...]) -> None:
    graph_store.query, pg_store.query, skill_store.load = originals


async def probe_routing_off() -> None:
    banner("⑥ 路由（关）：mcp_server_url 为空 ⇒ 三个工具走进程内，桥一次都不碰")

    original_url = settings.mcp_server_url
    spies = {
        "graph": _Spy("graph_store.query", explode=True),
        "pg": _Spy("pg_store.query", explode=True),
        "skill": _Spy("skill_store.load", explode=True),
    }
    bridge_spy = _Spy("mcp_bridge.call", explode=True)
    originals = _patch_stores(spies)
    original_call = mcp_bridge.call
    # 判据就是这一个字段：``is_enabled()`` 读它，三个 run() 也都读它。
    settings.mcp_server_url = ""
    mcp_bridge.call = bridge_spy  # type: ignore[assignment]
    try:
        check("⑥ 关：is_enabled() 为假", mcp_bridge.is_enabled(), False)
        cases = [
            (R.TOOL_GRAPH, {"query_type": "entity_search", "entity": "激光打印机"}, "graph"),
            (R.TOOL_PG, {"contains": "激光打印机"}, "pg"),
            (R.TOOL_SKILL, {"skill": "bid-analysis"}, "skill"),
        ]
        for name, args, which in cases:
            out = await _call(name, args)
            check(f"⑥ 关：{name} 走进程内（本地存储层被调到一次）",
                  len(spies[which].calls), 1)
            check(f"⑥ 关：{name} 没有走桥", len(bridge_spy.calls), 0)
            # 替身抛的哨兵必须被收住 —— 逃出去就是「工具执行永不抛」破了。
            check(f"⑥ 关：{name} 把哨兵收成了 ok=False", out.ok, False,
                  detail=str(out.text)[:100])
    finally:
        settings.mcp_server_url = original_url
        mcp_bridge.call = original_call  # type: ignore[assignment]
        _restore_stores(originals)


async def _probe_routing_on() -> None:
    """真机那半：URL 指着**正在跑的**服务器 ⇒ 必须走 MCP，本地存储层一次都不碰。

    ⚠⚠ 这是「经 MCP 执行」的**唯一凭证**。少了它，整个改造在探针眼里与没做一样 ——
    三条 `if mcp_bridge.is_enabled():` 若被谁改成恒假（或判据写错），
    ⑤ 那一组**照样全绿**（它直接调 `mcp_bridge.call()`，本来就不经过 `run()`）。
    """
    spies = {
        "graph": _Spy("graph_store.query", explode=False).bind(graph_store.query),
        "pg": _Spy("pg_store.query", explode=False).bind(pg_store.query),
        "skill": _Spy("skill_store.load", explode=False).bind(skill_store.load),
    }
    originals = _patch_stores(spies)
    try:
        ctx = _client_ctx()
        out_g = await _call(
            R.TOOL_GRAPH, {"query_type": "entity_search", "entity": "激光打印机"}, ctx
        )
        check("⑥ 开：图谱结果 ok", out_g.ok, True, detail=str(out_g.text)[:120])
        # ⚠ `total` 是**行数**（命中几个实体），不是笔数 —— entity_search 命中的是
        #   「标的物·激光打印机」这**一个**实体，107 在那一行的 `count` 上。两者别混。
        rows = (out_g.graph.rows if out_g.graph else []) or []
        check("⑥ 开：图谱 total=1（命中一个实体）",
              (out_g.graph.total if out_g.graph else None), 1)
        check("⑥ 开：那个实体的 count=107（与 ⑤c/⑤e 同一笔账）",
              (rows[0].count if rows else None), LASER_COUNT)
        check("⑥ 开：失败码为空（业务成功才不置 error）", out_g.error, None)

        out_p = await _call(R.TOOL_PG, {"contains": "激光打印机", "max_rows": 1}, ctx)
        check("⑥ 开：采购库结果 ok", out_p.ok, True, detail=str(out_p.text)[:120])
        check("⑥ 开：采购库 total=107", (out_p.pg.total if out_p.pg else None), LASER_COUNT)
        check("⑥ 开：采购库金额合计 973172.0（过 JSON 没丢精度）",
              ((out_p.pg.extra or {}).get("中标金额合计") if out_p.pg else None), LASER_TOTAL)

        out_s = await _call(R.TOOL_SKILL, {"skill": "bid-analysis"}, ctx)
        check("⑥ 开：技能正文经 MCP 取回", out_s.ok and len(out_s.text) > 200, True,
              detail=f"{len(out_s.text)} 字符")

        # ⚠ `elapsed_ms` 必须来自 **payload**（服务器侧的存储时钟），不是本地传输耗时。
        #   ⚠⚠ 只对**计时的那两个**断言 > 0：`LoadSkillTool` 的成功路径**本来就**不设这个
        #   字段（本地那份也是 0.0 —— 读的是本地 9K 文本，见该工具里那句注释）。
        #   所以 `out_s.elapsed_ms == 0.0` 恰恰是**忠实往返**的凭证：codec 没给它编一个数。
        #   别顺手写成「三个都 > 0」—— 那会逼着去改产品代码，把一条既有设计当 bug 修。
        check("⑥ 开：图谱 elapsed_ms > 0（来自载荷而非本地计时）", out_g.elapsed_ms > 0, True,
              detail=f"{out_g.elapsed_ms}ms")
        check("⑥ 开：采购库 elapsed_ms > 0", out_p.elapsed_ms > 0, True,
              detail=f"{out_p.elapsed_ms}ms")
        check("⑥ 开：技能工具未计时 ⇒ 0.0（本地路径同样不设，忠实往返）",
              out_s.elapsed_ms, 0.0)

        # ⚠⚠ 去重仍在分支**前面**：同一个 ctx 里重调同一件事必须被判重复。
        #   反过来的话（分支被提到去重之前），模型换个措辞重查会真打一次远端 ——
        #   白花一次往返，而**结果看起来完全正常**。
        again = await _call(
            R.TOOL_GRAPH, {"query_type": "entity_search", "entity": "激光打印机"}, ctx
        )
        check("⑥ 开：同一 ctx 重复调用 ⇒ 判为已执行过", "已执行过" in again.text, True,
              detail=str(again.text)[:100])

        for which, spy in spies.items():
            check(f"⑥ 开：{spy.name} 一次都没被本地调到", len(spy.calls), 0)
    finally:
        _restore_stores(originals)


# ================================================================ main
async def _run(offline: bool) -> None:
    await probe_inmemory()
    probe_codec()
    await probe_cooldown()
    probe_abort_filter()
    await probe_routing_off()
    if offline:
        print("\n--offline：跳过 ⑤ 与 ⑥开（要起子进程、连 Neo4j / PostgreSQL）")
        return
    await probe_live()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证自建 MCP 服务器（mcp_server.py + mcp_bridge）")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="只跑 ①②③④ 与 ⑥关（不需要任何服务）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    print(
        f"MCP_SERVER_URL = {settings.mcp_server_url!r}"
        "（⑥关 会临时置空并在 finally 还原；⑤ 自己起进程、用空闲端口）"
    )
    print(f"graph_store.ENABLED = {graph_store.ENABLED} / pg_store.ENABLED = {pg_store.ENABLED}")
    print(f"技能 = {skill_store.available_names()}")
    print(f"python = {sys.executable}")

    asyncio.run(_run(args.offline))

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
