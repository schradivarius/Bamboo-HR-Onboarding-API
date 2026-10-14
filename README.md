# BambooHR → Harvest onboarding

When a new employee appears in BambooHR, create the matching user in Harvest.

Built for Aurora Technical Services. Low volume by design — the hard parts here
are idempotency and credential handling, not throughput.

## How it works

```
BambooHR webhook ──▶ POST /webhooks/bamboohr
                       │  verify HMAC signature
                       │  extract employee ids ONLY
                       ▼
                     BambooHR API  (re-read the authoritative record)
                       │
                       ▼
                     mapping.py    (pure: eligibility + field translation)
                       │
                       ▼
                     Harvest API   (look up by email, then create)
```

Two decisions shape everything else:

**The webhook is a notification, not data.** We take employee *ids* from the
payload and nothing else, then re-read each employee from the BambooHR API.
BambooHR's payload schema could not be confirmed (see *Unverified
assumptions*), so this means a schema change can cost us a missed
notification — which `onboarding backfill` repairs — but can never write wrong
data to Harvest.

**Harvest is the sync-state store.** There is no local database. Before
creating a user we look them up by email; if they exist, we stop. Harvest is
the system a double-write would corrupt, so it is the only source of truth
that cannot drift. Add a database when you need audit history, not before.

## Quick start

```bash
uv sync
cp .env.example .env     # then fill it in
uv run pytest            # 89 tests, no network required
```

`DRY_RUN` defaults to **true**. Every Harvest user is a billable seat, so
writing is something you opt into:

```bash
uv run onboarding sync 42              # dry run: shows what it would create
uv run onboarding --live sync 42       # actually creates
```

## Operating it

| Command | What it does |
| --- | --- |
| `onboarding sync <id>...` | Mirror specific BambooHR employees. |
| `onboarding backfill [--days N]` | Reconcile everyone hired recently. The safety net for missed webhooks. |
| `onboarding smoke-harvest` | Prove Harvest credentials, pagination, and duplicate rejection work. |
| `onboarding diagnose-signature` | Work out which signed-string format BambooHR actually uses. |

Add `--live` to any of them to write for real.

### Running it

```bash
docker compose up                      # api on :8000
docker compose --profile tunnel up     # plus a public HTTPS URL for the webhook
docker compose logs tunnel             # the *.trycloudflare.com URL prints here
```

Quick tunnels get a **new random hostname on every restart**, and the webhook
URL is registered server-side at BambooHR — so for anything beyond a one-off
test, create a named Cloudflare tunnel and point it at a stable hostname.

Better still for the dev loop: capture one real delivery and replay it with
`curl`. BambooHR batches webhooks on a frequency limit, so they do not fire
the moment you click save, and debugging through that delay is miserable.

## Unverified assumptions

**Both vendors' documentation sites were unreachable from the environment this
was written in** (`help.getharvest.com`, `documentation.bamboohr.com`, and
`nango.dev` were all blocked by an egress proxy). The following come from
secondary sources and need confirming against the live APIs. Each one is
pinned by a test, so correcting it means changing a fixture and watching what
breaks.

| Assumption | Where | How to check |
| --- | --- | --- |
| `POST /v2/users` requires only `first_name`, `last_name`, `email` | `models/harvest.py` | `onboarding --live smoke-harvest` |
| `/v2/users` has no email filter, so lookup pages through all users | `clients/harvest.py` | Harvest Users API docs |
| Harvest rejects duplicate emails with 422 | `sync.py` | `onboarding --live smoke-harvest` — **this is load-bearing**; if duplicates are accepted, the no-database design does not hold |
| BambooHR webhook signs `body + timestamp`, no separator | `webhook_auth.py` | `onboarding diagnose-signature` on a real delivery |
| Webhook payload carries ids under `employees[].id` or `data.employeeId` | `api.py` | Watch for `webhook.no_employee_ids` in the logs |
| BambooHR custom-report endpoint shape | `clients/bamboo.py` | `onboarding backfill` (dry run) |

### The signature question

BambooHR's own PHP sample concatenates the raw body and the timestamp with no
separator; several third-party guides use Stripe-style `{timestamp}.{body}`.
Pick wrong and **every genuine delivery is rejected with a 401**.

All known variants live in `webhook_auth.py` behind `BAMBOO_SIGNATURE_FORMAT`.
On your first real delivery, capture the raw body and headers, then:

```bash
onboarding diagnose-signature \
    --body-file body.bin \
    --timestamp "<X-BambooHR-Timestamp>" \
    --signature "<X-BambooHR-Signature>"
```

It reports which variant matched. If deliveries start 401ing, the logs emit
`webhook.signature_format_mismatch` naming the variant that *would* have
worked.

## Open questions for your supervisor

These are policy, not code. The current behaviour is a placeholder:

1. **Role and billable rate for new users.** Right now we send neither and let
   Harvest apply its account defaults. `HARVEST_DEFAULT_ROLES` sets roles if
   you want them.
2. **Do contractors belong in Harvest?** `BAMBOO_CONTRACTOR_STATUSES` controls
   which BambooHR statuses map to `is_contractor`. Harvest bills per seat, so
   this is a budget question.
3. **Offboarding.** Not implemented. Nobody asks for it in v1 and everybody
   needs it by month three; nothing here blocks adding it.
4. **A test Harvest account.** Harvest has no sandbox, so `smoke-harvest`
   creates a real billable user. Get a trial account, or archive test users
   immediately.

## Design notes

- `mapping.py` is pure — no I/O, no clock of its own. Almost all the business
  logic lives there, which is why the awkward cases (preferred names, missing
  work emails, rehires, `0000-00-00` dates) are cheap to test.
- Errors are a typed hierarchy (`clients/errors.py`), so `sync.py` branches on
  meaning rather than on status codes.
- A timed-out `POST` raises `AmbiguousWriteError` and is **never** retried —
  it may have landed. `sync.py` recovers by re-reading. Blind-retrying writes
  is the classic way to create duplicates.
- `find_user_by_email` includes **inactive** users on purpose: an archived
  rehire still occupies that email, so treating them as absent just earns a
  422.
- The webhook route fails closed. No `BAMBOO_WEBHOOK_PRIVATE_KEY` means 503,
  not an open endpoint that creates billable users on request.
- Secrets are `SecretStr` and never logged; auth errors deliberately omit the
  token.

## Status

- 89 tests pass; `ruff check` and `ruff format` clean.
- **Never run against a live API.** No credentials were available, so every
  test uses recorded HTTP (`respx`) or fakes. `onboarding smoke-harvest` is
  the first thing to run once you have a token.
- **The Docker build is unverified** — no Docker daemon was available, so
  `docker compose up` has not been executed.
