# CLAUDE.md — Agent Guide for winefornia-agent

This file is for Claude (and any AI agent) working on this repo. It explains what
the system is, how the code is laid out, the rules that must never be broken, and
how a change gets from an edit to production. A non-engineer operator will ask
you for fixes and features through Claude Code — explain things in plain language,
make small safe changes, and always follow the Hard Rules below.

## What this system is

One FastAPI server (Fly.io app **`winefornia-agent`**) runs **two completely
separate assistants**, both living in Google Chat, for the Winefornia / Innovatus
winery:

| | Invoice pipeline | Tasting-room pipeline |
|---|---|---|
| Purpose | Turn an order into a Square invoice, track it to paid | Turn a Squarespace booking form into a confirmed visit |
| Chat app | **Winefornia_Invoice** | **Winefornia Tasting Room** |
| Case opens when | Staff types/pastes an order in Chat | A client submits the Squarespace form (arrives by email) |
| Case closes when | Square's own email confirms created/paid | Final confirmation sent (or cancelled) |

The two pipelines **never share cases**. Square notification emails belong to the
invoice pipeline only; the Squarespace form belongs to the tasting room only.
See `docs/how-it-works.md` for the full plain-language guide.

## Production topology

- **Fly.io** app `winefornia-agent` (region iad, 1GB VM — 512MB gets OOM-killed).
  Two processes from `fly.toml`: `web` (uvicorn `app.main:app` on :8080) and
  `tastingroom_watcher` (`scripts/tastingroom_mail_watcher.py`, polls Gmail ~60s).
- **Supabase** project `zlbixpklvejcuxifqzjk` — all tables (reservations,
  invoices, cases, traces). Postgres must connect via **port 6543 (pgBouncer),
  not 5432**.
- **Watched mailbox**: the winery Google Workspace account (mail to
  contact@innovatuswine.com lands under the `INNOVATUS` label). Access is via
  service account + domain-wide delegation. Any claude.ai Gmail connector is a
  *different* account — winery-mailbox operations must run on the Fly machine
  with the app's own credentials.
- **Square** is production money. **Anthropic API** (Claude Haiku) is the LLM
  sidecar. **Mem0** stores operator skill memory.
- Secrets live in **Fly secrets** (`flyctl secrets`), never in the repo.
  `.env.example` lists only a subset; `app/config.py` lists most (a few are read
  directly in services — see the file map note on config.py).

## Hard Rules (break these and real bookings/money are lost)

1. **Schema changes are NOT auto-applied.** `db/schema.sql` is applied to
   Supabase **manually** — deploy does not run migrations (`scripts/migrate.py`
   is data-only). If a change adds/renames a table or column, the matching
   `ALTER`/`CREATE` must be applied to Supabase **before or with** the deploy.
   Code/DB drift caused a lost booking in July 2026 (PGRST204 on
   `calendar_event_id`). Always call this out explicitly when a change touches
   `db/schema.sql` or `db/models.py`.
2. **Never mix the pipelines.** Square notification emails are consumed only by
   `services/invoice_mail_validator.py`; the tasting-room intake
   (`services/tastingroom_mailbox.py`) deliberately rejects them. Only an
   identity-bearing Squarespace booking form may open a tasting case.
3. **Money and outbound email are confirm-first.** Nothing creates/sends a
   Square invoice or emails a client without an explicit human approval in
   Chat. All such actions route through `services/tool_registry.py` and the
   approval/staged-action mechanisms. Never add a code path that sends or
   charges directly.
4. **The LLM is a sidecar, never the router.** Deterministic code (state
   machine, goal model, guardrails) decides every action; Claude is used only
   for extraction, clarifying questions, fuzzy-match hints, and edit parsing.
   Keep it that way.
5. **No secrets in the repo.** Tokens/keys go in Fly secrets or GitHub Actions
   secrets. If you see one pasted in code or chat, flag it.
6. **Safety toggles**: `TASTINGROOM_SAFE_MODE` defaults to true (outbound mail
   goes only to the test recipient); `PRODUCTION_MODE` must be true in prod
   (gates unsafe fallbacks like in-memory checkpointing); `GCHAT_VERIFY`
   defaults to `observe` — `enforce` is the secure setting. The
   `GOOGLE_CHAT_*_AUTHORIZED_EMAILS` lists are **fail-closed**: a blank or
   malformed value denies everyone.

## How a change reaches production

```
edit on a branch → open PR → GitHub Actions runs pytest (hermetic, all mocked)
  → merge to main → same workflow auto-deploys: flyctl deploy --remote-only
  → verify: GET https://winefornia-agent.fly.dev/health  (also checks watcher heartbeat)
```

- CI: `.github/workflows/ci.yml`. Deploy only runs on green tests and only on
  `main`, and requires the `FLY_API_TOKEN` repo secret.
- Tests: `pytest -q` — no real network or secrets needed
  (`tests/conftest.py` mocks Square/Supabase/Anthropic and sets dummy env).
  ~31 test files: `tests/unit/` for services, `tests/integration/` for full flows.
- Local single-command smoke test of the invoice graph: `python cli.py --message "..."`.
- **Checklist for any PR**: tests pass → does it touch `db/schema.sql`? (Hard
  Rule 1) → does it touch money/email paths? (Hard Rule 3) → then merge.

## File map

```
app/                    FastAPI layer
  main.py               All webhooks & routes (see list below), startup heartbeat monitor
  config.py             Main env-var list (with comments). Not exhaustive: GCHAT_VERIFY
                        (app/main.py) and the Gmail service-account vars
                        (GOOGLE_SERVICE_ACCOUNT_JSON_B64, GOOGLE_DELEGATED_USER_EMAIL,
                        GOOGLE_TOKEN_JSON_B64_*) are read directly in services/
  schemas.py            Pydantic invoice shapes (LineItem, InvoiceDraft)
  adapters/             Google Chat UI adapters (wizard cards, chat, tasting-room cards)
  data/                 JSON fallbacks: product_catalog.json, pricing_tiers.json, approval_log.json

agents/                 Invoice pipeline brain (LangGraph)
  invoice_graph.py      Deterministic state machine: classify → extract → resolve
                        customer → tier/payment → price → preview → approval gate
                        → Square draft → confirm send. Interrupts become Chat cards.
  supervisor_graph.py   Intent router (currently routes everything to invoice_agent)

vertex_agent/           Tasting-room + chat brains (Google ADK, Claude via LiteLLM)
  agent.py, goal_model.py, tools.py, intake.py    Goal-driven tasting-room coordinator
  chat_agent.py, chat_actions.py                  Conversational tasting-room assistant
  invoice_chat_agent.py, invoice_chat_actions.py,
  invoice_chat_memory.py                          Free-form invoicing assistant (the
                                                  default invoice front-end today)

services/               All integrations & cross-cutting logic
  square_service.py         💰 every Square API call (idempotent keys)
  tool_registry.py          💰✉ single validated gate for all business actions
  gmail_service.py          ✉ Gmail read + client emails
  calendar_service.py       ✉ tasting calendar invites
  tastingroom_mailbox.py    ✉ intake: Squarespace mail → case (rejects Square mail)
  tastingroom_service.py    ✉ reservation coordination + approval decisions
  tastingroom_chat_service.py  staff NL commands for reservations
  invoice_mail_validator.py 💰 closes invoice cases from Square's own emails
  customer_service.py / product_service.py   lookups & pricing
  skill_service.py          "same as usual" memory (Mem0-backed)
  control_layer.py          case lifecycle, tracing, stale-case reaping
  guardrail_service.py      deterministic pre/post checks (never LLM)
  invoice_hooks.py / invoice_interrupts.py / gateway.py   plumbing
  heartbeat_monitor.py      alerts Chat if the mail watcher goes silent
  activity_service.py       /activity operator page
  drive_service.py / pdf_service.py    attached-PDF handling
  approval_service.py       approval formatting + audit log

scripts/                Long-running: tastingroom_mail_watcher.py (Fly process).
                        One-offs: migrate.py (data), sync.py (Square→Supabase cron),
                        reap_stale_cases.py, google_auth.py, gmail_auth_check.py,
                        backfill_tastingroom_gmail_labels.py,
                        update_pricing_from_sheet.py, verify/audit scripts.

db/                     schema.sql (manually applied! see Hard Rule 1), models.py
                        (dataclasses), repository.py (all Supabase reads/writes),
                        eval_runner.py + eval_cases/ (deterministic evals)

tests/                  unit/ + integration/, hermetic via conftest.py mocks
docs/                   how-it-works.md (user guide), invoice-chat-agent.md,
                        tasting-room-system-report.md, tasting-room-architecture-and-goals.md
OVERVIEW.md             engineering module map; README.md quickstart
```

Key routes in `app/main.py`:
`POST /webhooks/google-chat` (invoice chat, default front-end) ·
`/webhooks/google-chat/graph` (legacy wizard) ·
`/webhooks/google-chat/tastingroom` (approval cards) ·
`POST /webhooks/gmail/tastingroom/poll` and `/webhooks/gmail/invoice-validation/poll`
(on-demand mail polls) · `GET /invoices/recent`, `/reservations/recent` ·
`GET /activity?key=…` (operator page) · `GET /health`.

## When something breaks — where to look

| Symptom | Look at |
|---|---|
| "A booking never showed up in Chat" | Supabase `raw_email_events` (every fetched mail) → `unresolved_reservation_events` (quarantined/failed intake) → `reservations` |
| "Is the watcher alive?" | `GET /health` (503 = stale heartbeat) or Supabase `system_heartbeat` row `tastingroom_watcher` |
| Force an immediate mail check | `POST https://winefornia-agent.fly.dev/webhooks/gmail/tastingroom/poll` |
| "Invoice case never closed" | `POST …/webhooks/gmail/invoice-validation/poll`; Gmail label `Invoice Validation/Unmatched` |
| What happened on a case, step by step | Supabase `trace_events` for the case in `agent_cases` |
| Recent activity overview | `GET /activity?key=<ACTIVITY_API_KEY>` |
| Server logs / restart | `flyctl logs -a winefornia-agent` · `flyctl machine restart` |

## Gotchas a new maintainer should know

- The repo folder is `innovatus-agent` but everything deploys as
  `winefornia-agent` — same business, two names.
- Two invoice front-ends coexist: the conversational agent (default) and the
  legacy card wizard (`/webhooks/google-chat/graph`). They are separate code paths.
- The invoice and tasting-room Chat apps are **separate GCP projects** with
  separate service accounts, audiences, and authorized-email lists.
- Parts of `vertex_agent/` (the ADK tasting-room coordinator) run in parallel to
  the older LangGraph path — check what production actually imports before
  editing either.
- `Dockerfile` has no CMD; run commands come from `fly.toml` (or
  `supervisord.conf` for a single-box run).
- Default IDs (Supabase host, GCP project numbers, Chat space, webhook URLs,
  authorized emails) are hardcoded as fallbacks in `app/config.py` and
  overridden by env — change env, not the fallbacks.
- `scripts/sync.py` (weekly Square→Supabase sync) has **no scheduler in this
  repo or on Fly** — it runs from outside the deployed system. The invoice half
  of it is broken as of July 2026 (`square_invoices` is empty while `sync_state`
  reports success). See `docs/ownership-and-migration.md` §4.
- Account ownership across Fly/Supabase/GitHub/GCP/Square, and the migration
  plan to winery control: `docs/ownership-and-migration.md`.

## How to work with the operator (non-engineer)

- Explain diagnoses and fixes in plain language; name the pipeline and the file.
- Prefer the smallest change that fixes the problem; keep the two pipelines apart.
- Before merging anything that touches `db/schema.sql`, `services/square_service.py`,
  `services/gmail_service.py`, or `services/tool_registry.py`, state plainly what
  could go wrong and what to check after deploy (`/health`, a test booking, etc.).
- After any deploy, verify `GET /health` returns 200 and mention it.
