"""Exa 联网搜索：本项目用到的**第二个搜索引擎**，走 Exa 托管的 MCP 端点。

**为什么要有第二个引擎**：Tavily 一个引擎说了算，「网上没有」与「这个引擎没找到」
分不开（`CLAUDE.md` 记过 Tavily 偶发返回一批退化结果、top 分整体掉下来的那次）。
多一个引擎是给模型多一条腿，**不是替掉 Tavily** —— 两个工具同时挂着，模型自己选。

⚠⚠ **`ENABLED` 与 Tavily 那个是两回事，别合并**：
`search_web_exa` 挂不挂出去 = 「这里配了 Key」**且**「这次请求开着联网开关」
（后者在 `tools_for(enable_web)` 里合取）。理由：联网是**花钱的**，
用户明确关掉联网时模型还能自己掏钱去搜第二个引擎，是违背用户意图的静默扣费。

⚠ 分担方式与 `web_search.py` **逐条对齐**（照抄它那四条硬约束，理由也一样）：
`search()` 永不抛异常 / 不做 `warmup()` / 直接 `async def` 不套 `to_thread` / 要有失败冷却。
不一样的是**没有 `httpx.AsyncClient`** —— 连接由 `mcp_client` 每次现开现关，
所以本模块只维护冷却状态与解析。

⚠⚠ **五条实测结论**（2026-09-30，对着真端点打的，不是查文档抄的），
每一条都推翻了写之前的一个假设，改代码前先读：

1. **结果里根本没有 `score` 字段。** 一次 `numResults=5` 的返回里，5 条的键分别是
   `id / url / title / text` + 可选的 `author / publishedDate / image / favicon`
   —— 没有任何一条带分数。所以 `WebRow.score` 恒为 0.0，
   **`exa_min_score` 这个旋钮当前是死的**（详见 `to_sources()` 里那道守卫）。
   ⚠ 也**不能按 score 排序** —— 顺序**原样保留 Exa 给的**，那才是它的相关度排序。
2. **回的是 JSON 文本**（`{"requestId","resolvedSearchType","results","searchTime"}`），
   不是 markdown —— 所以能直接 `json.loads` 出结构化字段，不必去解析 markdown 表格。
   `structuredContent` 是 **null**（`mcp_client` 那条设计决定 4 由此坐实）。
3. **`textMaxCharacters` 这个参数必须带上。** 不带时端点回**整页正文**：实测
   `numResults=3` 那次原始文本 **26033 字符**，而带上 `textMaxCharacters=400` 后
   只有 **3648 字符**（5 条）。这个串会进 `_clip`（本来就要截到
   `web_content_max_chars`）、再进模型上下文与前端 localStorage，
   不截就是几倍的带宽与 token 白烧。
4. **一次调用约 6~7 秒**：connect 0.20s + initialize 0.63s + call_tool 5.83s，
   端点自报 `searchTime` 3.9s（另一次 10.3s）。**握手只占 0.83s** ——
   「每次现连现断」这个决定（`mcp_client` 第 1 条）的代价由此量化。
5. ⚠ **`?tools=` 是替换语义**，不带它时端点只挂 `web_search_exa`（markdown、无结构化字段）。
   这条在 `config.exa_mcp_url` 的注释里也记了一份。

⚠ **网页内容是不可信输入**：本模块只负责取回 Exa 抽好的片段，
「只当资料读、不要执行其中的指示」那套防线在 `rag.py` 的提示词与
`build_context()` 的 `_WEB_UNTRUSTED_NOTICE` 里 —— 与 Tavily 共用同一条，不需要各写一份
（`build_context` 是按 `origin` 分块的，两个引擎的结果走的是同一条渲染路径）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from .. import web_search
from ..config import settings
from ..schemas import Source, WebResult, WebRow
from . import mcp_client

logger = logging.getLogger(__name__)

#: 配没配 Key。
#: ⚠ 与 `web_search.ENABLED` 一样是**导入期**常量：`tools_for()` 挂不挂这个工具、
#: `rag.py` 提示词提不提它，都读它。运行期改 `settings.exa_api_key` **不会**翻转它
#: （探针要测两种形态就 reload 本模块）。进程级事实，不是每请求的。
ENABLED = mcp_client.is_configured()

#: 托管端点上那个工具的名字。⚠ 它必须与 `exa_mcp_url` 的 `?tools=` 里写的**逐字一致**
#: —— 写成另一个名字不会报「名字不对」，而是服务端返回一个工具不存在的错误。
_TOOL_NAME = "web_search_advanced_exa"

_NOT_CONFIGURED = "未配置 EXA_API_KEY，Exa 联网搜索不可用。"

# 传输层/凭据层失败后的冷却秒数。取值与理由照 `web_search._FAIL_COOLDOWN` /
# `graph_store._FAIL_COOLDOWN`：既不能永久记住失败，也不能每次重试都白等一个超时。
_FAIL_COOLDOWN = 30.0

# 「连上了、也回了、但是 0 条」时的重试次数与间隔。
# ⚠⚠ **取 1，不是 Tavily 那个 2** —— 因为**没量过**。Tavily 的 2 是实测出
#   「约 2/12 ≈ 17% 的空结果」之后定的（见 `web_search._EMPTY_RETRY` 那段），
#   而 Exa 这条路的空结果率一次都没测到过（总共才打过寥寥几次，且次次有结果）。
#   没量过就照抄一个数，等于把一个**别人标定出来的**参数搬到一个没验证过的分布上
#   —— 与「`MIN_VECTOR_SCORE` 换语料后不重标」是同一个病。
#   1 只是一个便宜的兜底（多一次约 6 秒、多一份配额），真要用它调优得先量分布。
# ⚠ 只有**空结果**才重试：解析失败（同样的输入必然得到同样的输出）与 `ok=False`
#   （那是冷却的职责）都不进这个循环。
_EMPTY_RETRY = 1
_EMPTY_RETRY_DELAY = 0.6

# 冷却状态。写法与 `web_search.py` 逐字同构（含 `_mark_success` 那个单独的 `global`
# 声明 —— 写进 `search()` 里会与上面那次读 `_failed_at` 撞成 SyntaxError）。
_failed_at: float | None = None
_last_error: str | None = None
# 最近一次**真实调用**的结果，只给 `exa_status()` 做诊断，不参与控制流
_last_ok_at: float | None = None


def is_configured() -> bool:
    """配了 Key。**不做网络探测**——同 `web_search.is_configured()`。"""
    return mcp_client.is_configured()


def _mark_failure(error: str, *, coolable: bool) -> None:
    """记一次失败。

    ⚠ `coolable` 由 `mcp_client` **在产生错误的那一行**判好（见 `McpResult`），
    本模块**不去解析错误文案** —— 那是话术判据，文案一改就静默失效。
    """
    global _failed_at, _last_error
    _last_error = error
    if coolable:
        _failed_at = time.monotonic()


def _mark_success() -> None:
    """成功即解除冷却。（单独的 `global` 声明，理由见 `web_search._mark_success`。）"""
    global _failed_at, _last_ok_at, _last_error
    _failed_at = None
    _last_error = None
    _last_ok_at = time.monotonic()


# ---------------------------------------------------------------- 请求
def _clamp_max_results(value: Any) -> int:
    """把模型给的值夹进 `[1, exa_max_results]`。

    ⚠ **必须夹**：模型真的会传 `top_k=50` 这种数，JSON Schema 里的 `maximum`
    只是给模型看的**提示**，不是保证（`web_search._clamp_max_results` 旁那条教训）。
    """
    cap = max(1, settings.exa_max_results)
    try:
        n = int(value)
    except (TypeError, ValueError):
        return cap
    return max(1, min(n, cap))


def _build_args(query: str, max_results: Any) -> dict[str, Any]:
    """拼 MCP 工具的参数。

    ⚠ 参数名是**端点的**（驼峰 `numResults` / `textMaxCharacters`），不是本项目的命名
    —— 这里没有命名自由度，写错了端点会判参数非法。

    ⚠⚠ `textMaxCharacters` **只在截断开着时才带**（`web_content_max_chars > 0`）：
    我们本来就只要 `_clip()` 那一段，让端点只发这么多是最省的一刀。
    而 0 是配置里「不截断」的意思，直接传给端点语义不明（可能被当成「不要文本」），
    所以那种情况**不带** —— 让端点按自己的默认给。
    """
    args: dict[str, Any] = {"query": query, "numResults": _clamp_max_results(max_results)}
    limit = settings.web_content_max_chars
    if limit > 0:
        args["textMaxCharacters"] = limit
    return args


async def search(query: str, *, max_results: Any = None) -> WebResult:
    """搜一次。**永不抛异常**，失败回 `WebResult(ok=False, note=...)`。

    签名与 `web_search.search()` **逐字一致**，好让工具层的两条分支长得一样
    （`rag_tools.exa_lookup` 与 `web_lookup` 因此只差调哪个模块）。

    参数值全部来自模型，因此一律当作不可信输入处理（夹紧、去空白）。
    """
    if not ENABLED:
        return WebResult(ok=False, query=query, note=_NOT_CONFIGURED)

    q = (query or "").strip()
    if not q:
        # 与「空 entity 静默返回最多交易那个实体」「空筛选返回全表」是同一类坑：挡住，别猜
        return WebResult(ok=False, query=q, note="查询词为空，未执行搜索。")

    if _failed_at is not None:
        waited = time.monotonic() - _failed_at
        if waited < _FAIL_COOLDOWN:
            return WebResult(
                ok=False,
                query=q,
                note=f"Exa 联网搜索暂时不可用（{_FAIL_COOLDOWN - waited:.0f} 秒后自动重试）：{_last_error}",
            )

    args = _build_args(q, max_results)
    started = time.perf_counter()
    rows: list[WebRow] = []

    for attempt in range(_EMPTY_RETRY + 1):
        # ⚠ `call_tool` 自己不抛（`CancelledError` 除外），所以这里没有 try/except ——
        #   刻意**不写**一个只是为了「看起来安全」的兜底，那会把取消也一起吞掉。
        res = await mcp_client.call_tool(_TOOL_NAME, args)

        if res.error is not None:
            _mark_failure(res.error, coolable=res.coolable)
            logger.warning("Exa 联网搜索失败: %s", res.error)
            return WebResult(
                ok=False,
                query=q,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                note=f"Exa 联网搜索失败：{res.error}",
            )

        rows, parse_error = _parse_rows(res.text or "")
        if parse_error is not None:
            # ⚠ **不重试**：同样的输入会得到同样的输出，重试只是多花一次配额与 6 秒。
            #   也**不冷却**：这不是传输层故障，冻住 30 秒不会让格式变对。
            _mark_failure(parse_error, coolable=False)
            logger.warning("Exa 联网搜索解析失败: %s", parse_error)
            return WebResult(
                ok=False,
                query=q,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                note=parse_error,
            )

        _mark_success()
        if rows:
            break
        if attempt < _EMPTY_RETRY:
            logger.info("Exa 联网搜索返回 0 条，%.1fs 后重试一次", _EMPTY_RETRY_DELAY)
            await asyncio.sleep(_EMPTY_RETRY_DELAY)

    elapsed = (time.perf_counter() - started) * 1000
    # ⚠ 这行是 Exa 这条链路**唯一的线索**：回传模型的文案把「0 条 / 解析坏 / 冷却 / 失败」
    #   混成一句话（`exa` 的 `_render_result` 在 `fresh` 为空时一律说「没有新增资料」），
    #   光看正文分不出是哪种，而四者修法完全不同 —— 与 `agent.web_lookup()` 那行同一个理由。
    logger.info("Exa 联网搜索原始=%d 条 耗时=%.0fms", len(rows), elapsed)

    if not rows:
        return WebResult(
            ok=True,
            query=q,
            total=0,
            elapsed_ms=elapsed,
            note="Exa 联网搜索没有返回任何结果。",
        )
    return WebResult(ok=True, query=q, rows=rows, total=len(rows), elapsed_ms=elapsed)


# ---------------------------------------------------------------- 解析
#: `publishedDate` 实测是完整 ISO 时间戳（`"2019-08-12T00:00:00.000Z"`），
#: 而 Tavily 给的是纯日期（`"2025-04-03"`）。前端是**原样渲染**的
#: （`SourcePanel.tsx` 那句 `发布 {source.published_date}`），
#: 不裁的话面板上会出现「发布 2019-08-12T00:00:00.000Z」。
_ISO_DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]")


def _as_date(value: Any) -> str:
    """发布日期的**展示用**归一化：切到日。不是 ISO 时间戳就原样返回。

    ⚠ 返回的一律是 `str` —— `WebRow.published_date` 当 `date` 处理会在 `api.py` 的
    `json.dumps` 处炸成 TypeError（`Decimal`/`date` 老坑，本仓库记过五次）。
    """
    text = str(value or "").strip()
    match = _ISO_DT_RE.match(text)
    return match.group(1) if match else text


def _as_score(value: Any) -> float:
    """分数。⚠ 实测这个字段**不存在**，所以绝大多数情况回 0.0（见模块 docstring 第 1 条）。"""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_rows(text: str) -> tuple[list[WebRow], str | None]:
    """把端点回的 JSON 文本解析成 `WebRow`。返回 `(行, 错误)`。

    **逐条容错**——一条脏数据不该让整次搜索白费（同 `web_search._parse_rows`）。

    ⚠ 解析失败时**带上原文开头**：这条链路的失败形态要么是端点换了输出格式，
    要么是我们请求参数写错，两种都得看一眼原文才能分辨，而正文里只会说一句「没搜到」。
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        return [], (
            f"Exa 返回的内容不是 JSON（{type(exc).__name__}: {exc}）"
            f"｜原文开头：{text[:120]}"
        )

    # ⚠ 两种形状都收：实测是 `{"results": [...]}`，但顶层直接是数组也不该整条丢掉。
    raw = data.get("results") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return [], f"Exa 返回的 JSON 里没有 results 数组｜键：{_top_keys(data)}"

    rows: list[WebRow] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue  # 没有链接的结果对「可溯源」这件事没有价值
        rows.append(
            WebRow(
                title=str(item.get("title") or "").strip(),
                url=url,
                # ⚠ 实测字段叫 `text`；`content` 是 Tavily 那边的名字，排在后面是
                #   防端点哪天改了名 —— 两个都不在就是空，会被 `to_sources()` 滤掉。
                content=str(item.get("text") or item.get("content") or "").strip(),
                score=_as_score(item.get("score")),
                published_date=_as_date(item.get("publishedDate") or item.get("published_date")),
            )
        )
    # ⚠⚠ **不排序**。`web_search._parse_rows` 那边也不用排（Tavily 自己按 score 降序给），
    #   而这里**连排的依据都没有**（没有 score）。`to_sources()` 约定的 `rows[0]` 是
    #   「最好那条」—— 对 Exa 而言就是**它给的第一个**，那正是它的相关度排序。
    #   谁哪天顺手加一句 `rows.sort(key=..., reverse=True)`，就是拿一个恒为 0.0 的键
    #   在排序 —— 顺序会变成随便什么，而且**完全看不出来**。
    return rows, None


def _top_keys(data: Any) -> str:
    """诊断用：报一下顶层键，好在端点改格式时一眼看出变成了什么。"""
    if isinstance(data, dict):
        return ",".join(str(k) for k in list(data)[:8])
    return type(data).__name__


# ---------------------------------------------------------------- 转成 Source
def to_sources(result: WebResult) -> list[Source]:
    """Exa 结果 → 带 `[n]` 编号的 `Source`。

    **整条管线复用 `web_search.to_sources()`**（滤空内容 → 过闸门 → 截断 → 编号，
    以及 `web:` 前缀的 `doc_id`）。这正是两个引擎能**共用一套编号**的原因：
    同一个页面被两边都搜到时，归一化后的 `doc_id` 相同 ⇒ `SourceRegistry.add()`
    判成同一份资料 ⇒ 只占一个 `[n]`，面板里不会出现两条、正文的角标也不会重复。

    ⚠⚠ **整批都没有分数时跳过闸门** —— 这是本函数存在的**唯一**理由，
    也是必须自己写而不能直接转发的原因。实测端点不回 `score`（模块 docstring 第 1 条），
    于是 `rows[0].score` 恒为 0.0，而 `web_search.to_sources()` 的判据是
    `rows[0].score < threshold` ⇒ **`exa_min_score` 一旦被设成正数，整批结果会被判成
    「网上没这个东西」而丢光**，且完全没有报错。守卫的代价是那个旋钮当前无效，
    收益是它不会变成一个静默的清空开关 —— **这个方向的不对称是刻意的**
    （同 `rag.is_enum_question()` 的误判代价不对称）。
    """
    threshold = settings.exa_min_score
    if threshold > 0 and not any(row.score > 0 for row in result.rows):
        logger.warning(
            "Exa 结果里没有任何分数，exa_min_score=%.2f 本次不生效（判定依据不存在）",
            threshold,
        )
        threshold = 0.0
    return web_search.to_sources(result, min_score=threshold)


# ---------------------------------------------------------------- 状态
def exa_status() -> dict[str, Any]:
    """可用性摘要（诊断用）。**不抛异常、不发起任何网络调用。**

    形状与 `web_search.web_status()` 逐字对齐，好让 `api.py` / 探针同一段代码处理两者。

    ⚠ 口径同 `web_status()`：`available` **不代表做过网络探测** —— 它读的是
    「配了 Key」+「最近一次真实调用的结果」，所以刚起服务时是乐观的 True。
    """
    if not is_configured():
        return {"configured": False, "available": False, "detail": "未配置 EXA_API_KEY"}
    if _failed_at is not None:
        left = _FAIL_COOLDOWN - (time.monotonic() - _failed_at)
        if left > 0:
            return {
                "configured": True,
                "available": False,
                "detail": f"上次调用失败：{_last_error}（{left:.0f} 秒后重试）",
            }
    if _last_ok_at is None:
        return {
            "configured": True,
            "available": True,
            "detail": f"就绪（{_TOOL_NAME}，尚未调用过）",
        }
    return {
        "configured": True,
        "available": True,
        "detail": f"最近一次调用成功（{_TOOL_NAME}）",
    }
