"""混合检索融合层。

策略：向量召回 + BM25 召回 分别取 Top-N，再用 RRF（Reciprocal Rank Fusion）融合。
RRF 只依赖排名而非分数，天然规避了两路分数量纲不一致的问题，工程上最稳。

    score(d) = Σ_channel  weight_channel / (k + rank_channel(d))

最终把 RRF 分数归一化到 0~1，便于前端展示与阈值过滤。
"""

from __future__ import annotations

import logging

from .bm25_index import tokenize
from .config import settings

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    vector_hits: list[dict],
    bm25_hits: list[tuple[int, float]],
    bm25_docs: list[dict],
    *,
    k: int | None = None,
    vector_weight: float | None = None,
    bm25_weight: float | None = None,
    limit: int | None = None,
) -> list[dict]:
    """融合两路召回结果。

    vector_hits: [{doc_id, score, payload}]（已按相关性降序）
    bm25_hits:   [(docs 下标, 原始 BM25 分数)]（已按分数降序）
    """
    k = k or settings.rrf_k
    vw = settings.vector_weight if vector_weight is None else vector_weight
    bw = settings.bm25_weight if bm25_weight is None else bm25_weight
    limit = limit or settings.top_k

    merged: dict[str, dict] = {}

    for rank, hit in enumerate(vector_hits, start=1):
        doc_id = hit["doc_id"]
        entry = merged.setdefault(
            doc_id,
            {
                "doc_id": doc_id,
                "payload": hit.get("payload", {}),
                "rrf_score": 0.0,
                "vector_score": None,
                "bm25_score": None,
                "rank_vector": None,
                "rank_bm25": None,
                "matched_by": [],
            },
        )
        entry["rrf_score"] += vw / (k + rank)
        entry["vector_score"] = hit["score"]
        entry["rank_vector"] = rank
        if "vector" not in entry["matched_by"]:
            entry["matched_by"].append("vector")

    for rank, (idx, score) in enumerate(bm25_hits, start=1):
        if idx >= len(bm25_docs):
            continue
        doc = bm25_docs[idx]
        doc_id = doc["doc_id"]
        entry = merged.setdefault(
            doc_id,
            {
                "doc_id": doc_id,
                "payload": doc,
                "rrf_score": 0.0,
                "vector_score": None,
                "bm25_score": None,
                "rank_vector": None,
                "rank_bm25": None,
                "matched_by": [],
            },
        )
        # BM25 命中但向量未命中的文档，payload 来自本地索引
        if not entry["payload"]:
            entry["payload"] = doc
        entry["rrf_score"] += bw / (k + rank)
        entry["bm25_score"] = float(score)
        entry["rank_bm25"] = rank
        if "bm25" not in entry["matched_by"]:
            entry["matched_by"].append("bm25")

    ranked = sorted(merged.values(), key=lambda x: x["rrf_score"], reverse=True)

    # 归一化：理论最高分是两路都排第 1
    max_possible = (vw + bw) / (k + 1)
    for entry in ranked:
        entry["score"] = round(min(1.0, entry["rrf_score"] / max_possible), 4) if max_possible else 0.0

    results = [r for r in ranked if r["score"] >= settings.min_score][:limit]
    logger.info("RRF 融合完成：向量 %d 条 + BM25 %d 条 -> %d 条", len(vector_hits), len(bm25_hits), len(results))
    return results


def make_snippet(answer: str, query: str, max_len: int | None = None) -> tuple[str, list[str]]:
    """从答案中截取与提问最相关的片段，返回 (片段, 命中关键词)。

    做法：按句切分后滑动窗口打分，取 jieba 词重合度最高的窗口。
    """
    max_len = max_len or settings.snippet_max_len
    text = (answer or "").strip()
    if not text:
        return "", []

    query_tokens = set(tokenize(query))
    sentences = _split_sentences(text)

    if len(text) <= max_len:
        return text, _highlights(text, query_tokens)

    # 滑动窗口：至少 1 句，直到超过 max_len
    best_start, best_end, best_score = 0, 1, -1.0
    for start in range(len(sentences)):
        end = start + 1
        while end < len(sentences):
            candidate = "".join(sentences[start:end])
            if len(candidate) > max_len:
                break
            end += 1
        window = "".join(sentences[start:end])
        overlap = len(query_tokens & set(tokenize(window)))
        # 命中数相同时优先更紧凑的片段
        score = overlap - len(window) / (max_len * 10)
        if score > best_score:
            best_start, best_end, best_score = start, end, score

    snippet = "".join(sentences[best_start:best_end]).strip()
    if len(snippet) > max_len:
        snippet = snippet[:max_len].rstrip() + "…"
    elif best_end < len(sentences):
        snippet = snippet + "…"
    return snippet, _highlights(snippet, query_tokens)


def _split_sentences(text: str) -> list[str]:
    import re

    # 保留分隔符，按中文句末标点切分
    parts = re.split(r"(?<=[。！？；\n])", text)
    return [p for p in parts if p and p.strip()]


def _highlights(snippet: str, query_tokens: set[str]) -> list[str]:
    """返回片段中出现的关键词（按长度降序，便于前端优先高亮长词）。"""
    if not query_tokens:
        return []
    snippet_tokens = set(tokenize(snippet))
    hits = [t for t in query_tokens & snippet_tokens if len(t) >= 2]
    # 去掉被更长词包含的短词
    hits.sort(key=len, reverse=True)
    result: list[str] = []
    for token in hits:
        if not any(token in kept for kept in result):
            result.append(token)
    return result[:8]
