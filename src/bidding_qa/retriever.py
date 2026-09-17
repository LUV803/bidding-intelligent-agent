"""检索服务：把向量检索与 BM25 检索串起来，对外只暴露 search()。"""

from __future__ import annotations

import logging
import time

from .bm25_index import BM25Index
from .config import settings
from .dataset import document_text, load_qa_docs
from .embedding import encode_query
from .hybrid import make_snippet, reciprocal_rank_fusion
from .reranker import rerank as rerank_documents
from .schemas import Source
from .vector_store import VectorStore

logger = logging.getLogger(__name__)


class Retriever:
    """单例式检索器：进程内复用 Qdrant 客户端与 BM25 索引。"""

    def __init__(self) -> None:
        self.store = VectorStore()
        self.bm25 = BM25Index()
        self._ready = False
        self._last_latency: dict[str, float] = {}

    # ---------- 生命周期 ----------
    def load(self, rebuild_bm25: bool = False) -> None:
        if self._ready:
            return

        self.store.ensure_collection()

        if not rebuild_bm25 and self.bm25.load(settings.index_path):
            pass
        else:
            logger.info("本地未找到 BM25 索引，从数据文件重建…")
            self.bm25.build(load_qa_docs())
            self.bm25.save(settings.index_path)

        self._ready = True
        logger.info(
            "检索服务就绪：Qdrant=%s(%d 条) / BM25=%d 条",
            self.store.mode,
            self.store.count(),
            len(self.bm25),
        )

    def reload_bm25(self) -> int:
        """入库后刷新内存中的 BM25 索引。"""
        self.bm25.build(load_qa_docs())
        self.bm25.save(settings.index_path)
        self._ready = True
        return len(self.bm25)

    # ---------- 检索 ----------
    def search(
        self,
        query: str,
        top_k: int | None = None,
        mode: str = "hybrid",
        rerank: bool | None = None,
    ) -> list[Source]:
        """mode: hybrid | vector | bm25；rerank=None 时沿用服务端配置。"""
        query = (query or "").strip()
        if not query:
            return []

        top_k = top_k or settings.top_k
        recall_k = max(settings.recall_k, top_k)
        latency: dict[str, float] = {}

        vector_hits: list[dict] = []
        bm25_hits: list[tuple[int, float]] = []

        if mode in ("hybrid", "vector"):
            started = time.perf_counter()
            vector_hits = self.store.search(encode_query(query).tolist(), limit=recall_k)
            latency["vector_ms"] = round((time.perf_counter() - started) * 1000, 2)

        if mode in ("hybrid", "bm25"):
            started = time.perf_counter()
            bm25_hits = self.bm25.search(query, limit=recall_k)
            latency["bm25_ms"] = round((time.perf_counter() - started) * 1000, 2)

        started = time.perf_counter()
        if mode == "vector":
            fused = _as_vector_only(vector_hits, recall_k)
        elif mode == "bm25":
            fused = _as_bm25_only(bm25_hits, self.bm25.docs, recall_k)
        else:
            fused = reciprocal_rank_fusion(vector_hits, bm25_hits, self.bm25.docs, limit=recall_k)
        latency["fuse_ms"] = round((time.perf_counter() - started) * 1000, 2)

        # 先过滤掉明显不相关的候选（闸门只看向量/BM25 原始分，与重排无关）
        kept = [item for item in fused if _passes_gate(item)]
        if not kept and fused:
            logger.info("检索到 %d 条候选但均未通过相关性阈值，判定为无匹配", len(fused))

        # 记录重排前的融合名次，供前端对比位次变化
        for position, item in enumerate(kept, start=1):
            item["rank_fused"] = position

        use_rerank = settings.rerank_enabled if rerank is None else rerank
        candidates = min(settings.rerank_candidates, len(kept))
        if use_rerank and candidates > 1:
            started = time.perf_counter()
            head = self._apply_rerank(query, kept[:candidates])
            if settings.min_rerank_score > 0:
                head = self._gate_by_rerank(head)
            # 未被重排打分的尾部只在「没judge够」时才回填；
            # 若 head 是被闸门筛空的，尾部只会更不相关，此时必须保持为空以触发「无匹配」。
            kept = head + kept[candidates:] if candidates < top_k else head
            latency["rerank_ms"] = round((time.perf_counter() - started) * 1000, 2)

        # 最后取 Top-K 并重新编号，保证正文里的 [1][2][3] 始终连续
        kept = kept[:top_k]
        sources = [self._to_source(i, item, query) for i, item in enumerate(kept, start=1)]
        latency["total_ms"] = round(sum(latency.values()), 2)
        self._last_latency = latency
        return sources

    @property
    def last_latency(self) -> dict[str, float]:
        return dict(self._last_latency)

    # ---------- 内部 ----------
    def _apply_rerank(self, query: str, items: list[dict]) -> list[dict]:
        """用 CrossEncoder 对候选精排。

        失败时「降级而非报错」：重排只是增益环节，模型没下载好或推理异常时
        应回退到融合顺序继续服务，不能因此让整个问答不可用。
        """
        texts = [document_text(item.get("payload") or {}) for item in items]
        try:
            scores = rerank_documents(query, texts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重排失败，回退到融合顺序: %s", exc)
            return items

        for item, score in zip(items, scores, strict=True):
            item["rerank_score"] = float(score)
        return sorted(items, key=lambda x: x["rerank_score"], reverse=True)

    def _gate_by_rerank(self, items: list[dict]) -> list[dict]:
        """按重排分数再筛一轮：模型认为「答非所问」的直接剔除。

        向量/BM25 只能判断「像不像」，CrossEncoder 判断的是「能不能回答」，
        因此这一步才是「知识库里根本没有这条」的可靠判据。
        """
        kept = [i for i in items if i["rerank_score"] >= settings.min_rerank_score]
        if len(kept) < len(items):
            logger.info(
                "重排后 %d/%d 条低于阈值 %.2f，已剔除",
                len(items) - len(kept),
                len(items),
                settings.min_rerank_score,
            )
        return kept

    def _to_source(self, index: int, item: dict, query: str) -> Source:
        payload = item.get("payload") or {}
        answer = payload.get("answer", "")
        snippet, highlights = make_snippet(answer, query)
        return Source(
            index=index,
            doc_id=item["doc_id"],
            question=payload.get("question", ""),
            answer=answer,
            snippet=snippet,
            highlights=highlights,
            score=item.get("score", 0.0),
            rrf_score=round(item.get("rrf_score", 0.0), 6),
            vector_score=_round(item.get("vector_score")),
            bm25_score=_round(item.get("bm25_score"), 4),
            rerank_score=_round(item.get("rerank_score")),
            rank_vector=item.get("rank_vector"),
            rank_bm25=item.get("rank_bm25"),
            rank_fused=item.get("rank_fused"),
            matched_by=item.get("matched_by", []),
        )


def _as_vector_only(vector_hits: list[dict], top_k: int) -> list[dict]:
    return [
        {
            "doc_id": hit["doc_id"],
            "payload": hit.get("payload", {}),
            "rrf_score": 0.0,
            "vector_score": hit["score"],
            "bm25_score": None,
            "rank_vector": rank,
            "rank_bm25": None,
            "matched_by": ["vector"],
            "score": round(hit["score"], 4),
        }
        for rank, hit in enumerate(vector_hits[:top_k], start=1)
    ]


def _as_bm25_only(bm25_hits: list[tuple[int, float]], docs: list[dict], top_k: int) -> list[dict]:
    if not bm25_hits:
        return []
    top_score = bm25_hits[0][1] or 1.0
    results = []
    for rank, (idx, score) in enumerate(bm25_hits[:top_k], start=1):
        if idx >= len(docs):
            continue
        results.append(
            {
                "doc_id": docs[idx]["doc_id"],
                "payload": docs[idx],
                "rrf_score": 0.0,
                "vector_score": None,
                "bm25_score": float(score),
                "rank_vector": None,
                "rank_bm25": rank,
                "matched_by": ["bm25"],
                "score": round(float(score) / top_score, 4),
            }
        )
    return results


def _passes_gate(item: dict) -> bool:
    """相关性闸门：向量或 BM25 任一通道达标即保留。"""
    vector_score = item.get("vector_score")
    if vector_score is not None and vector_score >= settings.min_vector_score:
        return True
    bm25_score = item.get("bm25_score")
    if bm25_score is not None and bm25_score >= settings.min_bm25_score:
        return True
    return False


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


_retriever: Retriever | None = None


def get_retriever() -> Retriever:
    """进程级单例。"""
    global _retriever
    if _retriever is None:
        _retriever = Retriever()
        _retriever.load()
    return _retriever
