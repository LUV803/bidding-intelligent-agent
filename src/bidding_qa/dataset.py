"""知识库数据加载：解析 Excel 问答样例数据。"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .config import settings

logger = logging.getLogger(__name__)


def load_qa_docs(path: Path | None = None, dedupe: bool | None = None) -> list[dict]:
    """读取问答数据，返回 [{doc_id, question, answer}]。

    自动识别「问 / 答」两列，容忍列名前后有空格或额外描述。
    dedupe=True（默认，见 .env 的 DEDUPE）时，会丢弃「仅标点/空白不同」的重复问题，
    避免同一条知识占用多个参考来源名额；被丢弃的条目会逐条打日志。
    """
    file_path = path or settings.data_file_path
    if not file_path.exists():
        raise FileNotFoundError(f"数据文件不存在: {file_path}")

    if dedupe is None:
        dedupe = settings.dedupe

    frame = pd.read_excel(file_path, engine="openpyxl", dtype=str)
    frame = frame.dropna(how="all")

    q_col = _pick_column(frame, "问")
    a_col = _pick_column(frame, "答")
    if q_col is None or a_col is None:
        raise ValueError(f"未能识别问答列，实际列名: {list(frame.columns)}")

    docs: list[dict] = []
    seen: dict[str, str] = {}
    dropped = 0

    for _, row in frame.iterrows():
        question = _clean(row.get(q_col))
        answer = _clean(row.get(a_col))
        if not question or not answer:
            continue

        key = _dedupe_key(question)
        if dedupe and key in seen:
            dropped += 1
            logger.info("跳过重复问题（与 %s 重复）: %s", seen[key], question[:40])
            continue

        doc_id = f"qa-{len(docs) + 1:04d}"
        seen[key] = doc_id
        docs.append({"doc_id": doc_id, "question": question, "answer": answer})

    logger.info(
        "已从 %s 载入 %d 条问答%s",
        file_path.name,
        len(docs),
        f"（去重丢弃 {dropped} 条重复问题）" if dropped else "",
    )
    return docs


def document_text(doc: dict) -> str:
    """用于向量化 / 重排的文本。

    问题放在最前：向量模型与重排模型的截断都发生在末尾，
    这样能保证作为主要检索目标的「问题」永远不会被截掉。
    """
    return f"{doc.get('question', '')}\n{doc.get('answer', '')}"


def _dedupe_key(question: str) -> str:
    """归一化后用于判重：忽略空白与中英文标点差异。"""
    import re

    text = re.sub(r"\s+", "", question)
    return re.sub(r"[?？!！。．,，、；;:：\"“”'‘’()（）\[\]【】]", "", text)


def _pick_column(frame: pd.DataFrame, keyword: str) -> str | None:
    for col in frame.columns:
        name = str(col).strip()
        if keyword in name:
            return col
    return None


def _clean(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("nan", "none"):
        return ""
    # 统一空白字符（Excel 中常见全角空格）
    return " ".join(text.split())
