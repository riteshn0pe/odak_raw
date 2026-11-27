"""Upload gate. Everything here runs before a byte is persisted.

Ordered cheapest check first so an attack costs us less than it costs the
attacker. Nothing downstream re-validates, so this is the only boundary.
"""

import hashlib
import io
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from app.core.config import Settings
from app.core.constants import MAX_ARCHIVE_EXPANSION_RATIO
from app.core.errors import (
    EncryptedDocumentError,
    PayloadTooLargeError,
    UnsupportedFileTypeError,
    ValidationError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class GateResult:
    data: bytes
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    page_count: int


@dataclass(slots=True)
class Quota:
    max_documents: int
    max_storage_bytes: int
    used_documents: int
    used_storage_bytes: int

    def check(self, incoming_bytes: int) -> None:
        if self.used_documents >= self.max_documents:
            raise PayloadTooLargeError(
                f"document limit reached ({self.max_documents}). delete documents before uploading more",
                limit_documents=self.max_documents,
            )
        if self.used_storage_bytes + incoming_bytes > self.max_storage_bytes:
            raise PayloadTooLargeError(
                f"storage limit reached ({self.max_storage_bytes} bytes). "
                "delete documents before uploading more",
                limit_bytes=self.max_storage_bytes,
            )


async def read_capped(
    stream: AsyncIterator[bytes], max_bytes: int, chunk_hint: int
) -> bytes:
    """Read a stream, aborting the moment the cap is passed.

    The cap is enforced during the read, not after, so an oversized upload never
    lands in memory or on disk in full.
    """
    buffer = bytearray()
    async for chunk in stream:
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise PayloadTooLargeError(
                f"upload exceeds {max_bytes} bytes", limit_bytes=max_bytes
            )
    return bytes(buffer)


def check_zip_bomb(data: bytes) -> None:
    """Reject archives that expand far beyond their compressed size.

    A 10 KB file that inflates to 4 GB is the attack. The 10 MB file is not.
    """
    if not data.startswith(b"PK\x03\x04"):
        return
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            uncompressed = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile:
        raise UnsupportedFileTypeError("corrupt archive", "the file is damaged") from None

    if len(data) and uncompressed / len(data) > MAX_ARCHIVE_EXPANSION_RATIO:
        raise PayloadTooLargeError(
            "archive expands beyond the allowed ratio", ratio=uncompressed // max(len(data), 1)
        )


def check_pdf_shape(data: bytes, max_pages: int) -> int:
    """Page count and encryption check.

    A PDF with 50k pages is a resource exhaustion attempt, and an encrypted PDF
    yields no text so it must fail loudly rather than index as empty.
    """
    if not data.startswith(b"%PDF-"):
        return 0

    import fitz

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise UnsupportedFileTypeError("corrupt pdf", "the file could not be opened") from exc

    try:
        if document.is_encrypted and not document.authenticate(""):
            raise EncryptedDocumentError("remove the password and upload again")
        page_count = document.page_count
    finally:
        document.close()

    if page_count > max_pages:
        raise PayloadTooLargeError(
            f"document has {page_count} pages, limit is {max_pages}", pages=page_count
        )
    return page_count


class UploadGate:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        from app.adapters.parsers.registry import ParserRegistry

        self._registry = ParserRegistry()

    async def run(
        self,
        tenant_id: UUID,
        filename: str,
        stream: AsyncIterator[bytes],
        quota: Quota,
        max_bytes: int | None = None,
    ) -> GateResult:
        if not filename or not filename.strip():
            raise ValidationError("filename is required")

        # strip any path component a client may have sent
        clean_name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not clean_name or clean_name.startswith("."):
            raise ValidationError("invalid filename")

        limit = max_bytes or self._settings.MAX_UPLOAD_BYTES
        data = await read_capped(stream, limit, self._settings.UPLOAD_READ_CHUNK_BYTES)

        if not data:
            raise ValidationError("file is empty")

        quota.check(len(data))
        check_zip_bomb(data)

        # content decides the parser, the extension is only a hint
        _, mime = self._registry.resolve(data, clean_name)
        page_count = check_pdf_shape(data, self._settings.MAX_DOCUMENT_PAGES)

        digest = hashlib.sha256(data).hexdigest()

        logger.info(
            "upload_gate_passed",
            tenant_id=str(tenant_id),
            filename=clean_name,
            mime=mime,
            size_bytes=len(data),
            page_count=page_count,
        )
        return GateResult(
            data=data,
            filename=clean_name,
            mime=mime,
            size_bytes=len(data),
            sha256=digest,
            page_count=page_count,
        )


async def bytes_stream(data: bytes, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """Adapt an in memory buffer to the streaming interface, for tests and for
    server side ingestion of fetched urls."""
    for start in range(0, len(data), chunk_size):
        yield data[start : start + chunk_size]
