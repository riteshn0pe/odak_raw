"""Error hierarchy. Every raised error carries an HTTP status and a stable code.

The code is part of the public API contract, clients branch on it. Messages are
for humans and may change.
"""

from typing import Any


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    message = "an unexpected error occurred"

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class ConfigError(AppError):
    code = "config_error"
    message = "service is misconfigured"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"
    message = "request failed validation"


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthenticated"
    message = "missing or invalid credentials"


class AuthorizationError(AppError):
    status_code = 403
    code = "forbidden"
    message = "not permitted"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "resource not found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "resource conflict"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "upload exceeds the allowed size"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"
    message = "too many requests"

    def __init__(self, message: str | None = None, retry_after: int = 60, **details: Any) -> None:
        super().__init__(message, retry_after=retry_after, **details)
        self.retry_after = retry_after


class TenantSuspendedError(AppError):
    status_code = 403
    code = "tenant_suspended"
    message = "tenant is suspended"


class ProviderError(AppError):
    """An upstream provider failed. Carries the provider name for telemetry."""

    status_code = 502
    code = "provider_error"
    message = "an upstream provider failed"

    def __init__(self, provider: str, message: str | None = None, **details: Any) -> None:
        super().__init__(message or f"{provider} failed", provider=provider, **details)
        self.provider = provider


class ProviderTimeoutError(ProviderError):
    status_code = 504
    code = "provider_timeout"


class CircuitOpenError(ProviderError):
    status_code = 503
    code = "circuit_open"
    message = "provider is temporarily unavailable"


class ProviderRateLimitError(ProviderError):
    """Rate limited upstream.

    retry_after carries the provider's own hint when it sends one. Honouring it
    beats guessing, since the provider knows when the window resets.
    """

    status_code = 429
    code = "provider_rate_limited"
    message = "provider rate limit reached"

    def __init__(
        self,
        provider: str,
        message: str | None = None,
        retry_after: float | None = None,
        **details: Any,
    ) -> None:
        super().__init__(provider, message, retry_after=retry_after, **details)
        self.retry_after = retry_after


class ProviderUnavailableError(ProviderError):
    status_code = 503
    code = "provider_unavailable"
    message = "provider is unavailable"


class LLMError(AppError):
    """A model level failure that a retry cannot fix.

    Refusals, context overflow, and malformed requests all land here. Separate
    from ProviderError because the fix is to change the request, not to wait.
    """

    status_code = 502
    code = "llm_failed"
    message = "the model could not complete this request"


class ContextLengthError(LLMError):
    status_code = 413
    code = "context_too_long"
    message = "the request exceeds the model context window"


class IngestionError(AppError):
    status_code = 422
    code = "ingestion_failed"
    message = "document could not be ingested"


class UnsupportedFileTypeError(IngestionError):
    code = "unsupported_file_type"
    message = "file type is not supported"

    def __init__(self, detected: str = "", hint: str = "", **details: Any) -> None:
        text = f"unsupported file type: {detected}" if detected else self.message
        if hint:
            text = f"{text}. {hint}"
        super().__init__(text, detected=detected, **details)


# ingestion code refers to this name, both point at one class
UnsupportedFormatError = UnsupportedFileTypeError


class EncryptedDocumentError(IngestionError):
    code = "document_encrypted"
    message = "document is password protected"


class ParseError(IngestionError):
    """Extraction produced nothing usable.

    Distinct from an unsupported type: the format was understood, the content was
    not recoverable. Usually a scan with no OCR available, or a corrupt file.
    """

    code = "parse_failed"
    message = "no text could be extracted from this document"
