"""BambooHR client -- mostly pinning the quirks the models have to absorb."""

from __future__ import annotations

import httpx
import pytest
import respx

from onboarding.clients.bamboo import BambooClient, base_url
from onboarding.clients.errors import AuthError, NotFoundError

BASE = base_url("aurora")

EMPLOYEE = {
    "id": "42",
    "firstName": "Caleb",
    "lastName": "Schrader",
    "preferredName": "",
    "workEmail": "caleb@aurora.example",
    "hireDate": "2026-10-01",
    "status": "Active",
    "employmentHistoryStatus": "Full-Time",
    "terminationDate": "0000-00-00",
}


def client() -> BambooClient:
    return BambooClient(api_key="key", subdomain="aurora")


@respx.mock(base_url=BASE)
async def test_get_employee_requests_explicit_fields(
    respx_mock: respx.Router,
) -> None:
    # BambooHR returns only the fields you ask for, so the request must carry
    # them or we silently get almost nothing back.
    route = respx_mock.get("/employees/42").mock(
        return_value=httpx.Response(200, json=EMPLOYEE)
    )
    async with client() as bamboo:
        employee = await bamboo.get_employee("42")

    assert employee.work_email == "caleb@aurora.example"
    requested = route.calls.last.request.url.params["fields"]
    assert "workEmail" in requested and "hireDate" in requested


@respx.mock(base_url=BASE)
async def test_uses_basic_auth_with_api_key_as_username(
    respx_mock: respx.Router,
) -> None:
    route = respx_mock.get("/employees/42").mock(
        return_value=httpx.Response(200, json=EMPLOYEE)
    )
    async with client() as bamboo:
        await bamboo.get_employee("42")

    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


@respx.mock(base_url=BASE)
async def test_blank_and_zero_date_sentinels_become_none(
    respx_mock: respx.Router,
) -> None:
    respx_mock.get("/employees/42").mock(return_value=httpx.Response(200, json=EMPLOYEE))
    async with client() as bamboo:
        employee = await bamboo.get_employee("42")

    assert employee.preferred_name is None
    assert employee.termination_date is None


@respx.mock(base_url=BASE)
async def test_integer_id_is_coerced_to_string(respx_mock: respx.Router) -> None:
    respx_mock.get("/employees/42").mock(
        return_value=httpx.Response(200, json=EMPLOYEE | {"id": 42})
    )
    async with client() as bamboo:
        assert (await bamboo.get_employee("42")).id == "42"


@respx.mock(base_url=BASE)
async def test_error_message_header_is_surfaced(respx_mock: respx.Router) -> None:
    # BambooHR puts the useful detail in a header and HTML in the body.
    respx_mock.get("/employees/99").mock(
        return_value=httpx.Response(
            404,
            headers={"X-BambooHR-Error-Message": "Employee not found"},
            text="<html>...</html>",
        )
    )
    async with client() as bamboo:
        with pytest.raises(NotFoundError, match="Employee not found"):
            await bamboo.get_employee("99")


@respx.mock(base_url=BASE)
async def test_auth_error_does_not_leak_the_key(respx_mock: respx.Router) -> None:
    respx_mock.get("/employees/42").mock(return_value=httpx.Response(401))
    async with client() as bamboo:
        with pytest.raises(AuthError) as caught:
            await bamboo.get_employee("42")

    assert "key" not in str(caught.value).replace("credentials", "")


@respx.mock(base_url=BASE)
async def test_list_employees_reads_custom_report(respx_mock: respx.Router) -> None:
    respx_mock.post("/reports/custom").mock(
        return_value=httpx.Response(
            200, json={"fields": [], "employees": [EMPLOYEE, EMPLOYEE | {"id": "43"}]}
        )
    )
    async with client() as bamboo:
        employees = await bamboo.list_employees()

    assert [e.id for e in employees] == ["42", "43"]
