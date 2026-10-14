"""The webhook route, and the defensive id extraction it depends on."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from onboarding.api import app, extract_employee_ids
from onboarding.webhook_auth import compute

KEY = "test-private-key"


# --- id extraction ----------------------------------------------------------
# Payload schema could not be confirmed from the docs, so these cases cover
# every shape reported anywhere. Being permissive is safe because ids are the
# ONLY thing taken from the payload.


def test_extracts_from_batched_employees_list() -> None:
    payload = {"employees": [{"id": 1}, {"id": 2}]}
    assert extract_employee_ids(payload) == ["1", "2"]


def test_extracts_from_single_event_data_object() -> None:
    payload = {"type": "employee.updated", "data": {"employeeId": 42}}
    assert extract_employee_ids(payload) == ["42"]


def test_extracts_from_nested_fields_object() -> None:
    payload = {"employees": [{"fields": {"id": "7", "firstName": "Caleb"}}]}
    assert extract_employee_ids(payload) == ["7"]


def test_extracts_from_bare_list() -> None:
    assert extract_employee_ids([{"id": 3}, {"employee_id": 4}]) == ["3", "4"]


def test_extracts_from_flat_object() -> None:
    assert extract_employee_ids({"employeeId": 11}) == ["11"]


def test_deduplicates_repeated_ids() -> None:
    # A batch can mention the same person twice if several fields changed.
    payload = {"employees": [{"id": 5}, {"id": 5}, {"id": 6}]}
    assert extract_employee_ids(payload) == ["5", "6"]


def test_coerces_integer_ids_to_strings() -> None:
    # The REST API returns string ids; webhooks have been seen to send ints.
    assert extract_employee_ids({"employees": [{"id": 8}]}) == ["8"]


@pytest.mark.parametrize(
    "payload",
    [{}, {"employees": []}, {"employees": [{}]}, [], {"data": {}}, "nonsense"],
)
def test_returns_empty_for_unrecognised_payloads(payload: Any) -> None:
    assert extract_employee_ids(payload) == []


# --- route ------------------------------------------------------------------


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Environment wins over any local .env, so these tests are deterministic
    # on a developer machine with real credentials present.
    monkeypatch.setenv("HARVEST_TOKEN", "tok")
    monkeypatch.setenv("HARVEST_ACCOUNT_ID", "123")
    monkeypatch.setenv("HARVEST_USER_AGENT", "tests")
    monkeypatch.setenv("BAMBOO_API_KEY", "key")
    monkeypatch.setenv("BAMBOO_SUBDOMAIN", "aurora")
    monkeypatch.setenv("BAMBOO_WEBHOOK_PRIVATE_KEY", KEY)
    monkeypatch.setenv("BAMBOO_SIGNATURE_FORMAT", "body_then_timestamp")
    monkeypatch.setenv("DRY_RUN", "true")


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture what the route hands to the sync layer, without any network."""
    calls: list[list[str]] = []

    async def fake_sync(employee_ids: list[str], **_: Any) -> list:
        calls.append(employee_ids)
        return []

    monkeypatch.setattr("onboarding.api.sync_employee_ids", fake_sync)
    return calls


def post(client: TestClient, body: bytes, *, fmt: str = "body_then_timestamp"):
    ts = str(int(time.time()))
    return client.post(
        "/webhooks/bamboohr",
        content=body,
        headers={
            "X-BambooHR-Timestamp": ts,
            "X-BambooHR-Signature": compute(body, ts, KEY, fmt),
            "Content-Type": "application/json",
        },
    )


def test_healthz_reports_dry_run(configured: None) -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dry_run": True}


def test_signed_delivery_is_accepted_and_dispatched(
    configured: None, recorded: list[list[str]]
) -> None:
    with TestClient(app) as client:
        response = post(client, b'{"employees":[{"id":42}]}')

    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "employee_ids": ["42"]}
    # TestClient runs background tasks before returning.
    assert recorded == [["42"]]


def test_unsigned_delivery_is_rejected(
    configured: None, recorded: list[list[str]]
) -> None:
    with TestClient(app) as client:
        response = client.post("/webhooks/bamboohr", content=b"{}")

    assert response.status_code == 401
    assert recorded == []


def test_wrong_signature_is_rejected(configured: None, recorded: list[list[str]]) -> None:
    body = b'{"employees":[{"id":42}]}'
    ts = str(int(time.time()))
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/bamboohr",
            content=body,
            headers={
                "X-BambooHR-Timestamp": ts,
                "X-BambooHR-Signature": "deadbeef",
            },
        )

    assert response.status_code == 401
    assert recorded == []


def test_signature_from_the_wrong_format_is_rejected(
    configured: None, recorded: list[list[str]]
) -> None:
    # Genuine delivery, right key, wrong configured format. This is the exact
    # symptom to look for if real deliveries start 401ing.
    with TestClient(app) as client:
        response = post(client, b'{"employees":[{"id":42}]}', fmt="timestamp_dot_body")

    assert response.status_code == 401
    assert recorded == []


def test_tampered_body_is_rejected(configured: None, recorded: list[list[str]]) -> None:
    ts = str(int(time.time()))
    original = b'{"employees":[{"id":42}]}'
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/bamboohr",
            content=b'{"employees":[{"id":99}]}',
            headers={
                "X-BambooHR-Timestamp": ts,
                "X-BambooHR-Signature": compute(original, ts, KEY, "body_then_timestamp"),
            },
        )

    assert response.status_code == 401
    assert recorded == []


def test_missing_private_key_fails_closed(
    configured: None, monkeypatch: pytest.MonkeyPatch, recorded: list[list[str]]
) -> None:
    # An unauthenticated endpoint that creates billable users is not something
    # to leave open, so no key must mean no service.
    monkeypatch.delenv("BAMBOO_WEBHOOK_PRIVATE_KEY")
    with TestClient(app) as client:
        response = client.post("/webhooks/bamboohr", content=b"{}")

    assert response.status_code == 503
    assert recorded == []


def test_malformed_json_is_rejected(configured: None, recorded: list[list[str]]) -> None:
    with TestClient(app) as client:
        response = post(client, b"not json at all")

    assert response.status_code == 400
    assert recorded == []


def test_signed_payload_without_ids_is_acknowledged_not_dispatched(
    configured: None, recorded: list[list[str]]
) -> None:
    # Authentic but unrecognised: ack it so BambooHR stops retrying, log loudly
    # (the route logs webhook.no_employee_ids), and dispatch nothing.
    with TestClient(app) as client:
        response = post(client, b'{"unexpected":"shape"}')

    assert response.status_code == 202
    assert response.json()["accepted"] == 0
    assert recorded == []
