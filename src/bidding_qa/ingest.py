"""数据入库：Excel -> 向量化 -> 写入 Qdrant，同时构建 BM25 索引。

用法：
    python -m bidding_qa.ingest            # 增量写入（集合不存在则创建）
    python -m bidding_qa.ingest --recreate # 重建集合后全量写入
"""

from __future__ import annotations

import argparse
import logging
import sys

from .bm25_index import BM25Index
from .config import settings
from .dataset import document_text, load_qa_docs
from .embedding import encode_documents
from .vector_store import VectorStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bidding_qa.ingest")


def run(recreate: bool = False, data_file: str | None = None) -> dict:
    from pathlib import Path

    docs = load_qa_docs(Path(data_file) if data_file else None)
    if not docs:
        raise SystemExit("没有解析到任何问答数据，请检查数据文件格式。")

    texts = [document_text(d) for d in docs]
    logger.info("开始向量化 %d 条文本（首次运行会下载模型，请耐心等待）…", len(texts))
    vectors = encode_documents(texts)
    vector_size = int(vectors.shape[1])
    logger.info("向量化完成，维度=%d", vector_size)

    store = VectorStore()
    store.ensure_collection(recreate=recreate, vector_size=vector_size)

    payloads = [{"doc_id": d["doc_id"], "question": d["question"], "answer": d["answer"]} for d in docs]
    written = store.upsert([d["doc_id"] for d in docs], vectors.tolist(), payloads)

    bm25 = BM25Index()
    bm25.build(docs)
    index_path = bm25.save(settings.index_path)

    count = store.count()
    store.close()

    summary = {
        "documents": len(docs),
        "vectors_written": written,
        "vector_size": vector_size,
        "collection": settings.qdrant_collection,
        "qdrant_mode": store.mode,
        "collection_points": count,
        "bm25_index": str(index_path),
    }
    logger.info("入库完成：%s", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="招投标问答知识库入库")
    parser.add_argument("--recreate", action="store_true", help="删除并重建 Qdrant 集合")
    parser.add_argument("--data-file", default=None, help="自定义数据文件路径")
    args = parser.parse_args()
    try:
        run(recreate=args.recreate, data_file=args.data_file)
    except Exception as exc:  # noqa: BLE001
        logger.error("入库失败: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
