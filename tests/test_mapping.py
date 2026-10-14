"""The pure layer, where the awkward human cases live."""

from __future__ import annotations

from datetime import date

from onboarding.mapping import Eligible, MappingPolicy, Skip, SkipReason, evaluate

from .conftest import TODAY, make_employee


def test_happy_path_maps_to_harvest_payload(policy: MappingPolicy) -> None:
    decision = evaluate(make_employee(), policy, today=TODAY)
    assert isinstance(decision, Eligible)
    assert decision.payload.first_name == "Caleb"
    assert decision.payload.last_name == "Schrader"
    assert decision.payload.email == "caleb.schrader@aurora.example"
    assert decision.payload.is_contractor is False


def test_preferred_name_wins_over_legal_first_name(policy: MappingPolicy) -> None:
    decision = evaluate(
        make_employee(firstName="Jonathan", preferredName="Jack"), policy, today=TODAY
    )
    assert isinstance(decision, Eligible)
    assert decision.payload.first_name == "Jack"


def test_blank_preferred_name_falls_back(policy: MappingPolicy) -> None:
    # BambooHR sends "" rather than null, which must not win over firstName.
    decision = evaluate(make_employee(preferredName="   "), policy, today=TODAY)
    assert isinstance(decision, Eligible)
    assert decision.payload.first_name == "Caleb"


def test_email_is_normalised_to_lowercase(policy: MappingPolicy) -> None:
    decision = evaluate(
        make_employee(workEmail="Caleb.Schrader@Aurora.Example"), policy, today=TODAY
    )
    assert isinstance(decision, Eligible)
    assert decision.payload.email == "caleb.schrader@aurora.example"


def test_missing_work_email_is_skipped_not_failed(policy: MappingPolicy) -> None:
    # Very common: the HR record exists before IT provisions the mailbox.
    decision = evaluate(make_employee(workEmail=""), policy, today=TODAY)
    assert decision == Skip(SkipReason.NO_WORK_EMAIL)


def test_contractor_status_sets_is_contractor(policy: MappingPolicy) -> None:
    decision = evaluate(
        make_employee(employmentHistoryStatus="Contractor"), policy, today=TODAY
    )
    assert isinstance(decision, Eligible)
    assert decision.payload.is_contractor is True


def test_contractor_match_is_case_insensitive(policy: MappingPolicy) -> None:
    decision = evaluate(
        make_employee(employmentHistoryStatus="CONTRACTOR"), policy, today=TODAY
    )
    assert isinstance(decision, Eligible)
    assert decision.payload.is_contractor is True


def test_excluded_status_is_skipped(policy: MappingPolicy) -> None:
    decision = evaluate(
        make_employee(employmentHistoryStatus="Terminated"), policy, today=TODAY
    )
    assert isinstance(decision, Skip)
    assert decision.reason is SkipReason.EXCLUDED_STATUS


def test_inactive_employee_is_skipped(policy: MappingPolicy) -> None:
    decision = evaluate(make_employee(status="Inactive"), policy, today=TODAY)
    assert isinstance(decision, Skip)
    assert decision.reason is SkipReason.NOT_ACTIVE


def test_zero_date_sentinel_is_treated_as_absent(policy: MappingPolicy) -> None:
    # "0000-00-00" would otherwise blow up date parsing.
    decision = evaluate(make_employee(hireDate="0000-00-00"), policy, today=TODAY)
    assert isinstance(decision, Skip)
    assert decision.reason is SkipReason.NO_HIRE_DATE


def test_old_hire_date_is_skipped(policy: MappingPolicy) -> None:
    decision = evaluate(make_employee(hireDate="2020-01-01"), policy, today=TODAY)
    assert isinstance(decision, Skip)
    assert decision.reason is SkipReason.HIRE_DATE_TOO_OLD


def test_hire_date_exactly_on_cutoff_is_eligible(policy: MappingPolicy) -> None:
    cutoff = date(2026, 9, 14)  # TODAY - 30 days
    decision = evaluate(make_employee(hireDate=cutoff.isoformat()), policy, today=TODAY)
    assert isinstance(decision, Eligible)


def test_future_hire_date_is_eligible(policy: MappingPolicy) -> None:
    # Pre-boarding: the account should exist before day one.
    decision = evaluate(make_employee(hireDate="2026-12-01"), policy, today=TODAY)
    assert isinstance(decision, Eligible)


def test_missing_surname_is_skipped(policy: MappingPolicy) -> None:
    decision = evaluate(make_employee(lastName=""), policy, today=TODAY)
    assert isinstance(decision, Skip)
    assert decision.reason is SkipReason.MISSING_NAME


def test_default_roles_are_applied(policy: MappingPolicy) -> None:
    with_roles = MappingPolicy(
        max_hire_age_days=policy.max_hire_age_days,
        contractor_statuses=policy.contractor_statuses,
        excluded_statuses=policy.excluded_statuses,
        default_roles=("Employee",),
    )
    decision = evaluate(make_employee(), with_roles, today=TODAY)
    assert isinstance(decision, Eligible)
    assert decision.payload.roles == ["Employee"]
