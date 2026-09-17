"""BM25 稀疏检索：jieba 分词 + BM25Okapi。

与向量检索互补：BM25 擅长精确术语匹配（法条编号、专有名词、金额等）。
索引会持久化到 data/index 目录，服务启动时直接加载，无需重新分词。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

# 精简停用词：中文虚词 + 常见标点/符号
STOPWORDS: set[str] = {
    "的", "了", "和", "是", "在", "我", "有", "就", "不", "人", "都", "一", "一个", "上", "也",
    "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "那",
    "吗", "呢", "吧", "啊", "与", "及", "或", "对", "为", "被", "把", "从", "以", "并", "等",
    "请问", "什么", "怎么", "如何", "哪些", "可以", "是否", "能否", "应该", "需要", "这个", "那个",
}

_PUNCT_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """搜索引擎模式分词，便于中文子词召回。"""
    words = jieba.lcut_for_search(text or "")
    tokens = []
    for w in words:
        w = w.strip().lower()
        if not w or _PUNCT_RE.match(w) or w in STOPWORDS:
            continue
        tokens.append(w)
    return tokens


class BM25Index:
    """内存版 BM25 索引，支持持久化。"""

    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.corpus_tokens: list[list[str]] = []
        self._bm25: BM25Okapi | None = None

    # ---------- 构建 / 持久化 ----------
    def build(self, docs: list[dict]) -> None:
        """docs 中每项需包含 doc_id / question / answer。"""
        jieba.initialize()
        self.docs = list(docs)
        self.corpus_tokens = [tokenize(self._doc_text(d)) for d in self.docs]
        self._bm25 = BM25Okapi(self.corpus_tokens) if self.corpus_tokens else None
        logger.info("BM25 索引构建完成，文档数=%d", len(self.docs))

    def save(self, index_dir: Path) -> Path:
        index_dir.mkdir(parents=True, exist_ok=True)
        path = index_dir / "bm25_index.json"
        payload = {
            "version": 1,
            "docs": self.docs,
            "corpus_tokens": self.corpus_tokens,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        logger.info("BM25 索引已保存: %s", path)
        return path

    def load(self, index_dir: Path) -> bool:
        path = index_dir / "bm25_index.json"
        if not path.exists():
            return False
        # 预热分词器：jieba 首次调用要构建前缀词典（约 1.4s），
        # 放在加载阶段完成，避免拖慢第一个用户请求。
        jieba.initialize()
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.docs = payload["docs"]
        self.corpus_tokens = [list(t) for t in payload["corpus_tokens"]]
        self._bm25 = BM25Okapi(self.corpus_tokens) if self.corpus_tokens else None
        logger.info("BM25 索引已加载: %d 篇文档", len(self.docs))
        return True

    # ---------- 检索 ----------
    def search(self, query: str, limit: int = 20) -> list[tuple[int, float]]:
        """返回 [(docs 下标, BM25 分数)]，按分数降序；分数为 0 的不返回。"""
        if self._bm25 is None:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [(i, float(s)) for i, s in ranked[:limit] if s > 0]

    def __len__(self) -> int:
        return len(self.docs)

    @staticmethod
    def _doc_text(doc: dict) -> str:
        """问题权重更高：重复一次以提升其在 BM25 中的词频。"""
        question = doc.get("question", "")
        answer = doc.get("answer", "")
        return f"{question} {question} {answer}"
