"""Harvest client against recorded HTTP, via respx.

These tests pin the response shapes the code assumes. Since the live docs
were unreachable when this was written, a mismatch with the real API should
surface here first -- change a fixture, watch what breaks.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from onboarding.clients.errors import (
    AmbiguousWriteError,
    AuthError,
    RateLimitedError,
    ServerError,
    ValidationError,
)
from onboarding.clients.harvest import BASE_URL, HarvestClient
from onboarding.models.harvest import HarvestUserCreate

PAYLOAD = HarvestUserCreate(
    first_name="Caleb", last_name="Schrader", email="caleb@aurora.example"
)


def user(uid: int, email: str, *, is_active: bool = True) -> dict:
    return {
        "id": uid,
        "first_name": "Caleb",
        "last_name": "Schrader",
        "email": email,
        "is_active": is_active,
    }


def client() -> HarvestClient:
    return HarvestClient(token="tok", account_id="123", user_agent="tests")


@respx.mock(base_url=BASE_URL)
async def test_create_user_sends_required_headers(respx_mock: respx.Router) -> None:
    route = respx_mock.post("/users").mock(
        return_value=httpx.Response(201, json=user(7, "caleb@aurora.example"))
    )
    async with client() as harvest:
        created = await harvest.create_user(PAYLOAD)

    assert created.id == 7
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tok"
    assert request.headers["harvest-account-id"] == "123"
    # Harvest rejects requests without a User-Agent, so this is load-bearing.
    assert request.headers["user-agent"] == "tests"


@respx.mock(base_url=BASE_URL)
async def test_create_user_omits_unset_optionals(respx_mock: respx.Router) -> None:
    # Sending null could be read as "clear this field".
    route = respx_mock.post("/users").mock(
        return_value=httpx.Response(201, json=user(7, "caleb@aurora.example"))
    )
    async with client() as harvest:
        await harvest.create_user(PAYLOAD)

    body = route.calls.last.request.content.decode()
    assert "default_hourly_rate" not in body
    assert "null" not in body


@respx.mock(base_url=BASE_URL)
async def test_duplicate_email_raises_validation_error(
    respx_mock: respx.Router,
) -> None:
    # The behaviour the entire idempotency design rests on.
    respx_mock.post("/users").mock(
        return_value=httpx.Response(422, json={"message": "Email has already been taken"})
    )
    async with client() as harvest:
        with pytest.raises(ValidationError) as caught:
            await harvest.create_user(PAYLOAD)

    assert "already been taken" in str(caught.value)
    assert caught.value.body["message"] == "Email has already been taken"


@respx.mock(base_url=BASE_URL)
async def test_auth_error_does_not_leak_the_token(respx_mock: respx.Router) -> None:
    respx_mock.get("/users").mock(
        return_value=httpx.Response(401, json={"message": "nope"})
    )
    async with client() as harvest:
        with pytest.raises(AuthError) as caught:
            await harvest.list_users()

    assert "tok" not in str(caught.value)


@respx.mock(base_url=BASE_URL)
async def test_auth_error_is_not_retried(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/users").mock(return_value=httpx.Response(403))
    async with client() as harvest:
        with pytest.raises(AuthError):
            await harvest.list_users()

    assert route.call_count == 1


@respx.mock(base_url=BASE_URL)
async def test_rate_limit_is_retried_then_succeeds(respx_mock: respx.Router) -> None:
    respx_mock.get("/users").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"users": [user(1, "a@aurora.example")]}),
        ]
    )
    async with client() as harvest:
        users = await harvest.list_users()

    assert [u.id for u in users] == [1]


@respx.mock(base_url=BASE_URL)
async def test_rate_limit_eventually_gives_up(respx_mock: respx.Router) -> None:
    respx_mock.get("/users").mock(return_value=httpx.Response(429))
    async with client() as harvest:
        with pytest.raises(RateLimitedError):
            await harvest.list_users()


@respx.mock(base_url=BASE_URL)
async def test_server_error_is_retried(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/users").mock(return_value=httpx.Response(503))
    async with client() as harvest:
        with pytest.raises(ServerError):
            await harvest.list_users()

    assert route.call_count == 4


@respx.mock(base_url=BASE_URL)
async def test_post_timeout_is_ambiguous_not_retried(
    respx_mock: respx.Router,
) -> None:
    # A timed-out POST may have landed. Replaying it could create a second
    # user, so it must surface as ambiguous for the caller to resolve.
    route = respx_mock.post("/users").mock(side_effect=httpx.ReadTimeout("timed out"))
    async with client() as harvest:
        with pytest.raises(AmbiguousWriteError):
            await harvest.create_user(PAYLOAD)

    assert route.call_count == 1


@respx.mock(base_url=BASE_URL)
async def test_get_timeout_is_retryable(respx_mock: respx.Router) -> None:
    route = respx_mock.get("/users").mock(side_effect=httpx.ConnectError("boom"))
    async with client() as harvest:
        with pytest.raises(ServerError):
            await harvest.list_users()

    assert route.call_count == 4


@respx.mock(base_url=BASE_URL)
async def test_list_users_follows_pagination(respx_mock: respx.Router) -> None:
    page_two = f"{BASE_URL}/users?page=2&per_page=100"
    route = respx_mock.get("/users").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "users": [user(1, "a@aurora.example")],
                    "links": {"next": page_two},
                },
            ),
            httpx.Response(
                200,
                json={"users": [user(2, "b@aurora.example")], "links": {"next": None}},
            ),
        ]
    )
    async with client() as harvest:
        users = await harvest.list_users()

    assert [u.id for u in users] == [1, 2]
    # The second request must go to the absolute URL Harvest handed us, with
    # its query string intact and our own params not re-appended.
    assert str(route.calls[1].request.url) == page_two


@respx.mock(base_url=BASE_URL)
async def test_pagination_loop_is_broken_not_followed_forever(
    respx_mock: respx.Router,
) -> None:
    # A "next" link pointing back at itself must terminate. Without a guard
    # this hangs the request thread indefinitely -- which is exactly how this
    # bug was found.
    self_ref = f"{BASE_URL}/users?page=1&per_page=100"
    respx_mock.get("/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "users": [user(1, "a@aurora.example")],
                "links": {"next": self_ref},
            },
        )
    )
    async with client() as harvest:
        users = await harvest.list_users()

    # Stops as soon as it recognises the repeat rather than looping.
    assert [u.id for u in users] == [1, 1]  # two distinct URLs, then the repeat


@respx.mock(base_url=BASE_URL)
async def test_find_user_by_email_is_case_insensitive(
    respx_mock: respx.Router,
) -> None:
    respx_mock.get("/users").mock(
        return_value=httpx.Response(
            200, json={"users": [user(5, "Caleb.Schrader@Aurora.Example")]}
        )
    )
    async with client() as harvest:
        found = await harvest.find_user_by_email("caleb.schrader@aurora.example")

    assert found is not None and found.id == 5


@respx.mock(base_url=BASE_URL)
async def test_find_user_by_email_includes_inactive_users(
    respx_mock: respx.Router,
) -> None:
    # A rehire's archived account still occupies the address; reporting it as
    # absent would just earn us a 422 on create.
    respx_mock.get("/users").mock(
        return_value=httpx.Response(
            200,
            json={"users": [user(9, "caleb@aurora.example", is_active=False)]},
        )
    )
    async with client() as harvest:
        found = await harvest.find_user_by_email("caleb@aurora.example")

    assert found is not None and found.is_active is False


@respx.mock(base_url=BASE_URL)
async def test_find_user_by_email_returns_none_when_absent(
    respx_mock: respx.Router,
) -> None:
    respx_mock.get("/users").mock(return_value=httpx.Response(200, json={"users": []}))
    async with client() as harvest:
        assert await harvest.find_user_by_email("nobody@aurora.example") is None


@respx.mock(base_url=BASE_URL)
async def test_unknown_response_fields_are_ignored(respx_mock: respx.Router) -> None:
    # Harvest shipping a new field must not break us.
    payload = user(7, "caleb@aurora.example") | {"brand_new_field": "surprise"}
    respx_mock.post("/users").mock(return_value=httpx.Response(201, json=payload))
    async with client() as harvest:
        assert (await harvest.create_user(PAYLOAD)).id == 7
