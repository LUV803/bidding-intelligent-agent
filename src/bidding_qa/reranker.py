"""重排序（Re-ranking）：用 CrossEncoder 对初筛候选做二次精排。

为什么要两阶段：
- 初筛（向量 / BM25）为了让全库都能被扫到，必须用「双塔」结构——query 和 doc 各自
  独立编码，速度快、可预计算，但两者之间没有细粒度交互，只能算粗粒度相似度；
- CrossEncoder 把 (query, doc) 拼成一句送进模型，注意力可以跨越 query 与 doc 的每个词，
  精度显著更高，代价是无法预计算，只能对少量候选做在线打分。

所以标准做法是「召回 N 条（粗） → 重排取 K 条（精）」，本项目 N=20、K=5。
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from .config import settings

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()
# 加载失败的原因：记下来后直接抛出，避免每个请求都重试一次昂贵的加载
_load_error: Exception | None = None


def get_model():
    """懒加载重排模型（首次调用会从 hf-mirror 下载）。"""
    global _model, _load_error
    if _model is not None:
        return _model
    if _load_error is not None:
        raise RuntimeError(f"重排模型不可用（此前加载已失败）: {_load_error}")

    with _model_lock:
        if _model is None:
            try:
                from sentence_transformers import CrossEncoder

                cache_dir = settings.model_cache_path
                cache_dir.mkdir(parents=True, exist_ok=True)
                logger.info(
                    "加载重排模型 %s (device=%s)", settings.rerank_model, settings.embedding_device
                )
                _model = CrossEncoder(
                    settings.rerank_model,
                    device=settings.embedding_device,
                    cache_folder=str(cache_dir),
                    max_length=settings.rerank_max_length,
                )
                logger.info("重排模型加载完成")
            except Exception as exc:  # noqa: BLE001
                _load_error = exc
                raise
    return _model


def rerank(query: str, texts: list[str]) -> list[float]:
    """对候选文本打分，返回 0~1 的相关性概率（与入参顺序一致）。

    模型原始输出是 logits（范围约 -10~+10），这里统一做 sigmoid 映射到 0~1，
    便于展示与阈值比较；不依赖 predict(activation_fn=...) 以免受版本差异影响。
    """
    if not texts:
        return []

    model = get_model()
    pairs = [(query, text) for text in texts]
    logits = model.predict(
        pairs,
        batch_size=settings.rerank_batch_size,
        show_progress_bar=False,
    )
    logits = np.asarray(logits, dtype=np.float64).reshape(-1)
    return _sigmoid(logits).tolist()


def _sigmoid(x: np.ndarray) -> np.ndarray:
    """数值稳定版 sigmoid，避免 exp 溢出。"""
    return np.where(
        x >= 0,
        1.0 / (1.0 + np.exp(-np.abs(x))),
        np.exp(-np.abs(x)) / (1.0 + np.exp(-np.abs(x))),
    )


def warmup() -> None:
    """预热模型，避免首个请求耗时过长。

    注意：即使 rerank_enabled=False 也照常预加载——前端开关可以在任意时刻打开重排，
    若等到那时才加载模型，用户要白等十几秒；启动期的加载则藏在「服务初始化中」后面。

    与检索链路一致地「降级而非报错」：模型没下载全或加载失败时只记警告，
    不能让重排这个增益环节拖垮整个服务的启动。
    """
    try:
        rerank("预热", ["预热文档"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("重排模型预热失败，本次将回退到融合顺序: %s", exc)
        return
    logger.info("重排模型预热完成")
