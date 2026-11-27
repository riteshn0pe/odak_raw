"""In-process circuit breakers for external calls.

A breaker fails fast once an upstream has misbehaved enough, so a provider that
is down stops eating the timeout budget of every request behind it. Three states,
the textbook shape:

  closed     calls pass through. failures are counted; once the count reaches the
             threshold the breaker opens.
  open       calls are rejected immediately with CircuitOpenError, for the reset
             window. no upstream traffic while it is open.
  half-open  after the reset window one trial call is allowed. success closes the
             breaker, failure opens it again for another window.

This is deliberately process-local: the state lives in this module's registry,
per instance, and is NOT shared across replicas or restarts. That is acceptable
for the current single-instance deploy — each process protecting its own calls is
enough. A multi-instance deploy would want the state in Redis; until then, do not
assume a breaker tripping here has tripped anywhere else.

Adapters opt in by wrapping their external calls, e.g.

    with guard("gemini"):
        return await client.generate(...)

The breaker records the outcome from the `with` block, counting only transient
failures toward tripping — see `is_transient`. Every guarded call is also
recorded in a rolling window (`RATE_WINDOW_SECONDS`), which is what
`/admin/health/platform` reports as the provider's measured error rate.

Wired in at:

  llm         `anthropic`, and the provider name for each OpenAI-compatible
              backend (`openai`, `openrouter`, `gemini`, `nvidia`) — complete
              and stream
  embeddings  `jina_embeddings`, `openai_embeddings`
  rerank      `cohere_rerank`, `jina_rerank`
  ocr         each provider's own `name` (e.g. `gemini_vision:...`), so the OCR
              fallback chain skips a dead backend rather than waiting on it
  vector      `qdrant` (one guard on the shared `_request`)
  storage     `supabase_storage`
  web search  `tavily`
  whatsapp    `meta_whatsapp`
  queue       `qstash`

The local/in-memory backends (FakeEmbeddings, MemoryVectorStore, LocalStorage,
LocalQueue, BM25Encoder) are not guarded: there is no network call to protect.

Thread/async-safety: a single lock guards all state. For a single-process asyncio
app that is enough — the critical sections are tiny and never await.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.errors import AppError, CircuitOpenError, ProviderError
from app.core.logging import get_logger

logger = get_logger(__name__)

_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half-open"

# How far back the measured error rate looks. Long enough that a low-traffic
# provider still has a sample to divide by, short enough that an outage which
# ended ten minutes ago stops colouring the Health card red.
RATE_WINDOW_SECONDS = 300.0

# Hard cap on retained samples per breaker, so a hot loop cannot turn the window
# into a memory leak between prunes.
_MAX_OUTCOMES = 2000


@dataclass
class Breaker:
    """One named breaker. Not instantiated directly — use `get(name)`."""

    name: str
    fail_max: int
    reset_seconds: float
    state: str = _CLOSED
    failure_count: int = 0
    opened_at: float | None = None
    last_error: str | None = None
    # (monotonic time, succeeded) for every guarded call in the rate window.
    # This is what makes /admin/health/platform's error rate a measurement rather
    # than a reading of the breaker's own state: `failure_count` resets to zero
    # the moment one call succeeds, which says nothing about how a provider has
    # behaved over the last few minutes.
    _outcomes: deque[tuple[float, bool]] = field(default_factory=deque, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _allow(self) -> None:
        """Raise if the breaker is open and the reset window has not elapsed.

        A call that is allowed while open transitions the breaker to half-open, so
        it becomes the single trial call whose outcome decides the next state.
        """
        with self._lock:
            if self.state == _OPEN:
                elapsed = time.monotonic() - (self.opened_at or 0.0)
                if elapsed < self.reset_seconds:
                    raise CircuitOpenError(
                        self.name,
                        retry_after=max(0, int(self.reset_seconds - elapsed)),
                    )
                # window elapsed: let one trial through
                self.state = _HALF_OPEN

    def record_success(self) -> None:
        """A call succeeded. Closes the breaker and clears the failure count."""
        with self._lock:
            if self.state != _CLOSED or self.failure_count:
                logger.info("breaker_closed", breaker=self.name)
            self.state = _CLOSED
            self.failure_count = 0
            self.opened_at = None

    def record_failure(self, error: str | None = None) -> None:
        """A call failed. Trips the breaker once the threshold is reached, and a
        failure while half-open re-opens it immediately."""
        with self._lock:
            self.last_error = error
            if self.state == _HALF_OPEN:
                self.state = _OPEN
                self.opened_at = time.monotonic()
                logger.warning("breaker_reopened", breaker=self.name, error=error)
                return
            self.failure_count += 1
            if self.failure_count >= self.fail_max and self.state != _OPEN:
                self.state = _OPEN
                self.opened_at = time.monotonic()
                logger.warning(
                    "breaker_opened",
                    breaker=self.name,
                    failure_count=self.failure_count,
                    error=error,
                )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            calls, errors = self._window_locked()
            return {
                "name": self.name,
                "state": self.state,
                "failure_count": self.failure_count,
                "fail_max": self.fail_max,
                "opened_at": self.opened_at,
                "last_error": self.last_error,
                "window_seconds": int(RATE_WINDOW_SECONDS),
                "calls": calls,
                "errors": errors,
                "error_rate": (errors / calls) if calls else 0.0,
            }

    def note_call(self, *, success: bool) -> None:
        """Record one physical call for the error-rate window.

        Separate from `record_failure` on purpose. The breaker's count is about
        deciding whether to trip — retry bursts collapse into one, and permanent
        failures are ignored entirely. The rate window is about what actually
        happened on the wire, so every guarded call lands here, including each
        attempt of a retry burst.
        """
        with self._lock:
            self._outcomes.append((time.monotonic(), success))
            self._prune_locked()

    def _prune_locked(self) -> None:
        """Drop outcomes older than the window. Caller holds the lock."""
        cutoff = time.monotonic() - RATE_WINDOW_SECONDS
        while self._outcomes and self._outcomes[0][0] < cutoff:
            self._outcomes.popleft()
        # a pathological burst on one provider must not grow without bound; the
        # newest samples are the ones worth keeping
        while len(self._outcomes) > _MAX_OUTCOMES:
            self._outcomes.popleft()

    def _window_locked(self) -> tuple[int, int]:
        """(calls, errors) inside the window. Caller holds the lock."""
        self._prune_locked()
        calls = len(self._outcomes)
        errors = sum(1 for _, ok in self._outcomes if not ok)
        return calls, errors


# process-global registry. one breaker per name, created on first use.
_registry: dict[str, Breaker] = {}
_registry_lock = threading.Lock()


def get(name: str) -> Breaker:
    """The breaker for `name`, created on first use with the configured limits."""
    with _registry_lock:
        breaker = _registry.get(name)
        if breaker is None:
            settings = get_settings()
            breaker = Breaker(
                name=name,
                fail_max=settings.CIRCUIT_FAIL_MAX,
                reset_seconds=float(settings.CIRCUIT_RESET_SECONDS),
            )
            _registry[name] = breaker
        return breaker


def is_transient(exc: BaseException) -> bool:
    """Whether a failure says anything about the upstream's health.

    Only transient failures belong in a breaker's count. A malformed request, a
    refused prompt, or a missing object means the provider answered and the
    request was wrong — counting those would let one bad request open the breaker
    for every tenant behind it. ProviderError and its subclasses are the
    transient family (app.core.errors); any other AppError is request shaped and
    permanent. Anything from outside the taxonomy — a socket error, an
    unparseable body — counts, because the call did not complete cleanly.
    """
    if isinstance(exc, CircuitOpenError):
        # our own fail-fast, not an upstream failure. normally raised before the
        # guarded block runs at all, so this only catches a nested guard
        return False
    if isinstance(exc, ProviderError):
        return True
    if isinstance(exc, AppError):  # noqa: SIM103 - the three cases read as a table
        return False
    return True


# `with_retry` (app/adapters/llm/base.py) retries one call up to three times and
# the breaker sits inside that loop, so a single logical call can report three
# failures a couple of seconds apart. Counting each would let one failed request
# trip a fail_max=3 breaker. Failures for the same breaker from the same asyncio
# task within this window collapse into one — that window is the retry burst. It
# is wider than with_retry's backoff ladder (0.5 + 1 + 2s plus jitter) and narrow
# enough that a long-lived worker task still reports each fresh outage it hits.
BURST_WINDOW_SECONDS = 10.0

# breaker name -> monotonic time of the last counted failure, per asyncio task.
# a ContextVar is copied per task on write, so a retry burst sees its own marker
# while a concurrent request never does. the default is None rather than {} so
# there is no shared mutable object to leak one task's marker into every other.
_counted_at: ContextVar[dict[str, float] | None] = ContextVar(
    "breaker_counted_at", default=None
)


def _is_retry_burst(name: str) -> bool:
    """Whether this task already counted a failure for `name` moments ago."""
    now = time.monotonic()
    counted = _counted_at.get() or {}
    previous = counted.get(name)
    if previous is not None and now - previous < BURST_WINDOW_SECONDS:
        return True
    # replaced, never mutated in place, so a marker set here cannot become visible
    # to a task that inherited the same dict
    _counted_at.set({**counted, name: now})
    return False


@contextmanager
def guard(
    name: str, *, count_failure: Callable[[BaseException], bool] = is_transient
) -> Iterator[Breaker]:
    """Wrap an external call. Raises CircuitOpenError when the breaker is open;
    records success or failure from the block's outcome.

        with guard("gemini"):
            return await client.generate(...)

    Only failures `count_failure` accepts are counted, transient ones by default.
    A permanent failure is re-raised untouched and leaves the count alone: it is
    neither evidence that the provider is unhealthy nor evidence that it
    recovered. Pass a narrower predicate where an adapter's own error mapping
    already draws that line more precisely.

    Every call that reaches the upstream is also recorded in the error-rate
    window, whether or not it counted toward tripping — that window is the
    measurement `/admin/health/platform` reports.
    """
    breaker = get(name)
    breaker._allow()
    # a call let through while the breaker was open is the half-open trial, and
    # its failure has to re-open the breaker, so it is never collapsed below
    trial = breaker.state == _HALF_OPEN
    try:
        yield breaker
    except Exception as exc:
        transient = count_failure(exc)
        # a permanent failure is the caller's fault, not the provider's, so it is
        # not an error in the provider's rate either
        breaker.note_call(success=not transient)
        if transient and (trial or not _is_retry_burst(name)):
            breaker.record_failure(str(exc) or exc.__class__.__name__)
        raise
    else:
        breaker.note_call(success=True)
        breaker.record_success()


def registry_snapshot() -> list[dict[str, Any]]:
    """Per-breaker state, for the admin platform-health view."""
    with _registry_lock:
        breakers = list(_registry.values())
    return [breaker.snapshot() for breaker in breakers]
