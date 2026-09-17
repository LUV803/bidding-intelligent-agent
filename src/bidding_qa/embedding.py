"""向量化：BAAI/bge-small-zh-v1.5（512 维）。

- 文档侧：直接编码
- 查询侧：按 BGE 官方要求添加中文检索指令前缀，可显著提升召回
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from .config import settings  # 必须先于 sentence_transformers 导入（设置 HF 环境变量）

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()


def get_model():
    """懒加载句向量模型（首次调用会从 hf-mirror 下载）。"""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                cache_dir = settings.model_cache_path
                cache_dir.mkdir(parents=True, exist_ok=True)
                logger.info("加载向量模型 %s (device=%s)", settings.embedding_model, settings.embedding_device)
                model = SentenceTransformer(
                    settings.embedding_model,
                    device=settings.embedding_device,
                    cache_folder=str(cache_dir),
                )
                model.max_seq_length = settings.embedding_max_seq_length
                _model = model
                logger.info("向量模型加载完成，维度=%d", _dimension(model))
    return _model


def embedding_dim() -> int:
    return _dimension(get_model())


def _dimension(model) -> int:
    """sentence-transformers 6.x 将该方法改名为 get_embedding_dimension。"""
    getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return int(getter())


def _as_float32(vectors: np.ndarray) -> np.ndarray:
    return np.asarray(vectors, dtype=np.float32)


def encode_documents(texts: list[str], batch_size: int | None = None) -> np.ndarray:
    """编码知识库文本，返回 L2 归一化后的向量。"""
    if not texts:
        return np.zeros((0, settings.qdrant_vector_size), dtype=np.float32)
    model = get_model()
    vectors = model.encode(
        texts,
        batch_size=batch_size or settings.embedding_batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=len(texts) > 64,
    )
    return _as_float32(vectors)


def encode_query(text: str) -> np.ndarray:
    """编码用户提问（带 BGE 中文检索指令前缀）。"""
    model = get_model()
    prefixed = f"{settings.embedding_query_instruction}{text}"
    vector = model.encode(
        [prefixed],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return _as_float32(vector)[0]


def warmup() -> None:
    """预热模型，避免首个请求耗时过长。"""
    dim = embedding_dim()
    encode_query("预热")
    logger.info("向量模型预热完成，维度=%d", dim)
