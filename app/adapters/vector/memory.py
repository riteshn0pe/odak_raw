"""In process vector store for development and tests.

Same isolation contract as the real adapter: tenant_id is a required argument and
filtering happens before scoring, so a test that would leak across tenants fails
here exactly as it would against Qdrant.
"""

import math
from collections import defaultdict
from typing import Any
from uuid import UUID

from app.core.constants import RRF_K
from app.domain.ports import SearchHit, SparseVector, VectorRecord


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def sparse_dot(left: SparseVector, right: SparseVector) -> float:
    right_map = dict(zip(right.indices, right.values, strict=True))
    return sum(value * right_map.get(index, 0.0) for index, value in zip(left.indices, left.values, strict=True))


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for position, identifier in enumerate(ranking):
            scores[identifier] += 1.0 / (k + position + 1)
    return dict(scores)


class MemoryVectorStore:
    def __init__(self) -> None:
        self._collections: dict[str, dict[str, VectorRecord]] = defaultdict(dict)
        self._dimensions: dict[str, int] = {}

    async def ensure_collection(self, name: str, dimensions: int) -> None:
        self._collections.setdefault(name, {})
        self._dimensions[name] = dimensions

    async def upsert(self, collection: str, records: list[VectorRecord]) -> int:
        store = self._collections[collection]
        for record in records:
            store[record.id] = record
        return len(records)

    def _matches(self, record: VectorRecord, tenant_id: UUID, filters: dict[str, Any] | None) -> bool:
        if record.tenant_id != tenant_id:
            return False
        for key, expected in (filters or {}).items():
            actual = record.payload.get(key)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    async def search_hybrid(
        self,
        collection: str,
        tenant_id: UUID,
        dense: list[float],
        sparse: SparseVector | None,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        candidates = [
            record
            for record in self._collections.get(collection, {}).values()
            if self._matches(record, tenant_id, filters)
        ]
        if not candidates:
            return []

        dense_ranked = sorted(
            candidates, key=lambda record: cosine(dense, record.dense), reverse=True
        )
        rankings = [[record.id for record in dense_ranked[: limit * 2]]]

        if sparse is not None and sparse.indices:
            sparse_ranked = sorted(
                (record for record in candidates if record.sparse is not None),
                key=lambda record: sparse_dot(sparse, record.sparse),  # type: ignore[arg-type]
                reverse=True,
            )
            rankings.append([record.id for record in sparse_ranked[: limit * 2]])

        fused = reciprocal_rank_fusion(rankings)
        by_id = {record.id: record for record in candidates}
        ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]
        return [
            SearchHit(id=identifier, score=score, payload=by_id[identifier].payload)
            for identifier, score in ordered
            if identifier in by_id
        ]

    async def delete_by_document(self, collection: str, tenant_id: UUID, document_id: UUID) -> int:
        store = self._collections.get(collection, {})
        doomed = [
            key
            for key, record in store.items()
            if record.tenant_id == tenant_id
            and record.payload.get("document_id") == str(document_id)
        ]
        for key in doomed:
            del store[key]
        return len(doomed)

    async def count(
        self, collection: str, tenant_id: UUID, document_id: UUID | None = None
    ) -> int:
        total = 0
        for record in self._collections.get(collection, {}).values():
            if record.tenant_id != tenant_id:
                continue
            if document_id is not None and record.payload.get("document_id") != str(document_id):
                continue
            total += 1
        return total

    async def close(self) -> None:
        self._collections.clear()
