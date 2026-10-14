"""Signature verification -- including the format question we could not
settle from the docs."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from onboarding.webhook_auth import SIGNATURE_FORMATS, compute, diagnose, verify

KEY = "super-secret-private-key"
BODY = b'{"employees":[{"id":42}]}'
TS = "1760000000"
NOW = 1760000010.0


def sign(fmt: str) -> str:
    return compute(BODY, TS, KEY, fmt)


@pytest.mark.parametrize("fmt", sorted(SIGNATURE_FORMATS))
def test_every_format_round_trips(fmt: str) -> None:
    assert verify(
        body=BODY,
        timestamp=TS,
        signature=sign(fmt),
        private_key=KEY,
        fmt=fmt,
        now=NOW,
    )


def test_bamboo_php_sample_format_is_body_then_timestamp() -> None:
    # Pins the documented-PHP-sample interpretation so a future edit cannot
    # silently redefine the default.
    expected = hmac.new(KEY.encode(), BODY + TS.encode(), hashlib.sha256).hexdigest()
    assert compute(BODY, TS, KEY, "body_then_timestamp") == expected


def test_stripe_style_format_is_distinct() -> None:
    # If these ever collide, diagnose() stops being able to tell them apart.
    assert sign("body_then_timestamp") != sign("timestamp_dot_body")


def test_wrong_key_is_rejected() -> None:
    assert not verify(
        body=BODY,
        timestamp=TS,
        signature=sign("body_then_timestamp"),
        private_key="not-the-key",
        fmt="body_then_timestamp",
        now=NOW,
    )


def test_tampered_body_is_rejected() -> None:
    assert not verify(
        body=b'{"employees":[{"id":99}]}',
        timestamp=TS,
        signature=sign("body_then_timestamp"),
        private_key=KEY,
        fmt="body_then_timestamp",
        now=NOW,
    )


def test_mismatched_format_is_rejected_even_though_signature_is_genuine() -> None:
    # This is the exact failure mode the docs ambiguity would cause: a real
    # delivery, a real key, and a rejected request.
    assert not verify(
        body=BODY,
        timestamp=TS,
        signature=sign("timestamp_dot_body"),
        private_key=KEY,
        fmt="body_then_timestamp",
        now=NOW,
    )


def test_stale_delivery_is_rejected() -> None:
    assert not verify(
        body=BODY,
        timestamp=TS,
        signature=sign("body_then_timestamp"),
        private_key=KEY,
        fmt="body_then_timestamp",
        max_skew_seconds=300,
        now=float(TS) + 3600,
    )


def test_future_dated_delivery_is_rejected() -> None:
    # Clock skew cuts both ways; a far-future timestamp is just as suspect.
    assert not verify(
        body=BODY,
        timestamp=TS,
        signature=sign("body_then_timestamp"),
        private_key=KEY,
        fmt="body_then_timestamp",
        max_skew_seconds=300,
        now=float(TS) - 3600,
    )


def test_non_numeric_timestamp_is_rejected() -> None:
    assert not verify(
        body=BODY,
        timestamp="not-a-timestamp",
        signature=sign("body_then_timestamp"),
        private_key=KEY,
        fmt="body_then_timestamp",
        now=NOW,
    )


def test_missing_signature_is_rejected() -> None:
    assert not verify(body=BODY, timestamp=TS, signature="", private_key=KEY, now=NOW)


def test_uppercase_signature_is_accepted() -> None:
    assert verify(
        body=BODY,
        timestamp=TS,
        signature=sign("body_then_timestamp").upper(),
        private_key=KEY,
        fmt="body_then_timestamp",
        now=NOW,
    )


def test_diagnose_identifies_the_real_format() -> None:
    matches = diagnose(
        body=BODY, timestamp=TS, signature=sign("timestamp_dot_body"), private_key=KEY
    )
    assert matches == ["timestamp_dot_body"]


def test_diagnose_returns_nothing_for_a_bad_key() -> None:
    assert (
        diagnose(
            body=BODY,
            timestamp=TS,
            signature=sign("body_then_timestamp"),
            private_key="wrong",
        )
        == []
    )


def test_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match="unknown signature format"):
        compute(BODY, TS, KEY, "invented")


def test_verify_uses_real_clock_by_default() -> None:
    now_ts = str(int(time.time()))
    assert verify(
        body=BODY,
        timestamp=now_ts,
        signature=compute(BODY, now_ts, KEY, "body_then_timestamp"),
        private_key=KEY,
    )
