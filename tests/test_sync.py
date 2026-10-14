"""Orchestration and idempotency.

Uses a hand-rolled fake rather than respx: the point of these tests is the
decision logic around failures, and injecting an exception is clearer than
scripting HTTP to produce one.
"""

from __future__ import annotations

from onboarding.clients.errors import (
    AmbiguousWriteError,
    AuthError,
    ValidationError,
)
from onboarding.mapping import MappingPolicy
from onboarding.models.harvest import HarvestUser, HarvestUserCreate
from onboarding.sync import Action, sync_employee

from .conftest import TODAY, make_employee


class FakeHarvest:
    """Stands in for HarvestClient.

    `existing` is the account state; `create_raises` lets a test force the
    failure path. `created_from_second_lookup` simulates a user appearing
    between our first lookup and our create -- the race the retry logic has to
    survive.
    """

    def __init__(
        self,
        *,
        existing: list[HarvestUser] | None = None,
        create_raises: Exception | None = None,
        appears_after_failure: HarvestUser | None = None,
    ) -> None:
        self.existing = existing or []
        self.create_raises = create_raises
        self.appears_after_failure = appears_after_failure
        self.create_calls: list[HarvestUserCreate] = []
        self.lookup_calls: list[str] = []
        self._failed_once = False

    async def find_user_by_email(self, email: str) -> HarvestUser | None:
        self.lookup_calls.append(email)
        if self._failed_once and self.appears_after_failure is not None:
            return self.appears_after_failure
        target = email.strip().lower()
        return next((u for u in self.existing if u.email.strip().lower() == target), None)

    async def create_user(self, payload: HarvestUserCreate) -> HarvestUser:
        self.create_calls.append(payload)
        if self.create_raises is not None:
            self._failed_once = True
            raise self.create_raises
        created = HarvestUser(
            id=999,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            is_active=True,
        )
        self.existing.append(created)
        return created


def harvest_user(uid: int, email: str) -> HarvestUser:
    return HarvestUser(
        id=uid, first_name="Caleb", last_name="Schrader", email=email, is_active=True
    )


async def test_creates_a_missing_user(policy: MappingPolicy) -> None:
    harvest = FakeHarvest()
    outcome = await sync_employee(
        make_employee(), harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )

    assert outcome.action is Action.CREATED
    assert outcome.harvest_user_id == 999
    assert len(harvest.create_calls) == 1


async def test_existing_user_is_not_recreated(policy: MappingPolicy) -> None:
    harvest = FakeHarvest(existing=[harvest_user(5, "caleb.schrader@aurora.example")])
    outcome = await sync_employee(
        make_employee(), harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )

    assert outcome.action is Action.ALREADY_PRESENT
    assert outcome.harvest_user_id == 5
    assert harvest.create_calls == []


async def test_repeated_delivery_is_idempotent(policy: MappingPolicy) -> None:
    # The property that matters most: replaying the same webhook must not
    # produce a second Harvest user.
    harvest = FakeHarvest()
    employee = make_employee()

    first = await sync_employee(
        employee, harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )
    second = await sync_employee(
        employee, harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )

    assert first.action is Action.CREATED
    assert second.action is Action.ALREADY_PRESENT
    assert len(harvest.create_calls) == 1


async def test_case_mismatch_still_counts_as_present(policy: MappingPolicy) -> None:
    harvest = FakeHarvest(existing=[harvest_user(5, "Caleb.Schrader@Aurora.Example")])
    outcome = await sync_employee(
        make_employee(), harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )

    assert outcome.action is Action.ALREADY_PRESENT
    assert harvest.create_calls == []


async def test_dry_run_never_writes(policy: MappingPolicy) -> None:
    harvest = FakeHarvest()
    outcome = await sync_employee(
        make_employee(), harvest=harvest, policy=policy, dry_run=True, today=TODAY
    )

    assert outcome.action is Action.WOULD_CREATE
    assert harvest.create_calls == []


async def test_lost_race_resolves_to_already_present(policy: MappingPolicy) -> None:
    # Two deliveries in flight: our lookup said absent, the create was
    # rejected, and a re-read shows the other delivery won. Desired end state
    # holds, so this is success, not failure.
    harvest = FakeHarvest(
        create_raises=ValidationError("Email has already been taken"),
        appears_after_failure=harvest_user(77, "caleb.schrader@aurora.example"),
    )
    outcome = await sync_employee(
        make_employee(), harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )

    assert outcome.action is Action.ALREADY_PRESENT
    assert outcome.harvest_user_id == 77


async def test_genuine_rejection_is_a_failure(policy: MappingPolicy) -> None:
    # Rejected, and still absent on re-read -- so something is actually wrong.
    harvest = FakeHarvest(create_raises=ValidationError("first_name is required"))
    outcome = await sync_employee(
        make_employee(), harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )

    assert outcome.action is Action.FAILED
    assert not outcome.ok


async def test_ambiguous_write_that_landed_is_reported_as_created(
    policy: MappingPolicy,
) -> None:
    # The POST timed out but did take effect. Re-reading is what turns a
    # scary ambiguity into a correct answer.
    harvest = FakeHarvest(
        create_raises=AmbiguousWriteError("timed out"),
        appears_after_failure=harvest_user(88, "caleb.schrader@aurora.example"),
    )
    outcome = await sync_employee(
        make_employee(), harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )

    assert outcome.action is Action.CREATED
    assert outcome.harvest_user_id == 88
    # Crucially: we did NOT replay the create.
    assert len(harvest.create_calls) == 1


async def test_ambiguous_write_that_did_not_land_is_a_failure(
    policy: MappingPolicy,
) -> None:
    harvest = FakeHarvest(create_raises=AmbiguousWriteError("timed out"))
    outcome = await sync_employee(
        make_employee(), harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )

    assert outcome.action is Action.FAILED
    assert len(harvest.create_calls) == 1


async def test_auth_failure_is_reported_not_swallowed(policy: MappingPolicy) -> None:
    harvest = FakeHarvest(create_raises=AuthError("bad token"))
    outcome = await sync_employee(
        make_employee(), harvest=harvest, policy=policy, dry_run=False, today=TODAY
    )

    assert outcome.action is Action.FAILED
    assert "bad token" in outcome.reason


async def test_skipped_employee_never_touches_harvest(policy: MappingPolicy) -> None:
    harvest = FakeHarvest()
    outcome = await sync_employee(
        make_employee(workEmail=""),
        harvest=harvest,
        policy=policy,
        dry_run=False,
        today=TODAY,
    )

    assert outcome.action is Action.SKIPPED
    # No lookup either -- skipping is decided purely from the BambooHR record.
    assert harvest.lookup_calls == []
