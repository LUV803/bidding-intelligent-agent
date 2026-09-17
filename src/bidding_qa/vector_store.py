"""Qdrant 向量检索封装。

支持两种模式：
- cloud：连接 Qdrant 云服务（.env 中 QDRANT_URL / QDRANT_API_KEY）
- local：本地嵌入式文件模式，无需 Docker
- auto ：优先云服务，连接失败自动回退本地，并在日志中明确提示

注意：本地嵌入式模式不支持并发写入，仅建议开发调试使用。
"""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from .config import settings

logger = logging.getLogger(__name__)

_DISTANCE_MAP = {
    "cosine": qm.Distance.COSINE,
    "dot": qm.Distance.DOT,
    "euclid": qm.Distance.EUCLID,
}


class VectorStore:
    def __init__(self, collection: str | None = None) -> None:
        self.collection = collection or settings.qdrant_collection
        self.mode = "unknown"
        self.client = self._build_client()

    # ---------- 连接 ----------
    def _build_client(self) -> QdrantClient:
        mode = settings.qdrant_mode.lower()

        if mode in ("auto", "cloud") and settings.qdrant_url.strip():
            try:
                client = QdrantClient(
                    url=settings.qdrant_url.strip(),
                    api_key=settings.qdrant_api_key.strip() or None,
                    timeout=settings.qdrant_timeout,
                    check_compatibility=False,
                )
                client.get_collections()  # 连通性探测
                self.mode = "cloud"
                logger.info("已连接 Qdrant 云服务: %s", settings.qdrant_url)
                return client
            except Exception as exc:  # noqa: BLE001 - 需要兜底一切网络异常
                if mode == "cloud":
                    raise
                logger.warning("Qdrant 云服务连接失败（%s），回退到本地嵌入式模式", exc)

        path = settings.qdrant_local_path_abs
        path.mkdir(parents=True, exist_ok=True)
        self.mode = "local"
        logger.info("使用 Qdrant 本地嵌入式模式: %s", path)
        return QdrantClient(path=str(path))

    # ---------- 集合管理 ----------
    def collection_exists(self) -> bool:
        try:
            return self.client.collection_exists(self.collection)
        except Exception:  # noqa: BLE001 - 老版本兼容
            try:
                self.client.get_collection(self.collection)
                return True
            except Exception:  # noqa: BLE001
                return False

    def ensure_collection(self, recreate: bool = False, vector_size: int | None = None) -> None:
        size = vector_size or settings.qdrant_vector_size

        if recreate and self.collection_exists():
            logger.warning("重建集合 %s（原有数据将被清空）", self.collection)
            try:
                self.client.delete_collection(self.collection)
            except Exception as exc:  # noqa: BLE001
                logger.warning("删除集合失败（%s），改为清空点位", exc)

        if not self.collection_exists():
            logger.info("创建集合 %s (size=%d, distance=%s)", self.collection, size, settings.qdrant_distance)
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=qm.VectorParams(
                    size=size,
                    distance=_DISTANCE_MAP.get(settings.qdrant_distance.lower(), qm.Distance.COSINE),
                ),
            )
        if recreate:
            # 本地嵌入式模式下 delete_collection 是空操作（点位仍留在磁盘上），
            # 必须再清空一次，否则上一轮的旧点位会残留并污染检索结果。
            removed = self.clear()
            if removed:
                logger.warning("集合重建后仍残留 %d 条旧数据，已强制清空", removed)

        self.ensure_payload_indexes()

    def clear(self) -> int:
        """清空集合内全部点位，返回删除条数（本地/云模式通用）。"""
        before = self.count()
        if before == 0:
            return 0
        try:
            self.client.delete(
                collection_name=self.collection,
                points_selector=qm.FilterSelector(filter=qm.Filter(must=[])),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("清空集合失败: %s", exc)
        return before - self.count()

    def ensure_payload_indexes(self) -> None:
        """为 doc_id 建索引，便于按来源过滤/去重（本地嵌入式模式不支持，跳过）。"""
        if self.mode == "local":
            return
        for field in ("doc_id",):
            try:
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=qm.PayloadSchemaType.KEYWORD,
                )
            except Exception:  # noqa: BLE001 - 已存在则忽略
                pass

    def recreate(self, vector_size: int | None = None) -> None:
        self.ensure_collection(recreate=True, vector_size=vector_size)

    # ---------- 写入 ----------
    def upsert(self, ids: list[str], vectors: list[list[float]], payloads: list[dict[str, Any]]) -> int:
        points = [
            qm.PointStruct(id=_to_point_id(doc_id), vector=vector, payload=payload)
            for doc_id, vector, payload in zip(ids, vectors, payloads, strict=True)
        ]
        # 分批写入，避免单次请求过大
        batch = 128
        for i in range(0, len(points), batch):
            self.client.upsert(collection_name=self.collection, points=points[i : i + batch], wait=True)
        logger.info("写入 Qdrant 完成，共 %d 条", len(points))
        return len(points)

    # ---------- 检索 ----------
    def search(self, vector: list[float], limit: int = 20) -> list[dict[str, Any]]:
        """向量检索，返回 [{doc_id, score, payload}]，score 已按余弦相似度换算到 0~1。"""
        if hasattr(self.client, "query_points"):
            response = self.client.query_points(
                collection_name=self.collection,
                query=vector,
                limit=limit,
                with_payload=True,
            )
            hits = response.points
        else:  # pragma: no cover - 兼容旧版 qdrant-client
            hits = self.client.search(
                collection_name=self.collection,
                query_vector=vector,
                limit=limit,
                with_payload=True,
            )

        results = []
        for hit in hits:
            payload = hit.payload or {}
            results.append(
                {
                    "doc_id": payload.get("doc_id", str(hit.id)),
                    "score": _normalize_cosine(hit.score),
                    "raw_score": float(hit.score),
                    "payload": payload,
                }
            )
        return results

    # ---------- 统计 ----------
    def count(self) -> int:
        try:
            return int(self.client.count(self.collection, exact=True).count)
        except Exception:  # noqa: BLE001
            return 0

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def _normalize_cosine(score: float) -> float:
    """余弦相似度 [-1, 1] -> [0, 1]，便于前端展示。"""
    return max(0.0, min(1.0, (float(score) + 1.0) / 2.0))


def _to_point_id(doc_id: str) -> str:
    """Qdrant 只接受 UUID 或整数作为 ID，这里用 UUID5 保证同一 doc_id 稳定映射。"""
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"bidding-qa:{doc_id}"))
