"""Tracing and usage metering.

Two consumers, two mechanisms. Traces go to Langfuse for debugging a bad answer —
which chunks came back, what the model saw, why the verifier objected. Usage goes
to Postgres because it is billing data and must survive a vendor outage.

Both are wrapped so nothing here can fail a request. An answer that was produced
correctly but not recorded is a lost metric; an answer refused because the
tracing vendor was down is a lost customer.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)


class NullTelemetry:
    """Telemetry disabled. Spans still write to structured logs.

    The default. Nothing about the product should depend on a tracing vendor
    being configured, so this is a complete implementation rather than a stub —
    the logs remain the fallback record of what happened.
    """

    def __init__(self, usage_repo: Any = None) -> None:
        self._usage = usage_repo

    @contextmanager
    def start_span(self, name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
        started = time.perf_counter()
        span: dict[str, Any] = dict(attributes)
        try:
            yield span
        finally:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.debug("span", span=name, duration_ms=elapsed, **_scrub(span))

    async def record_usage(
        self, tenant_id: UUID, kind: str, quantity: int, **meta: Any
    ) -> None:
        """Write a usage event.

        Postgres, not the tracing vendor: this is what invoices are built from.
        """
        if self._usage is None:
            logger.debug("usage", kind=kind, quantity=quantity)
            return
        try:
            await self._usage.record(tenant_id, kind, quantity, **meta)
        except Exception:
            logger.warning("usage_record_failed", kind=kind, exc_info=True)


class LangfuseTelemetry(NullTelemetry):
    """Langfuse tracing, with the local behaviour kept underneath.

    Subclasses the null implementation so usage metering and log spans are
    unchanged — Langfuse is additive, and if its client cannot be constructed the
    parent behaviour is what remains.
    """

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        host: str = "https://cloud.langfuse.com",
        usage_repo: Any = None,
    ) -> None:
        super().__init__(usage_repo)
        self._client: Any = None
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=public_key, secret_key=secret_key, host=host
            )
        except Exception:
            # the package may be absent in a slim image, or the keys wrong.
            # neither is worth failing startup for
            logger.warning("langfuse_unavailable_tracing_disabled")

    @contextmanager
    def start_span(self, name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
        if self._client is None:
            with super().start_span(name, **attributes) as span:
                yield span
            return

        started = time.perf_counter()
        span: dict[str, Any] = dict(attributes)
        trace: Any = None

        try:
            trace = self._client.trace(name=name, metadata=_scrub(attributes))
        except Exception:
            logger.debug("langfuse_trace_start_failed", exc_info=True)

        try:
            yield span
        except Exception as exc:
            if trace is not None:
                _safe(lambda: trace.update(level="ERROR", status_message=str(exc)[:500]))
            raise
        finally:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.debug("span", span=name, duration_ms=elapsed, **_scrub(span))
            if trace is not None:
                _safe(
                    lambda: trace.update(
                        output=_scrub(span), metadata={"duration_ms": elapsed}
                    )
                )

    async def flush(self) -> None:
        """Push buffered events.

        Needed on a serverless host: the process may be frozen immediately after
        a response, and anything still buffered is lost.
        """
        if self._client is not None:
            _safe(self._client.flush)

    async def close(self) -> None:
        await self.flush()


_SENSITIVE = {
    "question",
    "answer",
    "text",
    "prompt",
    "messages",
    "content",
    "api_key",
    "token",
    "authorization",
    "secret",
}


def _scrub(attributes: dict[str, Any]) -> dict[str, Any]:
    """Strip user content and credentials before anything leaves the process.

    Traces are useful for shape — how many chunks, what score, which model, how
    long. They are not a place to accumulate tenant documents or questions, both
    because of the credentials sitting next to them and because tenant data
    should not be replicated into a third party by a debugging feature.
    """
    clean: dict[str, Any] = {}
    for key, value in attributes.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SENSITIVE):
            clean[key] = f"<{_describe(value)}>"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, (list, tuple, set)):
            clean[key] = f"<{len(value)} items>"
        else:
            clean[key] = f"<{type(value).__name__}>"
    return clean


def _describe(value: Any) -> str:
    if isinstance(value, str):
        return f"redacted {len(value)} chars"
    if isinstance(value, (list, tuple, set)):
        return f"redacted {len(value)} items"
    return "redacted"


def _safe(call: Any) -> None:
    try:
        call()
    except Exception:
        logger.debug("telemetry_call_failed", exc_info=True)


@asynccontextmanager
async def traced(telemetry: Any, name: str, **attributes: Any) -> AsyncIterator[dict[str, Any]]:
    """Async wrapper over the sync span context manager.

    Exists so callers on the async path do not have to care which telemetry
    implementation is installed.
    """
    with telemetry.start_span(name, **attributes) as span:
        yield span


def build_telemetry(settings: Any, usage_repo: Any = None) -> Any:
    if settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY:
        return LangfuseTelemetry(
            settings.LANGFUSE_PUBLIC_KEY,
            settings.LANGFUSE_SECRET_KEY,
            settings.LANGFUSE_HOST,
            usage_repo,
        )
    return NullTelemetry(usage_repo)
