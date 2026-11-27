"""Queue contract and the local fallback.

Ingestion cannot run inside the upload request. A hundred-page scan takes minutes
of OCR, and a scale-to-zero host will cut the connection long before that. So the
upload returns as soon as the file is durable, and the work advances step by step
afterwards.

Two implementations behind one contract:

  LocalQueue    asyncio tasks in-process. Correct for dev, and honest about its
                limit — pending work dies with the process.
  QStashQueue   HTTP callbacks from an external scheduler. Each step is its own
                request, so no single request has to finish the document.

The step machine in `DocumentPipeline` is what makes this work: every step is
resumable, so a dropped callback costs a retry rather than the document.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class Job:
    """One unit of deferred work.

    `attempt` travels with the job rather than living only in the database,
    because the queue needs it to decide whether to retry before any handler runs.
    """

    kind: str
    tenant_id: UUID
    payload: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0
    delay_seconds: int = 0

    def as_body(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tenant_id": str(self.tenant_id),
            "payload": self.payload,
            "attempt": self.attempt,
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> Job:
        return cls(
            kind=str(body["kind"]),
            tenant_id=UUID(str(body["tenant_id"])),
            payload=dict(body.get("payload") or {}),
            attempt=int(body.get("attempt", 0)),
        )


class Queue(Protocol):
    async def enqueue(self, job: Job) -> str: ...


class LocalQueue:
    """In-process execution via asyncio tasks.

    Deliberately not a worker pool. This exists so the system runs on a laptop
    with no external queue; anything more would invite treating it as production
    infrastructure, which it is not — pending work is lost on restart.
    """

    def __init__(self, handler: Any = None, max_concurrency: int = 2) -> None:
        self._handler = handler
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # strong refs, else asyncio may garbage collect a running task
        self._tasks: set[asyncio.Task[None]] = set()

    def set_handler(self, handler: Any) -> None:
        self._handler = handler

    async def enqueue(self, job: Job) -> str:
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return f"local:{job.kind}:{id(task)}"

    async def _run(self, job: Job) -> None:
        if job.delay_seconds:
            await asyncio.sleep(job.delay_seconds)
        async with self._semaphore:
            try:
                if self._handler is None:
                    logger.warning("local_queue_no_handler", kind=job.kind)
                    return
                await self._handler(job)
            except Exception:
                # a crashed background task must never take the process down
                logger.error("local_queue_job_failed", kind=job.kind, exc_info=True)

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for in-flight work. Used on shutdown to lose less of it."""
        if not self._tasks:
            return
        await asyncio.wait(set(self._tasks), timeout=timeout)
