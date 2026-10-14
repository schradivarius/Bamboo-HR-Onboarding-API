"""Verification of inbound BambooHR webhook deliveries.

BambooHR signs deliveries with HMAC-SHA256 over the request body and the
X-BambooHR-Timestamp value, keyed with the private key returned when the
webhook was created.

THE SIGNED STRING IS DISPUTED. BambooHR's own PHP sample concatenates the raw
body and the timestamp with no separator; several third-party guides use the
Stripe-style "{timestamp}.{body}". Picking wrong rejects every genuine
delivery, so all known variants live here behind one setting, and
`diagnose()` reports which one a real delivery actually used.

To settle it on your first real delivery:
    onboarding diagnose-signature --body-file body.bin \
        --timestamp "<X-BambooHR-Timestamp>" --signature "<X-BambooHR-Signature>"
then set BAMBOO_SIGNATURE_FORMAT to whatever it reports.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable

import structlog

log = structlog.get_logger(__name__)

TIMESTAMP_HEADER = "X-BambooHR-Timestamp"
SIGNATURE_HEADER = "X-BambooHR-Signature"

# Deliveries older than this are refused, so a captured request cannot be
# replayed indefinitely. The timestamp is set when BambooHR *sends*, so this
# only has to cover network time -- but BambooHR batches deliveries, so leave
# some slack rather than tightening it to seconds.
DEFAULT_MAX_SKEW_SECONDS = 300

SignedString = Callable[[bytes, str], bytes]

SIGNATURE_FORMATS: dict[str, SignedString] = {
    # BambooHR's PHP sample: body immediately followed by the timestamp.
    "body_then_timestamp": lambda body, ts: body + ts.encode(),
    # Stripe-style, used by several third-party guides.
    "timestamp_dot_body": lambda body, ts: ts.encode() + b"." + body,
    # Same ordering, no separator.
    "timestamp_then_body": lambda body, ts: ts.encode() + body,
    # Body alone, in case the timestamp is not part of the MAC at all.
    "body_only": lambda body, ts: body,
}


def compute(body: bytes, timestamp: str, private_key: str, fmt: str) -> str:
    """Return the hex HMAC-SHA256 for one signed-string variant."""
    try:
        builder = SIGNATURE_FORMATS[fmt]
    except KeyError:
        raise ValueError(
            f"unknown signature format {fmt!r}; "
            f"expected one of {sorted(SIGNATURE_FORMATS)}"
        ) from None
    return hmac.new(
        private_key.encode(), builder(body, timestamp), hashlib.sha256
    ).hexdigest()


def verify(
    *,
    body: bytes,
    timestamp: str,
    signature: str,
    private_key: str,
    fmt: str = "body_then_timestamp",
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
    now: float | None = None,
) -> bool:
    """True if the delivery is authentic and recent.

    Uses compare_digest so a wrong signature takes the same time to reject as
    a right one, and checks the timestamp so a captured delivery cannot be
    replayed forever.
    """
    try:
        sent_at = float(timestamp)
    except (TypeError, ValueError):
        log.warning("webhook.bad_timestamp", timestamp=timestamp)
        return False

    current = time.time() if now is None else now
    if abs(current - sent_at) > max_skew_seconds:
        log.warning(
            "webhook.stale_timestamp",
            skew_seconds=round(current - sent_at, 1),
            max_skew_seconds=max_skew_seconds,
        )
        return False

    expected = compute(body, timestamp, private_key, fmt)
    if hmac.compare_digest(expected, (signature or "").strip().lower()):
        return True

    # A mismatch is far more likely to be the wrong format than an attack, so
    # make that easy to spot in the logs without leaking the signature.
    others = [
        name
        for name in SIGNATURE_FORMATS
        if name != fmt
        and hmac.compare_digest(
            compute(body, timestamp, private_key, name),
            (signature or "").strip().lower(),
        )
    ]
    if others:
        log.error(
            "webhook.signature_format_mismatch",
            configured=fmt,
            matches=others,
            hint="set BAMBOO_SIGNATURE_FORMAT to the matching value",
        )
    else:
        log.warning("webhook.signature_mismatch", configured=fmt)
    return False


def diagnose(
    *, body: bytes, timestamp: str, signature: str, private_key: str
) -> list[str]:
    """Return every signed-string variant matching this delivery.

    Exactly one match settles the format question. Zero matches means the key
    is wrong, the body was re-serialised somewhere in transit, or BambooHR
    uses a scheme not listed above.
    """
    target = (signature or "").strip().lower()
    return [
        name
        for name in SIGNATURE_FORMATS
        if hmac.compare_digest(compute(body, timestamp, private_key, name), target)
    ]
