"""Orchestration: mirror a BambooHR employee into Harvest, idempotently.

The only stateful decision in the system lives here, and it is deliberately
backed by Harvest rather than by a local database: Harvest is the system that
would be corrupted by a double-write, so Harvest is the only source of truth
that cannot drift from reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import structlog

from onboarding.clients.bamboo import BambooClient
from onboarding.clients.errors import (
    AmbiguousWriteError,
    IntegrationError,
    NotFoundError,
    ValidationError,
)
from onboarding.clients.harvest import HarvestClient
from onboarding.mapping import Eligible, MappingPolicy, Skip, evaluate
from onboarding.models.bamboo import BambooEmployee

log = structlog.get_logger(__name__)


class Action(StrEnum):
    CREATED = "created"
    ALREADY_PRESENT = "already_present"
    SKIPPED = "skipped"
    WOULD_CREATE = "would_create"  # dry run
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    employee_id: str
    action: Action
    reason: str = ""
    harvest_user_id: int | None = None
    email: str | None = None

    @property
    def ok(self) -> bool:
        return self.action is not Action.FAILED


async def sync_employee(
    employee: BambooEmployee,
    *,
    harvest: HarvestClient,
    policy: MappingPolicy,
    dry_run: bool,
    today: date | None = None,
) -> SyncOutcome:
    """Ensure one employee exists in Harvest. Safe to call repeatedly."""
    bound = log.bind(employee_id=employee.id)

    decision = evaluate(employee, policy, today=today)
    if isinstance(decision, Skip):
        bound.info("sync.skipped", reason=decision.reason, detail=decision.detail)
        return SyncOutcome(
            employee.id,
            Action.SKIPPED,
            reason=f"{decision.reason}: {decision.detail}".rstrip(": "),
        )

    assert isinstance(decision, Eligible)
    payload = decision.payload
    bound = bound.bind(email=payload.email)

    # Idempotency check #1: cheap, and catches the overwhelming majority of
    # repeat deliveries.
    existing = await harvest.find_user_by_email(payload.email)
    if existing is not None:
        bound.info("sync.already_present", harvest_user_id=existing.id)
        return SyncOutcome(
            employee.id,
            Action.ALREADY_PRESENT,
            harvest_user_id=existing.id,
            email=payload.email,
        )

    if dry_run:
        bound.info("sync.dry_run", payload=payload.model_dump(exclude_none=True))
        return SyncOutcome(employee.id, Action.WOULD_CREATE, email=payload.email)

    try:
        created = await harvest.create_user(payload)
    except ValidationError as exc:
        # Idempotency check #2: two deliveries racing, or Harvest normalising
        # the address differently than we do. Re-read before calling it a
        # failure -- the desired end state may already hold.
        raced = await harvest.find_user_by_email(payload.email)
        if raced is not None:
            bound.info("sync.already_present_after_race", harvest_user_id=raced.id)
            return SyncOutcome(
                employee.id,
                Action.ALREADY_PRESENT,
                reason="created concurrently",
                harvest_user_id=raced.id,
                email=payload.email,
            )
        bound.error("sync.rejected", detail=str(exc), body=exc.body)
        return SyncOutcome(
            employee.id, Action.FAILED, reason=str(exc), email=payload.email
        )
    except AmbiguousWriteError as exc:
        # The POST may or may not have landed. This is exactly why we never
        # blind-retry a create: re-read and find out.
        settled = await harvest.find_user_by_email(payload.email)
        if settled is not None:
            bound.warning("sync.recovered_ambiguous_write", harvest_user_id=settled.id)
            return SyncOutcome(
                employee.id,
                Action.CREATED,
                reason="recovered after ambiguous write",
                harvest_user_id=settled.id,
                email=payload.email,
            )
        bound.error("sync.ambiguous_write_lost", detail=str(exc))
        return SyncOutcome(
            employee.id, Action.FAILED, reason=str(exc), email=payload.email
        )
    except IntegrationError as exc:
        bound.error("sync.failed", detail=str(exc))
        return SyncOutcome(
            employee.id, Action.FAILED, reason=str(exc), email=payload.email
        )

    bound.info("sync.created", harvest_user_id=created.id)
    return SyncOutcome(
        employee.id, Action.CREATED, harvest_user_id=created.id, email=payload.email
    )


async def sync_employee_ids(
    employee_ids: list[str],
    *,
    bamboo: BambooClient,
    harvest: HarvestClient,
    policy: MappingPolicy,
    dry_run: bool,
    today: date | None = None,
) -> list[SyncOutcome]:
    """Fetch each employee from BambooHR and mirror them.

    Sequential on purpose. The volume is a handful of people, Harvest's rate
    limit is shared across the account, and serial execution makes the logs
    readable when something goes wrong. Concurrency here would buy nothing.
    """
    outcomes: list[SyncOutcome] = []
    for employee_id in employee_ids:
        try:
            employee = await bamboo.get_employee(employee_id)
        except NotFoundError:
            # Deleted between the webhook firing and us reading it.
            log.warning("sync.employee_not_found", employee_id=employee_id)
            outcomes.append(
                SyncOutcome(employee_id, Action.SKIPPED, reason="not found in BambooHR")
            )
            continue
        except IntegrationError as exc:
            log.error("sync.fetch_failed", employee_id=employee_id, detail=str(exc))
            outcomes.append(
                SyncOutcome(employee_id, Action.FAILED, reason=f"fetch failed: {exc}")
            )
            continue

        outcomes.append(
            await sync_employee(
                employee,
                harvest=harvest,
                policy=policy,
                dry_run=dry_run,
                today=today,
            )
        )
    return outcomes
