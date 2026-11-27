"""Ingestion pipeline as an explicit step machine.

Each step records its own completion, so a retry resumes at the failed step
instead of redoing paid work. Parsing is the expensive step (OCR and VLM cost
money per page), which is why parsed markdown is persisted before chunking.

Failure is always terminal-with-reason, never silent. A document either reaches
READY with verified vectors, or FAILED with a message the tenant can act on.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from app.core.constants import OCR_MAX_CONCURRENCY, DocumentStatus, JobState, JobStep
from app.core.errors import AppError, ParseError
from app.core.logging import get_logger
from app.domain.entities import Chunk, ParsedDocument
from app.domain.ports import OCRProvider
from app.services.ingestion.gate import GateResult
from app.services.ingestion.indexer import Indexer, build_chunks, tier_cost_units

logger = get_logger(__name__)

# ordered. index position is the resume point
STEP_ORDER: tuple[JobStep, ...] = (
    JobStep.VALIDATE,
    JobStep.STORE,
    JobStep.PARSE,
    JobStep.CHUNK,
    JobStep.EMBED,
    JobStep.INDEX,
    JobStep.FINALIZE,
)


@dataclass(slots=True)
class PipelineContext:
    """Carries state across steps so a resume does not recompute prior work."""

    tenant_id: UUID
    document_id: UUID
    gate: GateResult
    parsed: ParsedDocument | None = None
    chunks: list[Chunk] = field(default_factory=list)
    completed: set[JobStep] = field(default_factory=set)
    # per-agent tier feature: prepend a model-written context blurb before
    # embedding. resolved from agent config at enqueue time, off by default
    contextual: bool = False
    chunk_count: int = 0
    vector_count: int = 0
    token_count: int = 0
    cost_units: dict[str, int] = field(default_factory=dict)


class DocumentPipeline:
    def __init__(
        self,
        indexer: Indexer,
        parser_registry: object,
        document_repo: object,
        ocr_provider: OCRProvider | None = None,
        contextualiser: object | None = None,
        parent_child: bool = True,
    ) -> None:
        self._indexer = indexer
        self._registry = parser_registry
        self._repo = document_repo
        self._ocr = ocr_provider
        self._contextualiser = contextualiser
        self._parent_child = parent_child

    async def run(self, context: PipelineContext, resume_from: JobStep | None = None) -> PipelineContext:
        start_index = STEP_ORDER.index(resume_from) if resume_from else 0

        for step in STEP_ORDER[start_index:]:
            if step in context.completed:
                continue
            started = datetime.now(UTC)
            try:
                await self._run_step(step, context)
            except AppError as exc:
                await self._fail(context, step, exc.message, exc.code)
                raise
            except Exception as exc:
                await self._fail(context, step, str(exc), "internal_error")
                raise

            context.completed.add(step)
            elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            await self._mark_step(context, step, elapsed_ms)
            logger.info(
                "ingestion_step_done",
                tenant_id=str(context.tenant_id),
                document_id=str(context.document_id),
                step=step.value,
                elapsed_ms=elapsed_ms,
            )

        return context

    async def _run_step(self, step: JobStep, context: PipelineContext) -> None:
        match step:
            case JobStep.VALIDATE:
                # the gate already ran, this records the outcome against the job
                await self._set_status(context, DocumentStatus.PROCESSING)
            case JobStep.STORE:
                await self._store_raw(context)
            case JobStep.PARSE:
                await self._parse(context)
            case JobStep.CHUNK:
                await self._chunk(context)
            case JobStep.EMBED | JobStep.INDEX:
                # embedding and upsert are one transaction in the indexer, doing
                # them apart would mean holding every vector in memory. whichever
                # of the two runs first does the work and marks both complete
                if JobStep.INDEX not in context.completed:
                    await self._index(context)
                    context.completed.add(JobStep.INDEX)
                context.completed.add(JobStep.EMBED)
            case JobStep.FINALIZE:
                await self._finalize(context)

    async def _store_raw(self, context: PipelineContext) -> None:
        store = getattr(self._repo, "store_raw", None)
        if store is None:
            return
        await store(context.tenant_id, context.document_id, context.gate.data, context.gate.mime)

    async def _parse(self, context: PipelineContext) -> None:
        parser, _ = self._registry.resolve(context.gate.data, context.gate.filename)  # type: ignore[attr-defined]
        parsed: ParsedDocument = await parser.parse(context.gate.data, context.gate.filename)

        from app.adapters.parsers.pdf import drop_rasters, needs_escalation

        pending = needs_escalation(parsed)
        if pending:
            if self._ocr is not None:
                parsed = await self._escalate(context, parsed, pending)
            else:
                # no ocr configured. the free tier's text stands and the pages
                # are marked degraded, which is recoverable — the tenant can
                # enable ocr and re-index. failing the document would not be
                for page in parsed.pages:
                    if page.needs_escalation:
                        page.warnings.append("scanned page, no ocr provider configured")
                logger.info(
                    "ocr_skipped",
                    document_id=str(context.document_id),
                    pages=len(pending),
                )

        # rasters are far larger than the text they produced. release them before
        # the document is carried into chunking and indexing
        drop_rasters(parsed)

        if not parsed.markdown.strip():
            raise ParseError(
                "no text could be extracted from this document",
                filename=context.gate.filename,
            )

        context.parsed = parsed
        context.cost_units = tier_cost_units(parsed)

        persist = getattr(self._repo, "store_parsed", None)
        if persist is not None:
            await persist(context.tenant_id, context.document_id, parsed)

    async def _escalate(
        self, context: PipelineContext, parsed: ParsedDocument, pending: list[int]
    ) -> ParsedDocument:
        """Send only the pages that need it to the paid tier.

        Concurrency is bounded because OCR providers rate limit, and a burst that
        trips their limit costs us the whole document rather than one page.

        A page that fails or comes back illegible keeps whatever the cheap tier
        recovered. Partial text beats no text, and the warning records why.
        """
        semaphore = asyncio.Semaphore(OCR_MAX_CONCURRENCY)
        by_number = {page.page_number: page for page in parsed.pages}
        recovered = 0

        async def one(page_number: int) -> None:
            nonlocal recovered
            page = by_number.get(page_number)
            if page is None or page.image is None:
                return

            async with semaphore:
                try:
                    result = await self._ocr.extract(page.image, page.image_mime)  # type: ignore[union-attr]
                except Exception as exc:
                    logger.warning(
                        "ocr_page_failed",
                        document_id=str(context.document_id),
                        page=page_number,
                        error=str(exc),
                    )
                    page.warnings.append("ocr failed for this page")
                    return

            page.warnings.extend(result.warnings)

            if result.markdown.strip():
                page.markdown = result.markdown
                page.needs_escalation = False
                page.degraded = False
                recovered += 1
            else:
                page.warnings.append("ocr returned no usable text for this page")

        await asyncio.gather(*(one(number) for number in pending))
        logger.info(
            "ocr_escalation_done",
            document_id=str(context.document_id),
            attempted=len(pending),
            recovered=recovered,
        )
        return parsed

    async def _chunk(self, context: PipelineContext) -> None:
        assert context.parsed is not None
        context.chunks = build_chunks(
            context.tenant_id, context.document_id, context.parsed, self._parent_child
        )
        if not context.chunks:
            raise ParseError("document produced no chunks", filename=context.gate.filename)
        context.chunk_count = len(context.chunks)

        # contextualise before persisting, so the embed text is what the indexer
        # reads. only when the agent opts in and a contextualiser is configured
        if context.contextual and self._contextualiser is not None:
            await self._contextualiser.contextualise(context.parsed.markdown, context.chunks)

        persist = getattr(self._repo, "store_chunks", None)
        if persist is not None:
            await persist(context.tenant_id, context.document_id, context.chunks)

    async def _index(self, context: PipelineContext) -> None:
        result = await self._indexer.index(
            context.tenant_id,
            context.document_id,
            context.chunks,
            context.gate.filename,
        )
        context.vector_count = result.vector_count
        context.token_count = result.token_count

    async def _finalize(self, context: PipelineContext) -> None:
        await self._set_status(context, DocumentStatus.READY)
        record = getattr(self._repo, "record_usage", None)
        if record is not None:
            await record(
                context.tenant_id,
                context.document_id,
                chunk_count=context.chunk_count,
                token_count=context.token_count,
                cost_units=context.cost_units,
            )

    async def _set_status(
        self, context: PipelineContext, status: DocumentStatus, error: str | None = None
    ) -> None:
        setter = getattr(self._repo, "set_status", None)
        if setter is not None:
            await setter(context.tenant_id, context.document_id, status, error)

    async def _mark_step(self, context: PipelineContext, step: JobStep, elapsed_ms: int) -> None:
        marker = getattr(self._repo, "mark_step", None)
        if marker is not None:
            await marker(context.tenant_id, context.document_id, step, JobState.DONE, elapsed_ms)

    async def _fail(
        self, context: PipelineContext, step: JobStep, message: str, code: str
    ) -> None:
        """A failed document must not stay queryable.

        Vectors are removed even if the failure happened after indexing, because
        a half indexed document produces confident answers from partial content.
        """
        logger.error(
            "ingestion_failed",
            tenant_id=str(context.tenant_id),
            document_id=str(context.document_id),
            step=step.value,
            code=code,
            reason=message,
        )
        # EMBED is included because embedding and upsert share one step (see
        # _run_step), so a partial upsert can fail under the EMBED name
        if step in (JobStep.EMBED, JobStep.INDEX, JobStep.FINALIZE):
            try:
                await self._indexer.remove(context.tenant_id, context.document_id)
            except Exception:
                # the same outage that broke the step often breaks the delete.
                # a leaked vector is recoverable by a retry, a document stuck in
                # processing is not — the status write below must always happen
                logger.error(
                    "fail_path_cleanup_failed",
                    document_id=str(context.document_id),
                    exc_info=True,
                )

        marker = getattr(self._repo, "mark_step", None)
        if marker is not None:
            await marker(context.tenant_id, context.document_id, step, JobState.FAILED, 0, message)
        await self._set_status(context, DocumentStatus.FAILED, message)


def next_step(completed: set[JobStep]) -> JobStep | None:
    """Resume point for a retried job."""
    for step in STEP_ORDER:
        if step not in completed:
            return step
    return None
