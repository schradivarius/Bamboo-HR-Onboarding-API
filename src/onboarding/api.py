"""FastAPI surface: one webhook route and a healthcheck.

Deliberately thin. It verifies the delivery, works out *which* employees
changed, and hands off to sync.py. It never trusts field values from the
payload -- see extract_employee_ids.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import BackgroundTasks, FastAPI, Request, Response, status

from onboarding.clients.bamboo import BambooClient
from onboarding.clients.harvest import HarvestClient
from onboarding.config import Settings
from onboarding.logging import configure_logging
from onboarding.mapping import MappingPolicy
from onboarding.sync import sync_employee_ids
from onboarding.webhook_auth import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify,
)

log = structlog.get_logger(__name__)

# Keys BambooHR has been observed (or documented) to carry an employee id in.
ID_KEYS = ("id", "employeeId", "employee_id")
# Keys that may hold a list of changed employees.
LIST_KEYS = ("employees", "changedEmployees", "records")


def _id_from_object(obj: Any) -> list[str]:
    if not isinstance(obj, dict):
        return []
    for key in ID_KEYS:
        value = obj.get(key)
        if isinstance(value, str | int) and str(value).strip():
            return [str(value).strip()]
    nested = obj.get("fields")
    if isinstance(nested, dict):
        return _id_from_object(nested)
    return []


def extract_employee_ids(payload: Any) -> list[str]:
    """Pull employee ids out of a BambooHR delivery.

    UNVERIFIED and deliberately permissive: BambooHR's payload schema could
    not be confirmed from the build environment, and reports of it differ
    (a batched {"employees": [...]} list vs a single {"data": {...}} event).

    This is safe to be loose about precisely because ids are ALL we take from
    the payload -- every field we actually write to Harvest is re-read from
    the BambooHR API afterwards. A schema change can therefore cost us a
    missed notification (which the backfill pass repairs) but can never write
    wrong data.
    """
    items: list[Any] = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            data = payload.get("data")
            items = [data] if isinstance(data, dict) else [payload]

    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        for employee_id in _id_from_object(item):
            if employee_id not in seen:
                seen.add(employee_id)
                ordered.append(employee_id)
    return ordered


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    app.state.settings = settings
    app.state.policy = MappingPolicy.from_settings(settings)
    app.state.bamboo = BambooClient(
        api_key=settings.bamboo_api_key.get_secret_value(),
        subdomain=settings.bamboo_subdomain,
    )
    app.state.harvest = HarvestClient(
        token=settings.harvest_token.get_secret_value(),
        account_id=settings.harvest_account_id,
        user_agent=settings.harvest_user_agent,
    )

    log.info(
        "startup",
        dry_run=settings.dry_run,
        signature_format=settings.bamboo_signature_format,
        webhook_verification=bool(settings.bamboo_webhook_private_key),
    )
    if settings.dry_run:
        log.warning("startup.dry_run_enabled", hint="set DRY_RUN=false to write")

    try:
        yield
    finally:
        await app.state.bamboo.aclose()
        await app.state.harvest.aclose()


app = FastAPI(title="BambooHR -> Harvest onboarding", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    settings: Settings = app.state.settings
    return {"status": "ok", "dry_run": settings.dry_run}


async def _process(app_: FastAPI, employee_ids: list[str]) -> None:
    """Background worker. Must never raise -- nothing would catch it."""
    try:
        outcomes = await sync_employee_ids(
            employee_ids,
            bamboo=app_.state.bamboo,
            harvest=app_.state.harvest,
            policy=app_.state.policy,
            dry_run=app_.state.settings.dry_run,
        )
    except Exception:
        log.exception("webhook.processing_crashed", employee_ids=employee_ids)
        return

    log.info(
        "webhook.processed",
        total=len(outcomes),
        actions={
            action: sum(1 for o in outcomes if o.action == action)
            for action in {o.action for o in outcomes}
        },
    )


@app.post(
    "/webhooks/bamboohr",
    status_code=status.HTTP_202_ACCEPTED,
    # The handler returns either a dict or a bare Response, which FastAPI
    # cannot turn into a single response model.
    response_model=None,
)
async def bamboohr_webhook(
    request: Request, background: BackgroundTasks
) -> Response | dict[str, Any]:
    settings: Settings = request.app.state.settings

    # Raw bytes, before any parsing. Re-serialising JSON changes whitespace
    # and key order, which would break the HMAC.
    body = await request.body()

    private_key = settings.bamboo_webhook_private_key
    if private_key is None:
        # Fail closed. An unauthenticated endpoint that creates billable users
        # on demand is not something to leave open "just for now".
        log.error("webhook.rejected_unconfigured")
        return Response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content="BAMBOO_WEBHOOK_PRIVATE_KEY is not configured",
        )

    authentic = verify(
        body=body,
        timestamp=request.headers.get(TIMESTAMP_HEADER, ""),
        signature=request.headers.get(SIGNATURE_HEADER, ""),
        private_key=private_key.get_secret_value(),
        fmt=settings.bamboo_signature_format,
    )
    if not authentic:
        log.warning("webhook.unauthorized")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = await request.json()
    except ValueError:
        log.warning("webhook.malformed_json", bytes=len(body))
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    employee_ids = extract_employee_ids(payload)
    if not employee_ids:
        # Signed, so genuinely from BambooHR -- but we could not find an id.
        # Almost certainly a schema we do not handle yet, so make it loud.
        log.error(
            "webhook.no_employee_ids",
            payload_keys=sorted(payload)
            if isinstance(payload, dict)
            else type(payload).__name__,
        )
        return {"accepted": 0, "note": "no employee ids found in payload"}

    log.info("webhook.accepted", employee_ids=employee_ids)
    background.add_task(_process, request.app, employee_ids)
    return {"accepted": len(employee_ids), "employee_ids": employee_ids}
