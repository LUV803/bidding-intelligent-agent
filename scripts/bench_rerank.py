"""重排模型耗时基准：固定加载一次，只测 predict 本身。

用法：python scripts/bench_rerank.py
"""

from __future__ import annotations

import time

import numpy as np
import torch
from sentence_transformers import CrossEncoder

from bidding_qa.config import settings
from bidding_qa.dataset import document_text, load_qa_docs

MODEL = settings.rerank_model
QUERY = "评标委员会的专家人数和构成有什么要求？"


def build(max_length: int) -> CrossEncoder:
    return CrossEncoder(
        MODEL,
        device=settings.embedding_device,
        cache_folder=str(settings.model_cache_path),
        max_length=max_length,
    )


def measure(model: CrossEncoder, texts: list[str], batch_size: int, rounds: int = 3) -> float:
    pairs = [(QUERY, t) for t in texts]
    model.predict(pairs[:2], batch_size=batch_size, show_progress_bar=False)  # 预热
    best = float("inf")
    for _ in range(rounds):
        started = time.perf_counter()
        model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
        best = min(best, (time.perf_counter() - started) * 1000)
    return best


def main() -> None:
    print(f"模型 {MODEL} | torch 线程 {torch.get_num_threads()} | 设备 {settings.embedding_device}")
    docs = load_qa_docs()
    texts = [document_text(d) for d in docs]
    lengths = [len(t) for t in texts]
    print(f"候选文本：{len(texts)} 条，平均 {sum(lengths) // len(lengths)} 字，最长 {max(lengths)} 字\n")

    for max_length in (512, 256, 128):
        model = build(max_length)
        tokenizer = model.tokenizer
        row = []
        for n in (5, 10, 20):
            ms = measure(model, texts[:n], settings.rerank_batch_size)
            row.append(f"n={n:<3}{ms:7.0f}ms ({ms / n:5.0f}ms/条)")
        sample_tokens = len(tokenizer(texts[0], truncation=False)["input_ids"])
        print(f"max_length={max_length:<4} 首条实际 {sample_tokens:>3} token | " + " | ".join(row))

    # 答案长度分布：决定截断策略是否安全
    print("\n文本长度分位：", end="")
    for p in (50, 80, 95, 100):
        print(f" P{p}={int(np.percentile(lengths, p))}", end="")
    print()


if __name__ == "__main__":
    main()
