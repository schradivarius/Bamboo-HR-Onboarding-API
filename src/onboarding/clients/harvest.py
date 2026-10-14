"""Thin, typed wrapper over the Harvest v2 API.

Deliberately dumb: it turns HTTP into typed objects and typed errors and
nothing else. No "should we create this user" decisions -- those live in
sync.py.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from onboarding.clients._http import retrying, send
from onboarding.clients.errors import (
    AuthError,
    IntegrationError,
    NotFoundError,
    RateLimitedError,
    ServerError,
    ValidationError,
)
from onboarding.models.harvest import HarvestUser, HarvestUserCreate

log = structlog.get_logger(__name__)

BASE_URL = "https://api.harvestapp.com/v2"

# Harvest documents 100 requests per 15 seconds for the general API.
# find_user_by_email pages through users, so a very large account could brush
# this; the retry on 429 handles it.
MAX_PER_PAGE = 100

# Hard stop on pagination. A self-referential or looping "next" link -- a
# remote bug, or a proxy rewriting URLs -- would otherwise spin forever,
# silently, holding a request open. 500 pages is ~50k users: far past any
# plausible account, so hitting it means something is wrong.
MAX_PAGES = 500


class HarvestClient:
    """Async Harvest API client.

    Credentials arrive as plain arguments rather than by importing Settings,
    which keeps the client free of global state and trivial to construct in a
    test.

        async with HarvestClient(token=..., account_id=..., user_agent=...) as h:
            user = await h.create_user(payload)
    """

    def __init__(
        self,
        *,
        token: str,
        account_id: str,
        user_agent: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Harvest rejects requests with no User-Agent, so it is a required
        # argument rather than a default nobody notices.
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Harvest-Account-Id": account_id,
                "User-Agent": user_agent,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- plumbing -----------------------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return

        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {"message": body}

        detail = body.get("message") or body.get("error") or response.text[:500]

        if status in (401, 403):
            # Deliberately does not echo the token.
            raise AuthError(f"Harvest rejected credentials ({status}): {detail}")
        if status == 404:
            raise NotFoundError(f"Harvest 404: {detail}")
        if status in (400, 422):
            raise ValidationError(f"Harvest rejected the payload: {detail}", body)
        if status == 429:
            raw = response.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw else None
            except ValueError:
                retry_after = None
            raise RateLimitedError(f"Harvest rate limited: {detail}", retry_after)
        if status >= 500:
            raise ServerError(f"Harvest {status}: {detail}")
        raise IntegrationError(f"Unexpected Harvest response {status}: {detail}")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async for attempt in retrying():
            with attempt:
                response = await send(self._client, method, url, **kwargs)
                self._raise_for_status(response)
                return response
        raise AssertionError("unreachable: retrying() always yields or raises")

    # --- operations ---------------------------------------------------------

    async def create_user(self, payload: HarvestUserCreate) -> HarvestUser:
        """Create a user.

        Raises ValidationError when the email is already taken -- which is the
        behaviour the whole idempotency design rests on. Raises
        AmbiguousWriteError if the POST times out; the caller should re-read by
        email rather than retrying.
        """
        response = await self._request(
            "POST",
            "/users",
            # exclude_none so an unset optional is omitted rather than sent as
            # null, which Harvest could read as "clear this field".
            json=payload.model_dump(mode="json", exclude_none=True),
        )
        return HarvestUser.model_validate(response.json())

    async def get_user(self, user_id: int) -> HarvestUser:
        response = await self._request("GET", f"/users/{user_id}")
        return HarvestUser.model_validate(response.json())

    async def list_users(self, *, is_active: bool | None = None) -> list[HarvestUser]:
        """Fetch every user, following Harvest's pagination links.

        UNVERIFIED: /v2/users is not believed to support filtering by email.
        If it does, add it and simplify find_user_by_email accordingly.
        """
        users: list[HarvestUser] = []
        params: dict[str, Any] | None = {"per_page": MAX_PER_PAGE}
        if is_active is not None and params is not None:
            params["is_active"] = str(is_active).lower()

        url: str | None = "/users"
        seen: set[str] = set()

        for _ in range(MAX_PAGES):
            if not url:
                return users
            if url in seen:
                log.error(
                    "harvest.pagination_loop",
                    url=url,
                    pages_read=len(seen),
                    users_read=len(users),
                )
                return users
            seen.add(url)

            response = await self._request("GET", url, params=params)
            body = response.json()
            users.extend(HarvestUser.model_validate(u) for u in body.get("users", []))

            # Harvest returns absolute next-page URLs; the query string is
            # already baked in, so stop re-sending our own params.
            url = (body.get("links") or {}).get("next")
            params = None

        log.error(
            "harvest.pagination_limit_reached",
            max_pages=MAX_PAGES,
            users_read=len(users),
        )
        return users

    async def find_user_by_email(self, email: str) -> HarvestUser | None:
        """The idempotency check.

        Case-insensitive, because Harvest and BambooHR may normalise casing
        differently and email local-parts are not case-sensitive in practice.

        Includes INACTIVE users on purpose: an archived former employee still
        occupies that email address, so treating them as absent would just
        earn us a 422 on create. Rehires are exactly where this bites.
        """
        target = email.strip().lower()
        for user in await self.list_users():
            if user.email.strip().lower() == target:
                return user
        return None
