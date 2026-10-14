"""Transport plumbing shared by the Harvest and BambooHR clients."""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from onboarding.clients.errors import (
    AmbiguousWriteError,
    RetryableError,
    ServerError,
)

# Methods we are willing to replay blind. A rejected GET costs nothing; a
# replayed POST can create a second user.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def retrying(attempts: int = 4) -> AsyncRetrying:
    return AsyncRetrying(
        retry=retry_if_exception_type(RetryableError),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(attempts),
        reraise=True,
    )


async def send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Issue one request, translating transport failures into our taxonomy.

    A timeout on a non-idempotent method becomes AmbiguousWriteError rather
    than a retryable error: we genuinely cannot tell whether the remote
    processed it, so the caller must re-read rather than replay.
    """
    try:
        return await client.request(method, url, **kwargs)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        if method.upper() in IDEMPOTENT_METHODS:
            raise ServerError(f"{method} {url} failed in transport: {exc}") from exc
        raise AmbiguousWriteError(
            f"{method} {url} failed in transport ({exc}); "
            "the write may or may not have landed"
        ) from exc
