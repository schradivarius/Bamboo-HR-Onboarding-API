from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_none

from onboarding.clients.errors import RetryableError
from onboarding.mapping import MappingPolicy
from onboarding.models.bamboo import BambooEmployee

TODAY = date(2026, 10, 14)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry logic under test but drop the backoff waits.

    Without this, exercising the 429 path makes the suite sleep for seconds.
    """

    def instant(attempts: int = 4) -> AsyncRetrying:
        return AsyncRetrying(
            retry=retry_if_exception_type(RetryableError),
            wait=wait_none(),
            stop=stop_after_attempt(attempts),
            reraise=True,
        )

    monkeypatch.setattr("onboarding.clients._http.retrying", instant)
    monkeypatch.setattr("onboarding.clients.harvest.retrying", instant)
    monkeypatch.setattr("onboarding.clients.bamboo.retrying", instant)


@pytest.fixture
def policy() -> MappingPolicy:
    return MappingPolicy(
        max_hire_age_days=30,
        contractor_statuses=frozenset({"contractor", "temp", "intern"}),
        excluded_statuses=frozenset({"terminated"}),
        default_roles=(),
    )


def make_employee(**overrides: Any) -> BambooEmployee:
    """Build a BambooHR employee from raw API-shaped values.

    Takes aliases (firstName, workEmail, ...) on purpose so the tests feed the
    models the same shapes the real API does, including the "" and
    "0000-00-00" sentinels.
    """
    raw: dict[str, Any] = {
        "id": "42",
        "firstName": "Caleb",
        "lastName": "Schrader",
        "preferredName": "",
        "workEmail": "caleb.schrader@aurora.example",
        "hireDate": "2026-10-01",
        "jobTitle": "Systems Engineer",
        "department": "Technical Services",
        "status": "Active",
        "employmentHistoryStatus": "Full-Time",
        "terminationDate": "0000-00-00",
    }
    raw.update(overrides)
    return BambooEmployee.model_validate(raw)
