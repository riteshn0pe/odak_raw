"""Object storage for raw uploads.

The original bytes are kept for two reasons: a tenant can re-index without
re-uploading, and a citation can link back to the source document rather than
just quoting a chunk of it.

Paths are tenant-prefixed — `tenants/{tenant_id}/documents/{document_id}` — so a
bucket policy can enforce isolation at the storage layer too, independently of
whether application code gets its filters right.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx

from app.core.breaker import guard
from app.core.errors import NotFoundError, ProviderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


def document_path(tenant_id: UUID, document_id: UUID) -> str:
    return f"tenants/{tenant_id}/documents/{document_id}"


class Storage(Protocol):
    async def put(self, path: str, data: bytes, content_type: str) -> str: ...

    async def get(self, path: str) -> bytes: ...

    async def delete(self, path: str) -> None: ...

    async def signed_url(self, path: str, ttl_seconds: int = 900) -> str: ...


class LocalStorage:
    """Filesystem storage for development.

    Path traversal is blocked by resolving and confirming the result stays under
    the root — the object key reaches here from an upload, so it is untrusted
    input even though we construct it ourselves today.
    """

    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        target = (self._root / path).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError(f"path escapes storage root: {path}")
        return target

    async def put(self, path: str, data: bytes, content_type: str = "") -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # to_thread keeps the event loop free; a large upload otherwise blocks
        # every other request on this worker
        await asyncio.to_thread(target.write_bytes, data)
        return path

    async def get(self, path: str) -> bytes:
        target = self._resolve(path)
        if not target.exists():
            raise NotFoundError(f"no object at {path}")
        return await asyncio.to_thread(target.read_bytes)

    async def delete(self, path: str) -> None:
        target = self._resolve(path)
        if target.exists():
            await asyncio.to_thread(target.unlink)

    async def signed_url(self, path: str, ttl_seconds: int = 900) -> str:
        # local files have no signing story. the API serves them through an
        # authenticated route instead
        return f"/v1/documents/raw/{path}"


class SupabaseStorage:
    """Supabase Storage over its REST API.

    Signed URLs are the point: the browser fetches the source document directly
    from storage with a short-lived token, so document bytes never transit the
    API server.
    """

    def __init__(self, url: str, key: str, bucket: str, timeout: float = 60.0) -> None:
        self._bucket = bucket
        self._client = httpx.AsyncClient(
            base_url=f"{url.rstrip('/')}/storage/v1",
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"Authorization": f"Bearer {key}", "apikey": key},
        )

    async def put(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        # one breaker for the whole storage backend: a bucket that is refusing
        # writes is not going to serve reads either, and an upload that hangs on
        # a dead bucket blocks the request behind it
        with guard("supabase_storage"):
            try:
                response = await self._client.post(
                    f"/object/{self._bucket}/{path}",
                    content=data,
                    headers={"content-type": content_type, "x-upsert": "true"},
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("supabase_storage", str(exc)) from exc

            if response.status_code >= 400:
                raise ProviderUnavailableError(
                    "supabase_storage", f"{response.status_code}: {response.text[:200]}"
                )
        return path

    async def get(self, path: str) -> bytes:
        with guard("supabase_storage"):
            try:
                response = await self._client.get(f"/object/{self._bucket}/{path}")
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("supabase_storage", str(exc)) from exc

            # a missing object is an answer, not an outage. NotFoundError is not a
            # ProviderError, so `is_transient` leaves the breaker alone
            if response.status_code == 404:
                raise NotFoundError(f"no object at {path}")
            if response.status_code >= 400:
                raise ProviderUnavailableError(
                    "supabase_storage", f"{response.status_code}: {response.text[:200]}"
                )
        return response.content

    async def delete(self, path: str) -> None:
        with guard("supabase_storage"):
            try:
                await self._client.delete(f"/object/{self._bucket}/{path}")
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("supabase_storage", str(exc)) from exc

    async def signed_url(self, path: str, ttl_seconds: int = 900) -> str:
        with guard("supabase_storage"):
            try:
                response = await self._client.post(
                    f"/object/sign/{self._bucket}/{path}", json={"expiresIn": ttl_seconds}
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("supabase_storage", str(exc)) from exc

            if response.status_code >= 400:
                raise ProviderUnavailableError(
                    "supabase_storage", f"{response.status_code}: {response.text[:200]}"
                )
        signed = str(response.json().get("signedURL", ""))
        return f"{self._client.base_url}{signed}"

    async def close(self) -> None:
        await self._client.aclose()
