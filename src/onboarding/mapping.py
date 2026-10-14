"""Pure BambooHR -> Harvest translation and eligibility rules.

No I/O, no HTTP, no clock of its own. This is where almost all the real
business logic lives, and keeping it pure is what makes it cheap to test
against the awkward cases: preferred names, missing work emails, rehires,
contractors, people whose record exists before their email does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from onboarding.models.bamboo import BambooEmployee
from onboarding.models.harvest import HarvestUserCreate

if TYPE_CHECKING:
    from onboarding.config import Settings


class SkipReason(StrEnum):
    """Why an employee is not mirrored. Every value is a deliberate policy."""

    NO_WORK_EMAIL = "no_work_email"
    MISSING_NAME = "missing_name"
    NOT_ACTIVE = "not_active"
    EXCLUDED_STATUS = "excluded_status"
    NO_HIRE_DATE = "no_hire_date"
    HIRE_DATE_TOO_OLD = "hire_date_too_old"


@dataclass(frozen=True, slots=True)
class MappingPolicy:
    """The narrow slice of configuration the mapping actually needs.

    Depending on this rather than on Settings keeps the pure layer pure and
    makes tests read as data, not setup.
    """

    max_hire_age_days: int = 30
    contractor_statuses: frozenset[str] = frozenset()
    excluded_statuses: frozenset[str] = frozenset()
    default_roles: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            max_hire_age_days=settings.max_hire_age_days,
            contractor_statuses=frozenset(
                s.strip().casefold() for s in settings.bamboo_contractor_statuses
            ),
            excluded_statuses=frozenset(
                s.strip().casefold() for s in settings.bamboo_skipped_statuses
            ),
            default_roles=tuple(settings.harvest_default_roles),
        )


@dataclass(frozen=True, slots=True)
class Eligible:
    payload: HarvestUserCreate


@dataclass(frozen=True, slots=True)
class Skip:
    reason: SkipReason
    detail: str = ""


Decision = Eligible | Skip


def evaluate(
    employee: BambooEmployee,
    policy: MappingPolicy,
    *,
    today: date | None = None,
) -> Decision:
    """Decide whether an employee should exist in Harvest, and as what.

    `today` is injected rather than read from the clock so the hire-date
    window is testable. BambooHR hire dates are naive local dates, so a
    one-day fuzz against UTC is expected and harmless at a 30-day window.
    """
    reference = today or date.today()

    status = (employee.employment_status or "").strip().casefold()
    if status and status in policy.excluded_statuses:
        return Skip(SkipReason.EXCLUDED_STATUS, employee.employment_status or "")

    if not employee.is_active:
        return Skip(SkipReason.NOT_ACTIVE, employee.status or "unknown")

    # No work email means nothing to key on, and email is how we stay
    # idempotent. Common for records created before IT provisions the mailbox,
    # so this is a "come back later", not an error -- the reconcile pass will
    # pick them up once the address exists.
    if not employee.work_email:
        return Skip(SkipReason.NO_WORK_EMAIL)

    first = (employee.preferred_name or employee.first_name or "").strip()
    last = (employee.last_name or "").strip()
    if not first or not last:
        return Skip(SkipReason.MISSING_NAME, employee.display_name())

    if employee.hire_date is None:
        return Skip(SkipReason.NO_HIRE_DATE)

    # Guards the backfill path: without this, one broad webhook or a manual
    # reconcile would try to mirror every employee who ever worked here.
    cutoff = reference - timedelta(days=policy.max_hire_age_days)
    if employee.hire_date < cutoff:
        return Skip(
            SkipReason.HIRE_DATE_TOO_OLD,
            f"hired {employee.hire_date.isoformat()}, cutoff {cutoff.isoformat()}",
        )

    return Eligible(to_harvest_user(employee, policy))


def to_harvest_user(employee: BambooEmployee, policy: MappingPolicy) -> HarvestUserCreate:
    """Translate an eligible employee into a Harvest create-user payload.

    Call evaluate() first -- this assumes email and both names are present.
    """
    email = (employee.work_email or "").strip().lower()
    first = (employee.preferred_name or employee.first_name or "").strip()
    last = (employee.last_name or "").strip()
    status = (employee.employment_status or "").strip().casefold()

    return HarvestUserCreate(
        first_name=first,
        last_name=last,
        email=email,
        is_active=True,
        is_contractor=bool(status) and status in policy.contractor_statuses,
        # Rates are a billing decision nobody has made yet, so we deliberately
        # send nothing and let Harvest apply its account default. See the
        # open questions in README.md.
        roles=list(policy.default_roles) or None,
    )
