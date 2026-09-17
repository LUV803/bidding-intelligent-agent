"""FastAPI 服务：/api/chat 流式问答 + /api/search 纯检索 + 健康检查。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from . import conv_store, web_search
# ⚠ 这里只 import 还需要的那几个。原先还有 TOOL_WEB / recover_tool_calls / tools_for，
#   它们只被已删除的 `_recover_web_query()`（`/api/chat` 的泄漏恢复）用；
#   那套机制在 `/api/agent` 上仍然保留，那边自己 import。
from .agent import SourceRegistry, run_agent_stream, web_lookup
from .config import settings
from .conv_store import ConvStoreUnavailable, conv_status
from .graph_store import graph_status
from .pg_store import pg_status
from .clients import LLMNotConfiguredError, available_providers, get_llm, resolve_provider
from .rag import stream_answer, with_image_context
from .retriever import get_retriever
from .schemas import (
    ChatRequest,
    ConfigResponse,
    ConversationFull,
    ConversationHit,
    ConversationMeta,
    ConversationUpsert,
    FeedbackRow,
    FeedbackUpsert,
    HealthResponse,
    SearchRequest,
    SearchResponse,
    Source,
    VisionRequest,
    VisionResult,
)
from .clients.vision_client import VisionNotConfiguredError, analyze, vision_status
from .web_search import web_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class AppState:
    """记录后台初始化进度，避免阻塞服务启动。"""

    def __init__(self) -> None:
        self.ready = False
        self.error: str | None = None
        self.stage = "starting"


state = AppState()


async def _bootstrap() -> None:
    try:
        state.stage = "loading_index"
        retriever = await asyncio.to_thread(get_retriever)
        await asyncio.to_thread(retriever.load)

        state.stage = "loading_model"
        from .embedding import warmup
        from .reranker import warmup as rerank_warmup

        await asyncio.to_thread(warmup)
        await asyncio.to_thread(rerank_warmup)

        # 知识图谱预热：套一层自己的 try，**绝不写 state.error**。
        # 上面那个 try 里任何异常都会让 state.ready 保持 False → /api/health 503 →
        # 整个服务不可用。图谱是可选能力，连不上只能降级成「图谱工具报告失败」。
        # 另外新 driver 的首个查询有约 2 秒的连接建立开销（实测 2047ms，之后 15ms），
        # 在这里付掉，用户第一次问图谱问题时不必等。
        state.stage = "connecting_graph"
        try:
            from .graph_store import warmup as graph_warmup

            await asyncio.to_thread(graph_warmup)
        except Exception:  # noqa: BLE001 - graph_warmup 自己已吞异常，这层纯属兜底
            logger.warning("知识图谱预热异常（已忽略，问答链路不受影响）", exc_info=True)

        # 采购记录库预热：同上，**套自己的 try、绝不写 state.error**。区别是它是本机的，
        # 建连只要几毫秒，所以顺手在 warmup() 里探一下表在不在——别等演示到一半才发现
        # 表被删了或库名写错（那时图谱工具已经在降级，两个工具一起哑掉很难查）。
        state.stage = "connecting_postgres"
        try:
            from .pg_store import warmup as pg_warmup

            await asyncio.to_thread(pg_warmup)
        except Exception:  # noqa: BLE001 - pg_warmup 自己已吞异常，这层纯属兜底
            logger.warning("采购记录库预热异常（已忽略，问答链路不受影响）", exc_info=True)

        # 对话历史库预热：同上，**套自己的 try、绝不写 state.error**。
        # 它顺手把表建出来（conv_store.warmup → _get_conn → ensure_schema），
        # 所以冷启动的第一个请求不会撞上「表还不存在」。
        # ⚠ 建表**不依赖这一处**：conv_store 自己也在每次取连接时懒确保。
        #   上面那个外层 try 一旦失败（索引炸了），内层的三个 try 根本不会执行 ——
        #   而历史与索引无关，那时它仍然必须能用。
        state.stage = "connecting_history"
        try:
            from .conv_store import warmup as conv_warmup

            await asyncio.to_thread(conv_warmup)
        except Exception:  # noqa: BLE001 - conv_warmup 自己已吞异常，这层纯属兜底
            logger.warning("对话历史库预热异常（已忽略，问答链路不受影响）", exc_info=True)

        state.stage = "ready"
        state.ready = True
        logger.info("服务初始化完成")
    except Exception as exc:  # noqa: BLE001 - 启动期异常需要暴露给 /api/health
        state.error = str(exc)
        state.stage = "failed"
        logger.exception("服务初始化失败")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_bootstrap())
    yield
    task.cancel()


app = FastAPI(
    title="招投标智能问答机器人",
    description="Qdrant 向量检索 + BM25 关键词检索 混合召回，DeepSeek 生成带来源引用的回答",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 限流
# slowapi：每个 IP 每分钟最多 `settings.rate_limit_per_minute` 次（0 = 关掉）。
#
# ⚠⚠ 有**两个**中间件，选错了会在流式响应上炸：
#   - `SlowAPIMiddleware`（BaseHTTPMiddleware）—— 本文件用的是这个；
#   - `SlowAPIASGIMiddleware`（纯 ASGI）—— **不能用**。它的 send_wrapper 每收到一个
#     `http.response.body` 就**重发一次 `http.response.start`**（只对「一个 body 就结束」
#     的普通响应成立）。而本服务的三条问答链路全是 SSE，有几百上千个 body 分片 ——
#     重发 start 是 ASGI 协议违例。
#
# ⚠ 429 处理器**必须是同步的 `def`**：`SlowAPIMiddleware` 走的是 slowapi 的
#   `sync_check_limits`，那里对协程处理器会**静默**退回英文默认文案
#   （`_rate_limit_exceeded_handler`），不报错、只是文案变成英文。
# ⚠ 自己补 `Retry-After`：slowapi 在中间件这条路径上**不注入**任何限流响应头
#   （`sync_check_limits` 出错时返回 `should_inject_headers=False`），
#   所以不写这一行客户端拿到的是一个光秃秃的 429、无从知道该等多久。
def _rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": "60"},
        content={
            "detail": f"请求过于频繁：本端点每个 IP 每分钟最多 {settings.rate_limit_per_minute} 次，请稍后重试"
        },
    )


# ⚠⚠ 桶键是 `(key_func() 的结果, 端点函数名)` —— slowapi 的 `_endpoint_key` 默认取
#   `f"{func.__module__}.{func.__name__}"`（`key_style="url"` 才按路径分桶）。
#   所以限额是**每个端点**各自 120/分钟，**不是全站合计**。
#   真机验过：把 /api/search 打满 429 之后，/api/config 照样 200（它有自己的桶）。
# ⚠ `get_remote_address` 取的是 `request.client.host`。本服务单用户、无反向代理，
#   所以每个端点的桶实际上只装本机那一个 IP —— 够挡失控循环，不是多租户限流。
_limits = [f"{settings.rate_limit_per_minute}/minute"] if settings.rate_limit_per_minute > 0 else []
limiter = Limiter(key_func=get_remote_address, default_limits=_limits, enabled=bool(_limits))
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limited)
if _limits:
    app.add_middleware(SlowAPIMiddleware)


def _require_ready() -> None:
    if state.ready:
        return
    if state.error:
        raise HTTPException(status_code=503, detail=f"服务初始化失败: {state.error}")
    raise HTTPException(status_code=503, detail=f"服务正在初始化（{state.stage}），请稍后重试")


# ---------------------------------------------------------------- 健康检查
# ⚠ 健康检查**刻意豁免限流**，两个理由都不是洁癖：
#   ① 前端 `page.tsx` 的轮询间隔是 `status === "ok" ? 30000 : 4000` —— 健康检查一旦被
#      429，`status` 就不再是 "ok"，前端会把轮询**加快到每 4 秒一次**，正好去撞更紧的限流，
#      是个正反馈。② 它不调大模型、不碰外部 API，是整个服务里最便宜的一个端点，
#      限它只会让「后端没连上」的假象出现在界面上。
@limiter.exempt
@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    retriever = get_retriever() if state.ready else None
    # 图谱/数据库/视觉状态只读内部变量，不触发连接、不抛异常——健康检查不能被可选能力拖垮
    graph = graph_status()
    pg = pg_status()
    vision = vision_status()
    # 联网同理，但**多一维**：这里只能报告「服务端配没配 Key」。用户这次开没开前端开关
    # 只存在于请求体里（ChatRequest.web_search），健康检查看不见，也不该看见。
    web = web_status()
    history = conv_status()
    # 文本大模型同理也是**两维**：`llm_provider` / `llm_providers` 报的是**服务端默认**那家
    # 与「哪些配了 Key」，而「这次用哪家」只在请求体里（`ChatRequest.llm_provider`）。
    # 所以 `llm_model` / `llm_ready` 说的**只是默认那家**，别当成「这次实际用的」。
    # （原先这里 `get_llm()` 调了两次，顺手合成一个局部变量。）
    default_llm = get_llm()
    return HealthResponse(
        status="ok" if state.ready else (state.error or state.stage),
        collection=settings.qdrant_collection,
        qdrant_mode=retriever.store.mode if retriever else "unknown",
        qdrant_points=retriever.store.count() if retriever else 0,
        bm25_docs=len(retriever.bm25) if retriever else 0,
        embedding_model=settings.embedding_model,
        vector_size=settings.qdrant_vector_size,
        llm_model=default_llm.model,
        llm_ready=default_llm.ready,
        llm_provider=resolve_provider(),
        llm_providers=available_providers(),
        rerank_enabled=settings.rerank_enabled,
        rerank_model=settings.rerank_model,
        graph_configured=bool(graph["configured"]),
        graph_available=bool(graph["available"]),
        graph_detail=str(graph["detail"]),
        pg_configured=bool(pg["configured"]),
        pg_available=bool(pg["available"]),
        pg_detail=str(pg["detail"]),
        vision_configured=bool(vision["configured"]),
        vision_available=bool(vision["available"]),
        vision_detail=str(vision["detail"]),
        vision_model=settings.zhipu_vision_model,
        web_configured=bool(web["configured"]),
        web_available=bool(web["available"]),
        web_detail=str(web["detail"]),
        history_configured=bool(history["configured"]),
        history_available=bool(history["available"]),
        history_detail=str(history["detail"]),
    )


@app.get("/api/config", response_model=ConfigResponse)
async def config() -> ConfigResponse:
    return ConfigResponse(
        llm_model=get_llm().model,
        embedding_model=settings.embedding_model,
        vector_size=settings.qdrant_vector_size,
        top_k=settings.top_k,
        hybrid=True,
        rerank=settings.rerank_enabled,
        rerank_model=settings.rerank_model,
        qdrant_mode=get_retriever().store.mode if state.ready else "unknown",
        collection=settings.qdrant_collection,
    )


# ---------------------------------------------------------------- 纯检索
@app.post("/api/search", response_model=SearchResponse)
async def search(payload: SearchRequest) -> SearchResponse:
    _require_ready()
    if not payload.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")

    retriever = get_retriever()
    started = time.perf_counter()
    sources = await asyncio.to_thread(
        retriever.search, payload.query, payload.top_k, payload.mode, payload.rerank
    )
    latency = retriever.last_latency
    latency["request_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return SearchResponse(query=payload.query, mode=payload.mode, sources=sources, latency_ms=latency)


# ---------------------------------------------------------------- 图片解析
# ⚠ **刻意不调用 _require_ready()**：图片解析完全不碰 Qdrant/BM25/向量模型，把它挂在索引
#   加载上，等于让一个独立能力被核心链路拖死——与 graph_store / pg_store 的降级精神相悖。
#   （_require_ready() 只被 /api/search、/api/chat、/api/agent 调用。）
#
# ⚠ 也**刻意不在 _bootstrap() 里预热**（图谱和采购库都预热了）：视觉能力没有任何「冷连接」
#   成本，不像 Neo4j 首查要付 2 秒握手。预热只会多一次网络往返、多花一次钱，还会在启动
#   日志里制造一条无意义的失败。别「补全」它。
@app.post("/api/vision", response_model=VisionResult)
async def vision(payload: VisionRequest) -> VisionResult:
    """解析一张图片，返回结构化字段。普通 JSON，**不是 SSE**——解析是一次性的，
    前端在附件 chip 上显示「解析中…」即可，不需要流式。"""
    image = payload.image_base64.strip()
    if image.startswith("data:"):
        # 前端已经会切掉前缀（见 schemas.VisionRequest 的注释）。这里**仍然兼容**带前缀的
        # 形态，只是记一条警告：手搓 curl 时很容易忘记，而为一个纯装饰性的差异返回 400
        # 对排查毫无帮助。SDK 内部自己也会剥（drop_prefix_image_data），两边行为一致。
        logger.warning("图片带了 data: 前缀，已剥离（前后端约定是裸 base64，请检查调用方）")
        image = image.split("base64,", 1)[-1].strip()
    if not image:
        raise HTTPException(status_code=400, detail="image_base64 不能为空")

    # base64 比原图大约 4/3，所以按**解码后**的字节数比对：前端也是拿 File.size 跟同一个
    # 上限比的，判据必须一致，否则前端按 10MB 放行的图（编码后 13.3MB）会被这里挡下来。
    approx_bytes = len(image) * 3 // 4
    if approx_bytes > settings.vision_max_image_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"图片过大（约 {approx_bytes / 1048576:.1f}MB），"
                f"上限 {settings.vision_max_image_bytes / 1048576:.0f}MB。请压缩后重试。"
            ),
        )

    try:
        # SDK 只有同步客户端（httpx.Client，没有 AsyncZhipuAI），所以包一层 to_thread——
        # 与 retriever / graph_store 的调用方式一致。
        return await asyncio.to_thread(analyze, image, payload.media_type)
    except VisionNotConfiguredError as exc:
        # 这里是**唯一**能给 HTTP 状态码的地方：还没开始流式，不存在 _sse 那个
        # 「响应头已经 200 发出去了」的约束（对比 /api/chat 里 LLMNotConfiguredError 的处置）。
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------- 对话历史
# 前端的对话（会话 + 消息）持久化在 PostgreSQL 里，localStorage 降为**降级回退**。
# 后端只是存取，**不解释消息内容**（payload 原样是一坨 jsonb）。
#
# ⚠⚠ **这五条都刻意不调用 `_require_ready()`** —— 照 `/api/vision` 的先例，但对历史来说
#   这条**更硬**：索引 + 向量模型加载要几十秒，那期间 `/api/health` 是 503。若把历史也挂在
#   `state.ready` 上，用户重启后端后的头几十秒打开页面会**看不到任何历史**，而 localStorage
#   版本是**秒出**的 —— 那是明确的退步。历史与 Qdrant / BM25 毫无关系。
#
# ⚠⚠ **路由声明顺序**：`/api/conversations/search` 必须写在 `/api/conversations/{id}` 的
#   **前面**。FastAPI 按声明顺序匹配，反了的话 `search` 会被当成一个对话 id。
#   （PUT / DELETE 是别的方法，不冲突；只有这两个 GET 冲突。）
#
# ⚠ 错误码**两分**，别合并：`ConvStoreUnavailable` → **503**（服务这一侧的能力不可用）、
#   `get_conversation` 回 `None` → **404**（这条对话不存在）。都塌成 503 的话前端分不出
#   「这条在别处被删了」和「数据库挂了」，而前者**不该**触发整体降级。
#   这也是本仓库第一次抛 404（此前只有 400 / 413 / 503）。
#
# ⚠ 失败**不要**返回 200 + `ok:false`：`lib/api.ts` 里所有辅助函数都是
#   `if (!response.ok) throw`（`readError` 优先取 `detail`），返回 200 会让它们以为成功。
#
# ⚠ 返回值照 `/api/reindex` 的成例用普通 dict：PUT / DELETE 的回执前端根本不看
#   （它只 care `response.ok`），为它造一个模型是多余的。


def _conv_unavailable(exc: ConvStoreUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@app.get("/api/conversations", response_model=list[ConversationMeta])
async def list_conversations(limit: int = 100) -> list[ConversationMeta]:
    """列出对话的**元数据**（不含正文），按最近更新倒序。"""
    try:
        return await asyncio.to_thread(conv_store.list_conversations, limit)
    except ConvStoreUnavailable as exc:
        raise _conv_unavailable(exc) from exc


@app.delete("/api/conversations")
async def delete_all_conversations() -> dict:
    """清空**全部**对话（消息随外键级联；`feedback` 一行不动）。

    ⚠⚠ **这条路由不做任何确认** —— 它假设调用方已拿到用户的明确确认。后端没有别的守卫
       可言（单用户、无鉴权、也没有可问的对象），防误触的唯一落点是侧栏那个原地二次确认。
       一句话：**curl 打这一发就是真删**。

    ⚠ 与 `DELETE /api/conversations/{conv_id}` **不冲突**：FastAPI 按**路径段数**匹配，
       这条 1 段、那条 2 段，谁先声明都一样。上面「search 必须写在 `{id}` 之前」那条规矩
       只在**同方法 + 同段数**时才成立，别看着像就照搬
       （`probe_conv_store.py` ⑪ 拿「两条同时都还能打到」钉住）。

    ⚠ 回执里的 `deleted` 是给探针与人工排查用的，**前端刻意不读它**：本地那份看不到
       「别的浏览器建的对话」，拿它对账只会编出一个看起来完全合理的错误数字。
    """
    try:
        deleted = await asyncio.to_thread(conv_store.delete_all_conversations)
    except ConvStoreUnavailable as exc:
        raise _conv_unavailable(exc) from exc
    return {"status": "ok", "deleted": deleted}


@app.get("/api/conversations/search", response_model=list[ConversationHit])
async def search_conversations(q: str = "", limit: int = 20) -> list[ConversationHit]:
    """在**标题与消息正文**里模糊搜索。`q` 为空 → 400（不做「空查询返回全部」）。"""
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="搜索词不能为空")
    try:
        return await asyncio.to_thread(conv_store.search_conversations, query, limit)
    except ConvStoreUnavailable as exc:
        raise _conv_unavailable(exc) from exc


@app.get("/api/conversations/{conv_id}", response_model=ConversationFull)
async def get_conversation(conv_id: str) -> ConversationFull:
    """取一条对话的完整正文。"""
    try:
        found = await asyncio.to_thread(conv_store.get_conversation, conv_id)
    except ConvStoreUnavailable as exc:
        raise _conv_unavailable(exc) from exc
    if found is None:
        raise HTTPException(status_code=404, detail="对话不存在（可能已在别处被删除）")
    return found


@app.put("/api/conversations/{conv_id}")
async def put_conversation(conv_id: str, payload: ConversationUpsert) -> dict:
    """整体 upsert 一条对话（**替换**语义，不是合并）。"""
    try:
        await asyncio.to_thread(conv_store.save_conversation, conv_id, payload)
    except ConvStoreUnavailable as exc:
        raise _conv_unavailable(exc) from exc
    return {"status": "ok", "id": conv_id, "message_count": len(payload.messages)}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str) -> dict:
    """删除一条对话（消息随外键级联删除）。**幂等** —— 删不存在的 id 也回 ok。"""
    try:
        await asyncio.to_thread(conv_store.delete_conversation, conv_id)
    except ConvStoreUnavailable as exc:
        raise _conv_unavailable(exc) from exc
    return {"status": "ok", "id": conv_id}


# ---------------------------------------------------------------- 反馈
# 三条都照上面那五条的成例：**不调 `_require_ready()`**、`asyncio.to_thread`、
# `ConvStoreUnavailable` → 503。反馈不碰 Qdrant / BM25 / 向量模型，把它挂在索引加载上
# 等于让一个独立能力被核心链路拖死（理由与 `/api/vision` 那段逐字相同，而历史那边更强 ——
# 重启后端后的头几十秒 `/api/health` 是 503）。
#
# ⚠ 表结构、以及**为什么 `feedback` 上没有任何外键**，见 `conv_store.py` 的 `_DDL` 之后
#   那段注释。这里只需要知道：一条反馈可以指向一条已经被删掉的对话。


@app.put("/api/feedback/{conversation_id}/{message_id}")
async def put_feedback(
    conversation_id: str, message_id: str, payload: FeedbackUpsert
) -> dict:
    """记一条 👍/👎（同一条消息再评一次 = 覆盖）。"""
    # ⚠ 校验必须在**碰 store 之前**：判据错了还去连库，用户会拿到 503 而不是「你传错了」。
    if payload.rating not in (1, -1):
        raise HTTPException(
            status_code=400, detail="rating 只能是 1（👍）或 -1（👎）"
        )
    try:
        await asyncio.to_thread(
            conv_store.save_feedback, conversation_id, message_id, payload
        )
    except ConvStoreUnavailable as exc:
        raise _conv_unavailable(exc) from exc
    return {
        "status": "ok",
        "conversation_id": conversation_id,
        "message_id": message_id,
        "rating": payload.rating,
    }


@app.delete("/api/feedback/{conversation_id}/{message_id}")
async def delete_feedback(conversation_id: str, message_id: str) -> dict:
    """取消一条评价。**幂等** —— 删不存在的 key 也回 ok（前端「再点一次 = 取消」走这里）。"""
    try:
        await asyncio.to_thread(conv_store.delete_feedback, conversation_id, message_id)
    except ConvStoreUnavailable as exc:
        raise _conv_unavailable(exc) from exc
    return {"status": "ok", "conversation_id": conversation_id, "message_id": message_id}


@app.get("/api/feedback", response_model=list[FeedbackRow])
async def list_feedback(
    rating: int = 0, conversation_id: str = "", limit: int = 100
) -> list[FeedbackRow]:
    """列出反馈供分析取数。`rating=0`（默认）= 不按评价过滤，`rating=-1` = 只看差评。"""
    try:
        return await asyncio.to_thread(
            conv_store.list_feedback, rating, conversation_id, limit
        )
    except ConvStoreUnavailable as exc:
        raise _conv_unavailable(exc) from exc


# ---------------------------------------------------------------- 流式问答
def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ⚠⚠ 这里原先有一整套「联网**兜底**」机器，2026-09-20 随语义变更**整块删除**：
#   开关打开后不再是「答不出才搜」，而是**必定搜**（见 event_stream 里那段）。
#   删掉的东西与它们存在的理由，一并记在这里，免得日后有人「顺手补回来」：
#
#   - 触发 ① `not sources`（本地一条都没召回）
#   - 触发 ② `_recover_web_query()` + 泄漏恢复：模型把 `search_web` 调用写成正文标记时，
#     从标记里把查询词捞回来真搜一次。**判据极窄** —— 只认联网那一条 schema，
#     泄漏的是图谱/数据库形态就不认（`/api/chat` 压根没注册那些工具，模型是糊涂了，
#     不该顺势花一次 Tavily）。这套机制在 `/api/agent` 上**仍然保留**（那条链路有工具循环，
#     `agent.recover_tool_calls` 照旧在用）。
#   - 触发 ③ `_looks_like_no_answer()` + `_NO_ANSWER_RES` + `_NO_ANSWER_HEAD` + `_CITE_RE`：
#     「模型的开场白就是一句推辞」⇒ 真去搜一次再重答。
#
#   触发 ③ 那批判据踩过一次很值得记住的坑（2026-09-20，用户实测）：**初版只认提示词里
#   那句契约话术的字面**，而模型把它改写成了「未包含任何关于香港特别行政区招投标法规
#   流程的信息…无法基于现有参考材料对两地差异进行比较分析」——一个字都没提那句话，
#   于是整条判据**静默失效**，用户看到的就是「开了联网搜索却什么都没搜」。
#   当时连跑三次都碰巧说了标准话术，所以探针全绿。**教训是通用的：拿模型的话当判据，
#   别匹配话术、要匹配语义，而且必须有一条结构性护栏**（当时是「命中句之前不能已出现
#   行内引用 `[n]`」——有角标说明它在作答，不是在推辞）。
#
#   这三条现在都**不可达**了：它们全都要求 `payload.web_search`，而开关一打开，
#   联网就发生在任何正文流出**之前**，没有「先说一句再补搜」这种形态可言。
#   一并消失的还有 `web_used` 守卫、`leaked` / `answer_parts` 两个累积列表，
#   以及 `answer_reset` 在 `/api/chat` 上的用法 —— 那条链路现在**只发一次生成**。
#   前端 `types.ts` 本来就把 `answer_reset` 标成「仅 /api/agent 产生」，改完整对齐了。


def _log_web_outcome(where: str, outcome: object) -> None:
    """把一次联网搜索的结果记一行日志。

    ⚠ 不是为了好看：这条链路**整个是静默的** —— 搜没搜、搜到没搜到、是不是被
    `web_search_min_score` 那道闸门整批丢掉，日志里原本一个字都没有。用户报
    「开了联网却没有联网内容」时只能靠猜。2026-09-20 就是靠这行才发现
    **Tavily 偶尔回 200 但 `results` 是空的**（`search()` 那条 `if not rows` 分支），
    表现与「压根没联网」一模一样。

    ⚠ 改为「必定联网」之后这行**更重要**了：原先只有答不出时才走到这儿，现在每次请求
    都走；开关打开却没有联网内容时，它就是唯一的分诊口。
    """
    # ⚠ 全部走 getattr + 哨兵值：探针里的替身是个 `SimpleNamespace`，字段本来就少
    #   （`probe_web_search.py` 的 fake_web_lookup 就只有 new_sources / elapsed_ms，
    #   连 `ok` 都没有 —— 别在这里加非 getattr 的取法）。
    #   写 `(outcome.text or "").splitlines()[0]` 会在那儿直接 IndexError，
    #   而这行日志在请求路径上 —— 抛出去就是整条请求变成 error 事件。
    #   **日志辅助函数绝不能因为字段缺失把请求搞挂。**
    text = getattr(outcome, "text", "") or ""
    lines = str(text).splitlines()
    logger.info(
        "联网搜索[%s] ok=%s 新增=%d 耗时=%.0fms | %s",
        where,
        getattr(outcome, "ok", None),
        len(getattr(outcome, "new_sources", ()) or ()),
        float(getattr(outcome, "elapsed_ms", 0.0) or 0.0),
        (lines[0][:120] if lines else ""),
    )


@app.post("/api/chat")
async def chat(payload: ChatRequest) -> StreamingResponse:
    _require_ready()
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")

    retriever = get_retriever()
    # 每请求的供应商。空串 ⇒ None ⇒ 服务端默认那家（行为与加这个字段之前逐字相同）。
    # 名字认不出来时 get_llm → resolve_provider 会降级到默认 + 记 WARNING，不抛。
    llm = get_llm(payload.llm_provider or None)
    history = payload.history

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield _sse({"type": "start", "session_id": payload.session_id})

            started = time.perf_counter()
            sources: list[Source] = await asyncio.to_thread(
                retriever.search,
                question,
                payload.top_k,
                "hybrid" if payload.hybrid else "vector",
                payload.rerank,
            )
            retrieve_ms = round((time.perf_counter() - started) * 1000, 2)

            latency = {**retriever.last_latency, "retrieve_ms": retrieve_ms}
            yield _sse(
                {
                    "type": "sources",
                    "sources": [s.model_dump() for s in sources],
                    "latency_ms": latency,
                }
            )

            # ---------- 联网搜索：开关打开就**必定**搜一次 ----------
            # 需求原文：「选择了「联网搜索」后，**必需**检索本地知识库和联网搜索，并基于
            # 知识库检索结果和联网搜索结果回答用户问题。」—— 关键词是**必需**，
            # 不是「答不出才搜」。原先那三条兜底触发（本地 0 召回 / 正文泄漏恢复 /
            # 模型说答不出）已随之整块删除，它们的经过见模块顶部那段大注释。
            #
            # ⚠ 上面那个 sources 事件**照旧先发、不等联网**：来源卡片要秒出（约 50ms），
            #   为联网把首屏拖慢 2~3 秒不划算。空 sources 也不会闪空态——
            #   ChatMessage.tsx 是 `message.sources.length > 0 &&`，空数组根本不渲染面板。
            #
            # ⚠ 两次 sources 不会让来源面板闪一下或者收起来：前端是**整体替换**语义，
            #   而展开态是 ChatMessage 自己的 useState、组件不重挂；本地条目 doc_id 不变
            #   ⇒ React key 不变 ⇒ 实际只有网页条目追加进去。
            #
            # ⚠ 判据里的 `use_llm and llm.ready`：没有大模型时 stream_answer 直接退化成
            #   「把第一条知识库答案原样端出来」，搜回来的网页**没人会读**，只是白花一次
            #   credit。这不是给「必定联网」开后门——用户要的是**作答时**用上联网结果。
            #
            # ⚠ 查询词用**原问题** `question`，不是下面那个拼了图片上下文的 `llm_question`：
            #   图片内容只进提示词、**绝不进检索**（见 rag.with_image_context 的 docstring）。
            #   把一整份招标公告发给 Tavily 既是隐私问题，也是个烂查询词。
            #
            # ⚠ 联网发生在**任何正文流出之前**，所以这里没有 `answer_reset` 可用——
            #   也没有它要作废的东西（不存在「让我联网搜索」那句前导正文了），
            #   下面的生成只跑一遍。`/api/chat` 因此**不再产生 `answer_reset` 事件**，
            #   与前端 types.ts 把该事件标成「仅 /api/agent 产生」本来就一致。
            if (
                payload.web_search
                and payload.use_llm
                and llm.ready
                and web_search.ENABLED
            ):
                # ⚠ 本地条目必须**先占住 1..K** 再让网页续接（理由见 web_lookup 的
                #   docstring：本地有结果时直接用 to_sources() 的 1..N 会和本地撞号）。
                #   重盖后的本地编号与原来**逐个相同**——retriever 那边本来就是
                #   `enumerate(kept, start=1)`，这边是 `len+1`，同序同批必然相等，
                #   所以「本地条目在开关开着/关着时编号一致」这条不变量仍然成立。
                registry = SourceRegistry(cap=settings.agent_max_sources)
                # ⚠ 返回值第三项是 dropped（超出 agent_max_sources 被丢掉的条数）。这条链路
                #   原先**没有上限**，加了 registry 才引入；而 `top_k` 是调用方可传的
                #   （前端不传、默认 5），传大了就在这里被**静默截断**。更阴的是：联网没搜到
                #   时 `sources` 根本不会被重盖（见下面 `if outcome.new_sources`），
                #   于是同一份请求会因为 Tavily 的成败看到**不同的本地条目数**。
                #   这行日志是唯一能看出这件事的地方。
                _, _, dropped = registry.add(sources)
                if dropped:
                    logger.warning(
                        "本地来源超出上限 %d，已丢弃 %d 条（调小 top_k 或调大 agent_max_sources）",
                        settings.agent_max_sources,
                        dropped,
                    )
                outcome = await web_lookup(question, registry=registry)
                _log_web_outcome("必定联网", outcome)
                # ⚠ 只有**真拿到新来源**才重发：一条都没搜到时重发一次同样的表，
                #   除了让前端白刷一遍没有任何作用。
                if outcome.new_sources:
                    sources = registry.dump()
                    latency["web_ms"] = outcome.elapsed_ms
                    # sources 是**整体替换**语义，重发必须带上完整 latency_ms
                    yield _sse(
                        {
                            "type": "sources",
                            "sources": [s.model_dump() for s in sources],
                            "latency_ms": dict(latency),
                        }
                    )

            # ⚠ 上面那句 retriever.search 拿到的**永远是原问题**，只有送进大模型的问题才拼
            #   图片上下文。拼进检索词会稀释向量、让 BM25 满屏乱命中，很可能直接跌破
            #   MIN_VECTOR_SCORE 返回 0 条——详见 rag.with_image_context 的 docstring。
            llm_question = with_image_context(question, payload.image_context)

            async def _run_pass() -> AsyncIterator[str]:
                """跑一遍生成，把 kind 映射成 SSE 事件。

                ⚠⚠ **kind 的映射只在这一处**，别把它写回调用点。
                `markup_leak` 绝不能落到兜底的 `else` 上 —— 那会把它当成 `delta`
                **原样发给前端**，等于修这个 bug 反而亲手把标记送出去。
                """
                async for kind, delta in stream_answer(
                    llm, llm_question, sources, history, use_llm=payload.use_llm
                ):
                    if kind == "markup_leak":
                        # 抑制器（llm.py）已经把它从流里拿掉了，这里只要**别**把它
                        # 当正文发出去。⚠ 恢复路径（把标记还原成真调用去执行）随那三条
                        # 兜底一起删了，所以这行日志不能省：否则「模型说要去搜 →
                        # 正文莫名少了一截」会**完全无声**，与刚修完的那类静默故障同形。
                        logger.warning(
                            "模型把工具调用标记漏进了正文，已抑制（%d 字符）", len(delta)
                        )
                        continue
                    # reasoning = 思维链（前端渲染为「深度思考」折叠区），其余 = 正式回答
                    yield _sse(
                        {"type": "reasoning" if kind == "reasoning" else "delta", "content": delta}
                    )

            try:
                # ⚠ 只跑一遍。原先这里有「跑一遍 → 看模型是不是说要联网 / 说答不出 →
                #   补搜一次 → answer_reset → 再跑一遍」的补救链，随那三条兜底一起删了：
                #   联网现在发生在**生成之前**，那两种形态都不再存在。
                async for chunk in _run_pass():
                    yield chunk
            except LLMNotConfiguredError as exc:
                yield _sse({"type": "error", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 - 上游模型异常不应中断前端展示
                logger.exception("生成回答失败")
                yield _sse({"type": "error", "message": f"生成回答失败: {exc}"})

            yield _sse(
                {
                    "type": "done",
                    "total_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        except asyncio.CancelledError:  # 客户端主动断开
            logger.info("客户端断开连接，已停止生成")
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------- Agent 模式
@app.post("/api/agent")
async def agent(payload: ChatRequest) -> StreamingResponse:
    """与 /api/chat 并存的 Agent 版本：检索被注册成工具，模型可自行改写查询词追加检索。

    /api/chat 是写死流水线，保持原样不动；两者的请求体结构相同，前端用开关选择打哪个。
    """
    _require_ready()
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")

    retriever = get_retriever()
    # ⚠ 这是前端**唯一**走的那条链路，胶囊就是从这里生效的。
    # `agent.py` 全程不碰 get_llm —— llm 是纯形参传进去的，所以这里一句话就够。
    llm = get_llm(payload.llm_provider or None)

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for kind, data in run_agent_stream(
                llm,
                retriever,
                question=question,
                session_id=payload.session_id,
                history=payload.history,
                top_k=payload.top_k,
                mode="hybrid" if payload.hybrid else "vector",
                rerank=payload.rerank,
                use_llm=payload.use_llm,
                # 同上：run_agent_stream 内部的预检索与工具检索都用原问题，
                # 只有 build_agent_messages 那一行用带图问题。
                image_context=payload.image_context,
                # ⚠ 传的是**用户这次开没开开关**，不是「要不要挂工具」——与服务端配没配 Key
                # 合取这件事在 agent.tools_for() 里做，别在这里提前算（算重了就会出现
                # 「提示词提工具、工具表里没有」）。
                enable_web=payload.web_search,
            ):
                if kind == "sources":
                    data = {**data, "sources": [s.model_dump() for s in data["sources"]]}
                yield _sse({"type": kind, **data})
        except asyncio.CancelledError:  # 客户端主动断开
            logger.info("客户端断开连接，已停止生成")
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------- 运维
@app.post("/api/reindex")
async def reindex() -> dict:
    """从数据文件重建 BM25 索引（不重新写入向量库）。"""
    retriever = get_retriever()
    count = await asyncio.to_thread(retriever.reload_bm25)
    return {"status": "ok", "bm25_docs": count}
