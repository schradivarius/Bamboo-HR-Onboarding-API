"""Thin, typed wrapper over the BambooHR v1 API.

Read-only. Nothing in this integration should ever write to the HR system of
record, so there is deliberately no create/update method here -- the blast
radius of a bug stays at "we read too much".

UNVERIFIED: documentation.bamboohr.com was unreachable from the build
environment. Endpoint paths and the custom-report request shape follow the
documented v1 API; confirm before production use.
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
from onboarding.models.bamboo import EMPLOYEE_FIELDS, BambooEmployee

log = structlog.get_logger(__name__)


def base_url(subdomain: str) -> str:
    return f"https://api.bamboohr.com/api/gateway.php/{subdomain}/v1"


class BambooClient:
    """Async BambooHR API client.

    Auth is HTTP Basic with the API key as the username and any non-empty
    password (BambooHR's documented convention is "x").
    """

    def __init__(
        self,
        *,
        api_key: str,
        subdomain: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url(subdomain),
            timeout=timeout,
            transport=transport,
            auth=(api_key, "x"),
            headers={"Accept": "application/json"},
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
        # BambooHR often returns HTML or a bare string on error, and puts the
        # useful part in a header.
        detail = response.headers.get("X-BambooHR-Error-Message") or response.text[:500]

        if status in (401, 403):
            raise AuthError(f"BambooHR rejected credentials ({status}): {detail}")
        if status == 404:
            raise NotFoundError(f"BambooHR 404: {detail}")
        if status in (400, 422):
            raise ValidationError(f"BambooHR rejected the request: {detail}")
        if status == 429:
            raw = response.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw else None
            except ValueError:
                retry_after = None
            raise RateLimitedError(f"BambooHR rate limited: {detail}", retry_after)
        if status >= 500:
            raise ServerError(f"BambooHR {status}: {detail}")
        raise IntegrationError(f"Unexpected BambooHR response {status}: {detail}")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async for attempt in retrying():
            with attempt:
                response = await send(self._client, method, url, **kwargs)
                self._raise_for_status(response)
                return response
        raise AssertionError("unreachable: retrying() always yields or raises")

    # --- operations ---------------------------------------------------------

    async def get_employee(self, employee_id: str) -> BambooEmployee:
        """Fetch one employee.

        This is the authoritative read. Webhook payloads are treated only as
        "employee N changed" notifications -- we never trust field values that
        arrive over the webhook, so a payload-schema change cannot corrupt
        what we write to Harvest.
        """
        response = await self._request(
            "GET",
            f"/employees/{employee_id}",
            params={"fields": ",".join(EMPLOYEE_FIELDS)},
        )
        return BambooEmployee.model_validate(response.json())

    async def list_employees(self) -> list[BambooEmployee]:
        """Fetch all employees via a custom report.

        One request instead of N, which matters for the backfill path. The
        report endpoint returns {"fields": [...], "employees": [...]}.
        """
        response = await self._request(
            "POST",
            "/reports/custom",
            params={"format": "JSON", "onlyCurrent": "false"},
            json={"fields": list(EMPLOYEE_FIELDS)},
        )
        body = response.json()
        return [BambooEmployee.model_validate(row) for row in body.get("employees", [])]
