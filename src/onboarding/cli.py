"""Command line entrypoints.

The CLI is not an afterthought -- it is how you operate this thing: run a
backfill, re-sync one person, settle the webhook signature question, or prove
credentials work without exposing a public endpoint at all.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import structlog
from pydantic import ValidationError as ConfigValidationError

from onboarding.clients.bamboo import BambooClient
from onboarding.clients.harvest import HarvestClient
from onboarding.config import Settings
from onboarding.logging import configure_logging
from onboarding.mapping import Eligible, MappingPolicy, evaluate
from onboarding.sync import Action, SyncOutcome, sync_employee, sync_employee_ids
from onboarding.webhook_auth import diagnose

log = structlog.get_logger(__name__)


def _report(outcomes: list[SyncOutcome]) -> int:
    for outcome in outcomes:
        marker = {
            Action.CREATED: "created",
            Action.ALREADY_PRESENT: "exists ",
            Action.SKIPPED: "skipped",
            Action.WOULD_CREATE: "DRYRUN ",
            Action.FAILED: "FAILED ",
        }[outcome.action]
        detail = f" ({outcome.reason})" if outcome.reason else ""
        target = (
            f" -> harvest:{outcome.harvest_user_id}" if outcome.harvest_user_id else ""
        )
        print(f"  [{marker}] employee {outcome.employee_id}{target}{detail}")

    failures = [o for o in outcomes if not o.ok]
    print(f"\n{len(outcomes)} processed, {len(failures)} failed")
    return 1 if failures else 0


async def _sync(employee_ids: list[str], *, dry_run: bool) -> int:
    settings = _load_settings()
    configure_logging(level=settings.log_level, json_logs=False)
    policy = MappingPolicy.from_settings(settings)

    async with (
        BambooClient(
            api_key=settings.bamboo_api_key.get_secret_value(),
            subdomain=settings.bamboo_subdomain,
        ) as bamboo,
        HarvestClient(
            token=settings.harvest_token.get_secret_value(),
            account_id=settings.harvest_account_id,
            user_agent=settings.harvest_user_agent,
        ) as harvest,
    ):
        outcomes = await sync_employee_ids(
            employee_ids,
            bamboo=bamboo,
            harvest=harvest,
            policy=policy,
            dry_run=dry_run,
        )
    return _report(outcomes)


async def _backfill(*, days: int | None, dry_run: bool) -> int:
    """Reconcile: mirror everyone hired recently who is missing from Harvest.

    This is the safety net that makes missed or malformed webhooks survivable,
    and it is the reason the webhook path is allowed to be best-effort.
    """
    settings = _load_settings()
    configure_logging(level=settings.log_level, json_logs=False)
    policy = MappingPolicy.from_settings(settings)
    if days is not None:
        policy = replace(policy, max_hire_age_days=days)

    async with (
        BambooClient(
            api_key=settings.bamboo_api_key.get_secret_value(),
            subdomain=settings.bamboo_subdomain,
        ) as bamboo,
        HarvestClient(
            token=settings.harvest_token.get_secret_value(),
            account_id=settings.harvest_account_id,
            user_agent=settings.harvest_user_agent,
        ) as harvest,
    ):
        employees = await bamboo.list_employees()
        cutoff = date.today() - timedelta(days=policy.max_hire_age_days)
        print(f"{len(employees)} employees in BambooHR; cutoff {cutoff.isoformat()}")

        # Pre-filter with the pure rules so we only pay for a Harvest lookup
        # on people who could plausibly need creating. list_users() is a
        # paginated scan per candidate, so this filter is the difference
        # between a handful of calls and hundreds.
        candidates = [
            employee
            for employee in employees
            if isinstance(evaluate(employee, policy), Eligible)
        ]
        print(f"{len(candidates)} eligible candidates\n")

        outcomes = [
            await sync_employee(employee, harvest=harvest, policy=policy, dry_run=dry_run)
            for employee in candidates
        ]
    return _report(outcomes)


async def _smoke() -> int:
    """Prove the Harvest half end to end, including the duplicate rejection
    the idempotency design depends on."""
    from onboarding.clients.errors import ValidationError
    from onboarding.models.harvest import HarvestUserCreate

    settings = _load_settings()
    configure_logging(level=settings.log_level, json_logs=False)
    test_email = "smoke.test@aurora.example"

    async with HarvestClient(
        token=settings.harvest_token.get_secret_value(),
        account_id=settings.harvest_account_id,
        user_agent=settings.harvest_user_agent,
    ) as harvest:
        users = await harvest.list_users()
        print(f"[ok] auth + pagination work -- {len(users)} users visible")

        if await harvest.find_user_by_email(test_email):
            print(f"[!!] {test_email} already exists; archive it and rerun")
            return 1

        if settings.dry_run:
            print("[--] DRY_RUN=true, stopping before any write")
            print("     set DRY_RUN=false to run the create/duplicate checks")
            return 0

        created = await harvest.create_user(
            HarvestUserCreate(first_name="Smoke", last_name="Test", email=test_email)
        )
        print(f"[ok] created harvest user id={created.id}")

        found = await harvest.find_user_by_email(test_email)
        if not found or found.id != created.id:
            print("[XX] find_user_by_email did not return the user we just created")
            return 1
        print("[ok] idempotency lookup finds the new user")

        try:
            await harvest.create_user(
                HarvestUserCreate(first_name="Smoke", last_name="Test", email=test_email)
            )
        except ValidationError as exc:
            print(f"[ok] duplicate rejected as expected -- {exc}")
        else:
            print(
                "[XX] DUPLICATE WAS ACCEPTED.\n"
                "     Harvest permits duplicate emails, so email-as-idempotency-key\n"
                "     does not hold. Stop and add a local sync-state store."
            )
            return 1

        print(f"\nDone. Now archive harvest user {created.id}.")
    return 0


def _diagnose(args: argparse.Namespace) -> int:
    settings = _load_settings()
    if settings.bamboo_webhook_private_key is None:
        print("BAMBOO_WEBHOOK_PRIVATE_KEY is not set", file=sys.stderr)
        return 1

    body = Path(args.body_file).read_bytes()
    matches = diagnose(
        body=body,
        timestamp=args.timestamp,
        signature=args.signature,
        private_key=settings.bamboo_webhook_private_key.get_secret_value(),
    )
    if not matches:
        print(
            "No signed-string variant matched.\n"
            "Likely causes: wrong private key, the body was not captured raw,\n"
            "or BambooHR uses a scheme not listed in webhook_auth.py."
        )
        return 1
    print("Matched variant(s):", ", ".join(matches))
    print(f"\nSet BAMBOO_SIGNATURE_FORMAT={matches[0]}")
    return 0


def _load_settings() -> Settings:
    """Load config, or explain what is missing and exit.

    A pydantic traceback is not a useful thing to hand an operator at 7am.
    """
    try:
        return Settings()
    except ConfigValidationError as exc:
        missing = sorted(
            str(err["loc"][0]).upper() for err in exc.errors() if err["type"] == "missing"
        )
        print("Configuration is incomplete.\n", file=sys.stderr)
        if missing:
            print("Missing required settings:", file=sys.stderr)
            for name in missing:
                print(f"  - {name}", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        print(
            "\nCopy .env.example to .env and fill it in, or export these as"
            " environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onboarding")
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually write to Harvest (overrides DRY_RUN=true)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="mirror specific BambooHR employee ids")
    p_sync.add_argument("employee_ids", nargs="+")

    p_backfill = sub.add_parser(
        "backfill", help="reconcile everyone hired recently (the safety net)"
    )
    p_backfill.add_argument(
        "--days", type=int, default=None, help="override MAX_HIRE_AGE_DAYS"
    )

    sub.add_parser("smoke-harvest", help="prove the Harvest half works")

    p_diag = sub.add_parser(
        "diagnose-signature", help="work out which signed-string format BambooHR uses"
    )
    p_diag.add_argument("--body-file", required=True, help="raw request body bytes")
    p_diag.add_argument("--timestamp", required=True, help="X-BambooHR-Timestamp")
    p_diag.add_argument("--signature", required=True, help="X-BambooHR-Signature")

    args = parser.parse_args(argv)

    if args.command == "diagnose-signature":
        return _diagnose(args)

    dry_run = _load_settings().dry_run and not args.live
    if args.command == "sync":
        return asyncio.run(_sync(args.employee_ids, dry_run=dry_run))
    if args.command == "backfill":
        return asyncio.run(_backfill(days=args.days, dry_run=dry_run))
    if args.command == "smoke-harvest":
        return asyncio.run(_smoke())
    parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
