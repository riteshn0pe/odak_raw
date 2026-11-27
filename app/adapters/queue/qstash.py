"""QStash: an HTTP scheduler that calls us back.

The shape that matters — QStash holds the job and POSTs it to our own endpoint
when it is due. There is no worker process to keep alive, so this works on a host
that scales to zero, which is the deployment this project targets.

Retries are QStash's, not ours: it re-delivers on any non-2xx. A handler must
therefore be idempotent, which the ingestion step machine already is — each step
records its own completion and a repeat is a no-op.

Signature verification is not optional. The callback endpoint is public by
necessity, so without it anyone who learns the URL can drive ingestion.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx

from app.adapters.queue.base import Job
from app.core.breaker import guard
from app.core.errors import ProviderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

QSTASH_BASE_URL = "https://qstash.upstash.io/v2"

# How many times QStash re-delivers a failing callback before it gives up. Sent
# as `Upstash-Retries` on enqueue and compared against the 0-indexed
# `Upstash-Retried` header on the callback to spot the final attempt.
DEFAULT_MAX_RETRIES = 3


def _normalise_base_url(base_url: str) -> str:
    """Ensure the QStash base URL carries the `/v2` API prefix.

    The console shows the regional endpoint without a version path
    (``https://qstash-us-east-1.upstash.io``), and publishing to that host with no
    prefix returns 404 with an empty body — which reads like a bad callback URL
    rather than a missing path. Appending it here means either form works.
    """
    cleaned = base_url.strip().rstrip("/")
    if not cleaned:
        return QSTASH_BASE_URL
    if not cleaned.endswith("/v2"):
        cleaned = f"{cleaned}/v2"
    return cleaned


class QStashQueue:
    def __init__(
        self,
        token: str,
        callback_url: str,
        *,
        base_url: str = QSTASH_BASE_URL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = 30.0,
    ) -> None:
        self._callback_url = callback_url.rstrip("/")
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=_normalise_base_url(base_url),
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {token}",
                "content-type": "application/json",
            },
        )

    async def enqueue(self, job: Job) -> str:
        headers: dict[str, str] = {
            # QStash forwards Upstash-Forward-* as ordinary headers on the callback
            "Upstash-Forward-X-Job-Kind": job.kind,
            "Upstash-Retries": str(self._max_retries),
        }
        if job.delay_seconds:
            headers["Upstash-Delay"] = f"{job.delay_seconds}s"

        url = f"/publish/{self._callback_url}/internal/jobs"

        # the breaker wraps the mapping as well as the call, so what it records is
        # the classified error rather than a raw httpx exception. an open breaker
        # here surfaces as a failed enqueue, which the caller already handles
        with guard("qstash"):
            try:
                response = await self._client.post(url, json=job.as_body(), headers=headers)
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("qstash", str(exc)) from exc

            if response.status_code >= 400:
                raise ProviderUnavailableError(
                    "qstash",
                    # the body is often empty on a 404, so the URL is what makes
                    # the error diagnosable: a wrong base URL and an unreachable
                    # callback host look identical without it
                    f"{response.status_code} publishing to {url}: {response.text[:300]}",
                )

        message_id = str(response.json().get("messageId", ""))
        logger.info("job_enqueued", kind=job.kind, message_id=message_id)
        return message_id

    @property
    def max_retries(self) -> int:
        """The retry ceiling this queue enqueues with.

        The same value goes out in the `Upstash-Retries` header, so the callback
        can compare `Upstash-Retried` against it to spot the final attempt without
        re-deriving the constant.
        """
        return self._max_retries

    async def close(self) -> None:
        await self._client.aclose()


def verify_signature(
    signature: str, body: str, current_key: str, next_key: str = "", max_age_seconds: int = 300
) -> bool:
    """Verify a QStash callback.

    Both the current and next signing keys are accepted so a key rotation does
    not drop in-flight messages.

    The JWT is verified manually rather than with a library: the algorithm is
    fixed at HS256 here, which makes the alg-confusion and alg-none attacks
    unexpressible.
    """
    if not signature:
        return False

    for key in (current_key, next_key):
        if key and _verify_with_key(signature, body, key, max_age_seconds):
            return True
    return False


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _verify_with_key(signature: str, body: str, key: str, max_age_seconds: int) -> bool:
    parts = signature.split(".")
    if len(parts) != 3:
        return False

    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return False

    # a blob that decodes to a scalar or a list would raise AttributeError on
    # the .get below — on the public callback endpoint, from an unauthenticated
    # caller, before the signature has even been checked
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return False

    if header.get("alg") != "HS256":
        return False

    expected = hmac.new(
        key.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    expected_sig = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    if not hmac.compare_digest(expected_sig, signature_b64):
        return False

    now = time.time()
    try:
        exp = float(payload.get("exp", 0))
        iat = float(payload.get("iat", 0))
    except (TypeError, ValueError):
        # exp/iat set to a string or a list is not a token worth arguing with
        return False
    if exp < now:
        return False
    # bound replay: a captured callback stops being useful after the window
    if now - iat > max_age_seconds:
        return False

    # the body hash is what ties the signature to this specific payload
    body_hash = base64.urlsafe_b64encode(hashlib.sha256(body.encode()).digest()).rstrip(b"=")
    claimed = str(payload.get("body", "")).rstrip("=")
    if claimed and not hmac.compare_digest(body_hash.decode(), claimed):
        return False

    return True
