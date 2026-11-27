"""Qdrant vector store. One collection per embedding model, tenants separated by
an indexed tenant_id payload field.

Collection per tenant does not scale: Qdrant Cloud caps collections and per
collection overhead is significant. Payload partitioning is the documented
pattern for many small tenants, and it requires two things to be true, both
enforced here:
  the tenant_id keyword index exists before any ingest
  every search carries a tenant_id filter, with no way to opt out
"""

from typing import Any
from uuid import UUID, uuid5

import httpx

from app.core.breaker import guard
from app.core.config import Settings
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.ports import SearchHit, SparseVector, VectorRecord

logger = get_logger(__name__)

TENANT_FIELD = "tenant_id"
DOCUMENT_FIELD = "document_id"

# Namespace for turning a chunk id into a Qdrant point id. Chunk ids are sha256
# hex strings (see domain/chunking.derive_chunk_id) and Qdrant only accepts an
# unsigned integer or a UUID as a point id, so the hex has to be mapped. uuid5 is
# used rather than a random UUID because the mapping must be stable: re-indexing a
# document has to overwrite the same points, which is what makes ingestion
# idempotent. The canonical chunk id still travels in the payload under
# `chunk_id`, and that is what retrieval reads, so nothing downstream sees the
# derived id.
POINT_ID_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def point_id(chunk_id: str) -> str:
    """Deterministic Qdrant point id for a chunk id.

    Passed through unchanged when the chunk id is already a UUID, so an id that
    Qdrant accepts is never rewritten.
    """
    try:
        return str(UUID(chunk_id))
    except (ValueError, AttributeError, TypeError):
        return str(uuid5(POINT_ID_NAMESPACE, chunk_id))


class QdrantVectorStore:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.QDRANT_URL.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api-key": settings.QDRANT_API_KEY} if settings.QDRANT_API_KEY else {},
            timeout=settings.HTTP_TIMEOUT_SECONDS,
        )

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        ok_missing: bool = False,
    ) -> dict[str, Any]:
        # every Qdrant call funnels through here, so one guard covers search,
        # upsert, and delete. `ok_missing` exists so an existence probe's 404 is
        # an answer rather than an error — without it, booting against a healthy
        # Qdrant with no collection yet would count failures toward the breaker
        with guard("qdrant"):
            try:
                response = await self._client.request(method, path, json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderError("qdrant", "request timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderError("qdrant", str(exc)) from exc

            if ok_missing and response.status_code == 404:
                return {}
            if response.status_code >= 400:
                raise ProviderError("qdrant", f"{response.status_code}: {response.text[:300]}")
        return response.json()  # type: ignore[no-any-return]

    async def ensure_collection(self, name: str, dimensions: int) -> None:
        probe = await self._request("GET", f"/collections/{name}", ok_missing=True)

        if not probe:
            await self._request(
                "PUT",
                f"/collections/{name}",
                {
                    "vectors": {"dense": {"size": dimensions, "distance": "Cosine"}},
                    # idf modifier makes bm25 scoring meaningful inside qdrant
                    "sparse_vectors": {"sparse": {"modifier": "idf"}},
                },
            )
            logger.info("qdrant_collection_created", collection=name, dimensions=dimensions)

        # the tenant index must exist before data lands or every filtered query
        # degrades into a scan. is_tenant hints qdrant to co-locate tenant points
        await self._request(
            "PUT",
            f"/collections/{name}/index?wait=true",
            {"field_name": TENANT_FIELD, "field_schema": {"type": "keyword", "is_tenant": True}},
        )
        await self._request(
            "PUT",
            f"/collections/{name}/index?wait=true",
            {"field_name": DOCUMENT_FIELD, "field_schema": "keyword"},
        )

    async def upsert(self, collection: str, records: list[VectorRecord]) -> int:
        if not records:
            return 0

        points: list[dict[str, Any]] = []
        for record in records:
            vector: dict[str, Any] = {"dense": record.dense}
            if record.sparse is not None:
                vector["sparse"] = {
                    "indices": record.sparse.indices,
                    "values": record.sparse.values,
                }
            payload = dict(record.payload)
            payload[TENANT_FIELD] = str(record.tenant_id)
            # chunk_id is the canonical identity and retrieval reads it from the
            # payload; the point id is only Qdrant's addressing
            payload.setdefault("chunk_id", record.id)
            points.append(
                {"id": point_id(record.id), "vector": vector, "payload": payload}
            )

        await self._request("PUT", f"/collections/{collection}/points?wait=true", {"points": points})
        return len(points)

    def _tenant_filter(
        self, tenant_id: UUID, filters: dict[str, Any] | None
    ) -> dict[str, Any]:
        must: list[dict[str, Any]] = [
            {"key": TENANT_FIELD, "match": {"value": str(tenant_id)}}
        ]
        for key, value in (filters or {}).items():
            if isinstance(value, list):
                must.append({"key": key, "match": {"any": value}})
            else:
                must.append({"key": key, "match": {"value": value}})
        return {"must": must}

    async def search_hybrid(
        self,
        collection: str,
        tenant_id: UUID,
        dense: list[float],
        sparse: SparseVector | None,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        query_filter = self._tenant_filter(tenant_id, filters)

        prefetch: list[dict[str, Any]] = [
            {"query": dense, "using": "dense", "limit": limit * 2, "filter": query_filter}
        ]
        if sparse is not None and sparse.indices:
            prefetch.append(
                {
                    "query": {"indices": sparse.indices, "values": sparse.values},
                    "using": "sparse",
                    "limit": limit * 2,
                    "filter": query_filter,
                }
            )

        body: dict[str, Any] = {
            "prefetch": prefetch,
            # rrf needs no score normalisation, which matters because cosine and
            # bm25 scores are not on a comparable scale
            "query": {"fusion": "rrf"},
            "limit": limit,
            "with_payload": True,
            "filter": query_filter,
        }
        if sparse is not None and sparse.indices:
            # scope idf statistics to this tenant, otherwise one tenant's
            # vocabulary skews another tenant's relevance
            body["params"] = {"idf": {"corpus": self._tenant_filter(tenant_id, None)}}

        result = await self._request(
            "POST",
            f"/collections/{collection}/points/query",
            body,
            # a collection that does not exist yet means this tenant has indexed
            # nothing. that is an empty result, not a failure — without this a
            # brand-new tenant asking their first question gets a 503 instead of
            # the agent's "I don't know" fallback
            ok_missing=True,
        )
        points = result.get("result", {}).get("points", [])
        return [
            SearchHit(id=str(point["id"]), score=float(point["score"]), payload=point.get("payload", {}))
            for point in points
        ]

    async def delete_by_document(self, collection: str, tenant_id: UUID, document_id: UUID) -> int:
        await self._request(
            "POST",
            f"/collections/{collection}/points/delete?wait=true",
            {
                "filter": {
                    "must": [
                        {"key": TENANT_FIELD, "match": {"value": str(tenant_id)}},
                        {"key": DOCUMENT_FIELD, "match": {"value": str(document_id)}},
                    ]
                }
            },
            # deleting from a collection that was never created is already the
            # desired end state; a delete on the failure path must not mask the
            # original error with a 404
            ok_missing=True,
        )
        return 1

    async def count(
        self, collection: str, tenant_id: UUID, document_id: UUID | None = None
    ) -> int:
        must: list[dict[str, Any]] = [{"key": TENANT_FIELD, "match": {"value": str(tenant_id)}}]
        if document_id is not None:
            must.append({"key": DOCUMENT_FIELD, "match": {"value": str(document_id)}})
        result = await self._request(
            "POST",
            f"/collections/{collection}/points/count",
            {"filter": {"must": must}, "exact": True},
            # no collection means nothing indexed, which counts as zero
            ok_missing=True,
        )
        return int(result.get("result", {}).get("count", 0))

    async def close(self) -> None:
        await self._client.aclose()
