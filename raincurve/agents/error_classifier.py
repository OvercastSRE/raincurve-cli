from __future__ import annotations

from dataclasses import dataclass

AUTH_ERROR = "auth_error"
BILLING_EXHAUSTED = "billing_exhausted"
RATE_LIMITED = "rate_limited"
CONTEXT_OVERFLOW = "context_overflow"
SERVER_ERROR = "server_error"
TRANSPORT_ERROR = "transport_error"
INVALID_REQUEST = "invalid_request"
CONTENT_FILTERED = "content_filtered"
UNKNOWN = "unknown"


@dataclass
class ClassifiedError:
    category: str
    retryable: bool
    wait_seconds: float
    should_compress: bool
    should_fallback: bool
    message: str


_HINTS: dict[str, dict] = {
    AUTH_ERROR: dict(retryable=False, wait_seconds=0, should_compress=False, should_fallback=True),
    BILLING_EXHAUSTED: dict(retryable=False, wait_seconds=0, should_compress=False, should_fallback=True),
    RATE_LIMITED: dict(retryable=True, wait_seconds=30, should_compress=False, should_fallback=False),
    CONTEXT_OVERFLOW: dict(retryable=True, wait_seconds=0, should_compress=True, should_fallback=False),
    SERVER_ERROR: dict(retryable=True, wait_seconds=10, should_compress=False, should_fallback=True),
    TRANSPORT_ERROR: dict(retryable=True, wait_seconds=5, should_compress=False, should_fallback=False),
    INVALID_REQUEST: dict(retryable=False, wait_seconds=0, should_compress=False, should_fallback=False),
    CONTENT_FILTERED: dict(retryable=True, wait_seconds=0, should_compress=True, should_fallback=False),
    UNKNOWN: dict(retryable=False, wait_seconds=0, should_compress=False, should_fallback=False),
}


def classify(exc: Exception) -> ClassifiedError:
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    module = type(exc).__module__ or ""
    cls_name = type(exc).__name__

    category = _classify_by_type(cls_name, module, status, msg)

    if category == RATE_LIMITED:
        retry_after = _parse_retry_after(exc)
        if retry_after:
            hints = {**_HINTS[category], "wait_seconds": retry_after}
        else:
            hints = _HINTS[category]
    else:
        hints = _HINTS.get(category, _HINTS[UNKNOWN])

    return ClassifiedError(category=category, message=str(exc)[:300], **hints)


def _classify_by_type(cls_name: str, module: str, status: int | None, msg: str) -> str:
    if cls_name in ("AuthenticationError", "PermissionDeniedError"):
        return AUTH_ERROR

    if cls_name == "RateLimitError":
        if "credit" in msg or "balance" in msg or "quota" in msg or "billing" in msg:
            return BILLING_EXHAUSTED
        return RATE_LIMITED

    if cls_name == "BadRequestError" or status == 400:
        if any(s in msg for s in ("context_length", "maximum context", "max_tokens", "too long")):
            return CONTEXT_OVERFLOW
        return INVALID_REQUEST

    if status is not None:
        if status in (401, 403):
            return AUTH_ERROR
        if status == 402:
            return BILLING_EXHAUSTED
        if status == 429:
            if "credit" in msg or "balance" in msg:
                return BILLING_EXHAUSTED
            return RATE_LIMITED
        if status >= 500:
            return SERVER_ERROR

    if cls_name in ("ConnectionError", "TimeoutError", "OSError"):
        return TRANSPORT_ERROR

    return _classify_by_message(msg)


def _classify_by_message(msg: str) -> str:
    if "credit balance" in msg or "billing" in msg or "quota exceeded" in msg:
        return BILLING_EXHAUSTED
    if "rate_limit" in msg or "rate limit" in msg or "too many requests" in msg:
        return RATE_LIMITED
    if "context_length" in msg or "maximum context" in msg or "token limit" in msg:
        return CONTEXT_OVERFLOW
    if "content_filter" in msg or "content management" in msg or "safety" in msg:
        return CONTENT_FILTERED
    if "authentication" in msg or "unauthorized" in msg or "invalid api key" in msg:
        return AUTH_ERROR
    if "server error" in msg or "internal error" in msg or "bad gateway" in msg:
        return SERVER_ERROR
    if "timeout" in msg or ("connection" in msg and "refused" in msg):
        return TRANSPORT_ERROR
    return UNKNOWN


def _parse_retry_after(exc: Exception) -> float | None:
    headers = getattr(exc, "response", None)
    if headers is not None:
        headers = getattr(headers, "headers", None)
    if headers is None:
        return None
    val = headers.get("retry-after") or headers.get("Retry-After")
    if val:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return None
