"""Q&A pairs: a shortcut ingest, because a Q&A pair is already a chunk.

One `sources` row per set, one `qa_pairs` row per pair, one `documents` row per
pair. The documents row is what makes delete, quota, retrieval facets, tags,
collections and **citations** need no special case anywhere — a Q&A answer is
cited exactly like a page of a PDF.

The retrieval shape is the whole point. `build_qa_chunks` emits one PARENT_CHILD
chunk per question phrasing, whose `text` is the question (so the *question* is
embedded and matched) and whose `parent_text` is the answer (so the *answer* is
what reaches the model). Siblings collapse to one hit through the existing
`dedupe_key`, so three phrasings of one question do not crowd out three other
sources.

Ingest is three steps — build, store, index — and no parse, no OCR, no storage
blob: `qa_pairs` is the durable record and a re-index re-reads it. The steps still
go through `mark_step`, so `document_jobs` stays the single place to look at
ingestion state, and `Indexer.index` still does its delete-then-upsert-then-verify,
so re-indexing a pair is idempotent for the same reason a document's is.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from typing import Any
from uuid import UUID

from app.adapters.queue.base import Job
from app.core.constants import DocumentStatus, JobState, JobStep, UsageKind
from app.core.errors import NotFoundError, PayloadTooLargeError, ValidationError
from app.core.logging import get_logger
from app.domain.chunking import build_qa_chunks
from app.services.ingestion.gate import check_zip_bomb

logger = get_logger(__name__)

# a Q&A document holds no bytes, so its mime is a marker rather than a format the
# parser registry has to know about — nothing ever parses it
QA_MIME = "application/vnd.contextagent.qa+json"

# §5.2 limits. rejected before anything is written, because a half-applied import
# leaves a tenant unable to tell what to re-upload
QA_MAX_ROWS = 100
QA_MAX_VARIATIONS = 20
QA_MAX_QUESTION_CHARS = 500
QA_MAX_ANSWER_CHARS = 8000
QA_MAX_TITLE_CHARS = 200

# an xlsx of 100 rows is a few tens of KB. the cap is the outer bound the zip-bomb
# check then works inside
QA_IMPORT_MAX_BYTES = 5 * 1024 * 1024

# separators a single cell may use to hold several question phrasings
QA_VARIATION_SEPARATOR = "|"

_TITLE_COLUMNS = ("title", "name", "topic")
_QUESTION_COLUMNS = ("question", "questions", "q")
_ANSWER_COLUMNS = ("answer", "answers", "a", "response")


class QaService:
    """Q&A sets, pairs, their shortcut ingest, and the `.xlsx` importer."""

    def __init__(self, container: Any) -> None:
        self._container = container

    @property
    def _repo(self) -> Any:
        return self._container.source_repo

    @property
    def _documents(self) -> Any:
        return self._container.documents

    @property
    def _sources(self) -> Any:
        return self._container.sources

    # ----------------------------------------------------------------- authoring

    async def create_set(
        self,
        tenant_id: UUID,
        *,
        title: str,
        pairs: list[dict[str, Any]],
        tags: list[str] | None = None,
        collection: str | None = None,
        agent_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Create a Q&A source and its pairs."""
        cleaned = [_validate_pair(pair) for pair in pairs]
        if not cleaned:
            raise ValidationError("at least one Q&A pair is required")

        source: dict[str, Any] = await self._sources.create(
            tenant_id,
            kind="qa",
            title=title,
            tags=tags,
            collection=collection,
            agent_id=agent_id,
            enqueue=False,
        )
        source_id = UUID(str(source["id"]))

        created = []
        for pair in cleaned:
            created.append(
                await self._write_pair(tenant_id, source_id, pair, agent_id=agent_id)
            )
        source["pairs"] = created
        logger.info("qa_set_created", source_id=str(source_id), pairs=len(created))
        return source

    async def upsert_pair(
        self,
        tenant_id: UUID,
        *,
        source_id: UUID | None = None,
        pair_id: UUID | None = None,
        title: str | None = None,
        questions: list[str] | None = None,
        answer: str | None = None,
        agent_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Create a pair, or replace an existing one and re-index only that pair.

        The document id survives an edit: citations already written into message
        history point at it, and replacing the row would orphan every one of them.
        """
        if pair_id is None:
            if source_id is None:
                raise ValidationError("source_id is required to create a pair")
            pair = _validate_pair(
                {"title": title, "questions": questions, "answer": answer}
            )
            return await self._write_pair(tenant_id, source_id, pair, agent_id=agent_id)

        existing = await self._repo.get_qa_pair(tenant_id, pair_id)
        if existing is None:
            raise NotFoundError("Q&A pair not found")

        pair = _validate_pair(
            {
                "title": title if title is not None else existing.get("title"),
                "questions": questions
                if questions is not None
                else _as_list(existing.get("questions")),
                "answer": answer if answer is not None else existing.get("answer"),
            }
        )
        updated: dict[str, Any] | None = await self._repo.update_qa_pair(
            tenant_id,
            pair_id,
            title=pair["title"],
            questions=pair["questions"],
            answer=pair["answer"],
        )
        if updated is None:
            raise NotFoundError("Q&A pair not found")

        document_id = updated.get("document_id") or existing.get("document_id")
        if document_id is None:
            # a pair whose document was lost (a failed create, a manual delete) is
            # repaired rather than refused: the authored content is still here
            target = UUID(str(existing["source_id"]))
            document_id = await self._create_document(
                tenant_id, target, pair, agent_id=agent_id
            )
            await self._repo.update_qa_pair(
                tenant_id, pair_id, document_id=UUID(str(document_id)), document_set=True
            )
        else:
            await self._documents.replace_content(
                tenant_id,
                UUID(str(document_id)),
                sha256=_digest(pair),
                size_bytes=_payload_size(pair),
                mime=QA_MIME,
            )

        await self._enqueue(tenant_id, pair_id, UUID(str(document_id)))
        updated["document_id"] = str(document_id)
        return updated

    async def delete_pair(self, tenant_id: UUID, pair_id: UUID) -> None:
        """Delete a pair, its vectors and its document row, in that order."""
        document_id = await self._repo.delete_qa_pair(tenant_id, pair_id)
        if document_id is None:
            return
        try:
            await self._container.indexer.remove(tenant_id, document_id)
        except Exception:
            logger.warning("qa_unindex_failed", document_id=str(document_id), exc_info=True)
        await self._documents.soft_delete(tenant_id, document_id)

    # -------------------------------------------------------------------- ingest

    async def reindex(self, tenant_id: UUID, pair_id: UUID) -> dict[str, Any]:
        """The `ingest_qa` job body: build chunks, store them, index them.

        Re-read from `qa_pairs` every time rather than from a payload, so a
        redelivered job cannot index stale content, and so `/reindex` on a Q&A
        document is the same code path as its first ingest.
        """
        pair = await self._repo.get_qa_pair(tenant_id, pair_id)
        if pair is None:
            raise NotFoundError("Q&A pair not found")

        raw_document = pair.get("document_id")
        if raw_document is None:
            raise NotFoundError("this Q&A pair has no document to index")
        document_id = UUID(str(raw_document))

        title = str(pair.get("title") or "")
        questions = _as_list(pair.get("questions"))
        answer = str(pair.get("answer") or "")
        if not questions or not answer:
            await self._documents.set_status(
                tenant_id, document_id, DocumentStatus.FAILED, "the Q&A pair is empty"
            )
            raise ValidationError("the Q&A pair is empty")

        started = time.monotonic()
        await self._documents.mark_step(
            tenant_id, document_id, JobStep.CHUNK, JobState.RUNNING
        )
        chunks = build_qa_chunks(tenant_id, document_id, title, questions, answer)
        await self._documents.store_chunks(tenant_id, document_id, chunks)
        await self._documents.mark_step(
            tenant_id,
            document_id,
            JobStep.CHUNK,
            JobState.SUCCEEDED,
            elapsed_ms=_elapsed(started),
        )

        indexed = time.monotonic()
        await self._documents.mark_step(
            tenant_id, document_id, JobStep.INDEX, JobState.RUNNING
        )
        try:
            await self._container.indexer.index(
                tenant_id,
                document_id,
                chunks,
                title or "qa",
                {"source": "qa", "source_id": str(pair.get("source_id") or "")},
            )
        except Exception as exc:
            await self._documents.mark_step(
                tenant_id,
                document_id,
                JobStep.INDEX,
                JobState.FAILED,
                elapsed_ms=_elapsed(indexed),
                error=str(exc)[:500],
            )
            await self._documents.set_status(
                tenant_id, document_id, DocumentStatus.FAILED, str(exc)[:500]
            )
            raise
        await self._documents.mark_step(
            tenant_id,
            document_id,
            JobStep.INDEX,
            JobState.SUCCEEDED,
            elapsed_ms=_elapsed(indexed),
        )

        await self._documents.set_status(tenant_id, document_id, DocumentStatus.READY)
        await self._meter(tenant_id, document_id)

        logger.info(
            "qa_pair_indexed",
            pair_id=str(pair_id),
            document_id=str(document_id),
            chunks=len(chunks),
        )
        return {"pair_id": str(pair_id), "document_id": str(document_id), "chunks": len(chunks)}

    # -------------------------------------------------------------------- import

    async def import_xlsx(
        self,
        tenant_id: UUID,
        data: bytes,
        *,
        source_id: UUID | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        collection: str | None = None,
        agent_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Bulk import from a spreadsheet, all-or-nothing with a per-row report.

        An xlsx is a zip, so the bomb check is not optional. Parsing is
        `openpyxl.load_workbook(read_only=True, data_only=True)` rather than
        `XlsxParser`, because what is needed here is typed columns, not the
        markdown table the parser produces.
        """
        if len(data) > QA_IMPORT_MAX_BYTES:
            raise PayloadTooLargeError(
                f"the import file is limited to {QA_IMPORT_MAX_BYTES // (1024 * 1024)} MB"
            )
        check_zip_bomb(data)

        rows, errors = _read_workbook(data)
        if errors:
            return {"created": 0, "errors": errors}
        if not rows:
            return {"created": 0, "errors": [{"row": 1, "message": "no data rows found"}]}

        if source_id is None:
            source = await self._sources.create(
                tenant_id,
                kind="qa",
                title=(title or "Imported Q&A").strip(),
                tags=tags,
                collection=collection,
                agent_id=agent_id,
                    enqueue=False,
            )
            target = UUID(str(source["id"]))
        else:
            target = source_id

        created = 0
        for pair in rows:
            await self._write_pair(tenant_id, target, pair, agent_id=agent_id)
            created += 1

        logger.info("qa_import_done", source_id=str(target), created=created)
        return {"created": created, "errors": [], "source_id": str(target)}

    # ----------------------------------------------------------------- internals

    async def _write_pair(
        self,
        tenant_id: UUID,
        source_id: UUID,
        pair: dict[str, Any],
        *,
        agent_id: UUID | None = None,
    ) -> dict[str, Any]:
        """One validated pair: document, row, job. In that order.

        The document exists before the pair points at it, so a crash between the
        two leaves an unindexed document rather than a pair whose `document_id` is
        a dangling reference.
        """
        document_id = await self._create_document(tenant_id, source_id, pair, agent_id=agent_id)
        ordinal = await self._repo.next_qa_ordinal(tenant_id, source_id)
        row: dict[str, Any] = await self._repo.create_qa_pair(
            tenant_id,
            source_id,
            title=pair["title"],
            questions=pair["questions"],
            answer=pair["answer"],
            ordinal=ordinal,
            document_id=document_id,
        )
        await self._enqueue(tenant_id, UUID(str(row["id"])), document_id)
        return row

    async def _create_document(
        self,
        tenant_id: UUID,
        source_id: UUID,
        pair: dict[str, Any],
        *,
        agent_id: UUID | None = None,
    ) -> UUID:
        """The pair's `documents` row. No storage blob — there are no bytes.

        The `sha256` is the digest of the authored content, which is safe to
        duplicate because migration 006 narrows the byte-uniqueness index to
        `source_id IS NULL`: two tenants' identical FAQ answers no longer collide.
        """
        document = await self._documents.create(
            tenant_id=tenant_id,
            filename=pair["title"],
            mime=QA_MIME,
            size_bytes=_payload_size(pair),
            sha256=_digest(pair),
            storage_path="",
            agent_id=agent_id,
            metadata={"qa": True, "variations": len(pair["questions"])},
            source="qa",
            source_id=source_id,
        )
        return UUID(str(document.id))

    async def _enqueue(self, tenant_id: UUID, pair_id: UUID, document_id: UUID) -> None:
        """Queue `ingest_qa`. A queue failure fails the document, never the request:
        the authored pair is already durable in `qa_pairs`."""
        try:
            await self._container.queue.enqueue(
                Job(
                    kind="ingest_qa",
                    tenant_id=tenant_id,
                    payload={"pair_id": str(pair_id), "document_id": str(document_id)},
                )
            )
        except Exception as exc:
            logger.error("qa_enqueue_failed", pair_id=str(pair_id), error=str(exc))
            await self._documents.set_status(
                tenant_id, document_id, DocumentStatus.FAILED, "could not queue indexing"
            )

    async def _meter(self, tenant_id: UUID, document_id: UUID) -> None:
        """One metered ingest unit per pair, on the shared ledger."""
        try:
            await self._repo.record_usage(
                tenant_id,
                UsageKind.INGEST_PAGE.value,
                1,
                meta={"kind": "qa", "document_id": str(document_id)},
            )
        except Exception:
            logger.warning("qa_usage_failed", document_id=str(document_id), exc_info=True)


def _elapsed(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _as_list(value: Any) -> list[str]:
    """`qa_pairs.questions` as a list, whatever shape the driver returned."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value]
    if isinstance(value, list | tuple):
        return [str(item) for item in value if str(item).strip()]
    return []


def _digest(pair: dict[str, Any]) -> str:
    """Content hash over the authored fields, in a stable order."""
    payload = json.dumps(
        {"t": pair["title"], "q": pair["questions"], "a": pair["answer"]},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _payload_size(pair: dict[str, Any]) -> int:
    """Bytes the pair would occupy, for the storage quota. Counted rather than
    stored, because there is no blob to measure."""
    return len(pair["answer"].encode("utf-8")) + sum(
        len(question.encode("utf-8")) for question in pair["questions"]
    )


def _validate_pair(pair: dict[str, Any], *, row: int | None = None) -> dict[str, Any]:
    """Normalise one pair, raising `ValidationError` with a row number when given.

    The row number is what turns a failed import into an actionable message: "row
    47: answer is empty" tells a tenant what to fix, "invalid file" does not.
    """
    prefix = f"row {row}: " if row is not None else ""

    questions = [str(item).strip() for item in _as_list(pair.get("questions")) if str(item).strip()]
    if not questions:
        raise ValidationError(f"{prefix}at least one question is required")
    if len(questions) > QA_MAX_VARIATIONS:
        raise ValidationError(f"{prefix}at most {QA_MAX_VARIATIONS} question variations")
    for question in questions:
        if len(question) > QA_MAX_QUESTION_CHARS:
            raise ValidationError(
                f"{prefix}a question is limited to {QA_MAX_QUESTION_CHARS} characters"
            )

    answer = str(pair.get("answer") or "").strip()
    if not answer:
        raise ValidationError(f"{prefix}answer is empty")
    if len(answer) > QA_MAX_ANSWER_CHARS:
        raise ValidationError(f"{prefix}an answer is limited to {QA_MAX_ANSWER_CHARS} characters")

    title = str(pair.get("title") or "").strip() or questions[0][:80]
    return {"title": title[:QA_MAX_TITLE_CHARS], "questions": questions, "answer": answer}


def _read_workbook(data: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse the sheet into validated pairs, or into a per-row error list.

    Validation is all-or-nothing: every row is checked before any is written, and a
    single bad row returns `created: 0`. A 100-row import that half-succeeds is
    worse than one that fails with a row number, because the tenant cannot tell
    what to re-upload.
    """
    import openpyxl  # type: ignore[import-untyped]

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValidationError(f"the file is not a readable .xlsx workbook: {exc}") from exc

    try:
        sheet = workbook.worksheets[0] if workbook.worksheets else None
        if sheet is None:
            return [], [{"row": 1, "message": "the workbook has no sheets"}]

        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            return [], [{"row": 1, "message": "the sheet is empty"}]

        columns = _map_columns(header)
        if not columns["questions"]:
            return [], [{"row": 1, "message": "no 'question' column found"}]
        if columns["answer"] is None:
            return [], [{"row": 1, "message": "no 'answer' column found"}]

        pairs: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for index, values in enumerate(rows, start=2):
            if values is None or all(value is None or str(value).strip() == "" for value in values):
                continue
            if len(pairs) >= QA_MAX_ROWS:
                errors.append(
                    {"row": index, "message": f"at most {QA_MAX_ROWS} rows per import"}
                )
                break
            try:
                pairs.append(_validate_pair(_row_to_pair(values, columns), row=index))
            except ValidationError as exc:
                errors.append({"row": index, "message": str(exc)})

        return pairs, errors
    finally:
        workbook.close()


def _map_columns(header: tuple[Any, ...]) -> dict[str, Any]:
    """Header row to column indexes, matched case-insensitively.

    Several `question` columns are allowed and become variations of the same pair,
    which is the shape a support team's existing spreadsheet usually has.
    """
    questions: list[int] = []
    title: int | None = None
    answer: int | None = None

    for index, cell in enumerate(header):
        name = str(cell or "").strip().lower()
        if not name:
            continue
        if name in _QUESTION_COLUMNS or name.startswith("question"):
            questions.append(index)
        elif name in _ANSWER_COLUMNS and answer is None:
            answer = index
        elif name in _TITLE_COLUMNS and title is None:
            title = index

    return {"title": title, "questions": questions, "answer": answer}


def _row_to_pair(values: tuple[Any, ...], columns: dict[str, Any]) -> dict[str, Any]:
    """One spreadsheet row to the pair shape `_validate_pair` takes.

    A single cell holding several phrasings is split on newlines or `|`, because
    that is how a human writes variations into one column.
    """
    questions: list[str] = []
    for index in columns["questions"]:
        raw = values[index] if index < len(values) else None
        if raw is None:
            continue
        text = str(raw)
        parts = [
            part.strip()
            for chunk in text.splitlines()
            for part in chunk.split(QA_VARIATION_SEPARATOR)
        ]
        questions.extend(part for part in parts if part)

    answer_index = columns["answer"]
    answer = ""
    if isinstance(answer_index, int) and answer_index < len(values):
        answer = str(values[answer_index] or "")

    title = ""
    title_index = columns["title"]
    if isinstance(title_index, int) and title_index < len(values):
        title = str(values[title_index] or "")

    return {"title": title, "questions": questions, "answer": answer}
