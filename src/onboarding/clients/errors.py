"""Shared error taxonomy for outbound API clients.

The split that matters is Retryable vs not. Everything else exists so sync.py
can branch on meaning ("that email is taken") rather than on status codes.
"""

from __future__ import annotations

from typing import Any


class IntegrationError(Exception):
    """Base for any outbound API failure."""


class RetryableError(IntegrationError):
    """Safe to replay: the request provably did not take effect."""


class AuthError(IntegrationError):
    """401/403. Never retried -- a bad token stays bad."""


class NotFoundError(IntegrationError):
    """404."""


class ValidationError(IntegrationError):
    """400/422 -- the remote rejected our payload. Carries the parsed body."""

    def __init__(self, message: str, body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.body = body or {}


class RateLimitedError(RetryableError):
    """429. Retryable because the request was rejected, not processed."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServerError(RetryableError):
    """5xx, timeouts, and connection failures."""


class AmbiguousWriteError(IntegrationError):
    """A write may or may not have landed (e.g. a timeout mid-POST).

    Deliberately NOT retryable: replaying it risks a duplicate. Callers
    recover by re-reading state instead of guessing.
    """
