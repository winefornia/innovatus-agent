# winefornia-agent

Invoice agent for Winefornia / Innovatus Wine, built with LangGraph + Claude API.

Cecil or Audrey sends a raw order (Telegram message, forwarded email, PDF) and the agent extracts the details, looks up the customer, calculates the invoice, asks for approval, and creates a draft in Square. The invoice is **never sent to the client** without an explicit confirmation tap.

## Architecture

```
Cecil / Audrey
  ↓  (Telegram bot or Google Chat)
Gateway  (services/gateway.py)  ← channel normalization
  ├── Guardrail  (services/guardrail_service.py)  ← pre/post checks
  ├── Control Layer  (services/control_layer.py)  ← case lifecycle + tracing
  └── Invoice Graph  (agents/invoice_graph.py)    ← deterministic state machine
        ├── Claude Haiku  — extraction, clarification, edit parsing (sidecar only)
        ├── Tool Registry  (services/tool_registry.py)  ← Square/Gmail/Supabase
        ├── Hook Bus  (services/invoice_hooks.py)  ← lifecycle events
        └── Skill Memory  (services/skill_service.py)  ← Mem0 + reference resolver
```

Three design principles:
- **Deterministic brain owns every action.** State machine drives Square, DB writes, and approval gates.
- **LLM is a sidecar.** Claude is called only for extraction, clarifying questions, fuzzy match hints, and edit parsing — never for routing decisions.
- **Learning brain accumulates context.** Mem0 stores skill facts per operator; Supabase invoice history resolves "same as last time" references.

## Invoice graph flow

```
classify_intent
  ↓ invoice_request            ↓ chat / question
extract_invoice_fields      chat_response
  ↓
  [reference resolver: "usual" → Supabase history → Mem0]
  ↓
ask_missing_fields           [INTERRUPT: one focused LLM question if confidence < 0.75
  ↓                                       or required fields missing]
resolve_customer
  ↓ exact match               ↓ fuzzy match (LLM hint pre-populates)
  auto-confirmed              clarify_customer_match  [INTERRUPT]
  ↓
confirm_tier_and_payment     [INTERRUPT: inline keyboard wizard — tier, schedule, methods]
  ↓
resolve_products_and_prices  [deterministic — catalog × tier multiplier]
  ↓
create_invoice_preview
  ↓
approval_gate                [INTERRUPT: approve / reject / edit]
  ↓ approved    ↓ rejected    ↓ edit_requested
  │             respond       interpret_edit   [INTERRUPT: "what to change?"]
  │                             ↓ apply_patch  [deterministic; interrupt if confidence < 0.80]
  │                             ↓ resolve_products_and_prices → create_invoice_preview
  ↓
create_square_invoice_draft  [tool_registry: lookup → create customer → order → draft]
  ↓
confirm_send                 [INTERRUPT: send to client or keep as draft]
  ↓ send         ↓ draft
  publish        respond
  ↓
offer_email_receipt          [INTERRUPT: send receipt email?]
  ↓
respond
```

Max 2 edit rounds per invoice. After that Cecil must resubmit the full order.

## Channels

| Channel | Entry point | Thread ID scheme |
|---|---|---|
| Telegram | `bot.py` (long polling) | `tg_{chat_id}` |
| Google Chat | `app/adapters/google_chat_adapter.py` → `services/gateway.py` | `gc_{space_id}` |
| Email / Mailgun / SendGrid | `POST /webhooks/email` | `email_{uuid}` |
| Gmail labeled "To Invoice" | `POST /webhooks/gmail/poll` | `gmail_{message_id}` |
| HTTP API / Zapier / n8n | `POST /intake` | caller-supplied or auto-generated |
| PDF upload | `POST /intake/pdf` | caller-supplied or auto-generated |

All channels normalize to `NormalizedMessage` before reaching the invoice graph. Adding a new channel requires zero changes to business logic.

## Tasting room agent

A separate case-desk workflow (`agents/case_desk_graph.py`) handles reservation emails end-to-end:

```
Gmail inbox
  ↓  (tastingroom_mail_watcher.py polls every 60s)
tastingroom_mailbox.py  — candidate filtering, dedup, label management
  ↓
case_desk_graph.py  — 9-node LangGraph pipeline:
  store_raw_event → extract_claims → resolve_case → persist_claims
  → build_case_bundle → judge_case → save_case_judgment
  → update_reservation_cache → validate_and_act
  ↓
tastingroom_bot.py  — Telegram notifications with inline approve/reject buttons
  ↓
Cecil taps a button → process_action_decision() → sends email via Gmail
```

The judgment layer (Claude Sonnet) reads the full case bundle and returns a structured `CaseJudgment` with next-best-action, confidence, and interrupt level. Actions that need human approval get sent to Telegram immediately. All reservation state lives in Supabase (`reservations`, `availability_claims`, `case_judgments`, `reservation_action_requests`).

## Repo structure

```
winefornia-agent/
  app/
    config.py               # env vars
    main.py                 # FastAPI: /intake, /intake/pdf, /webhooks/*
    adapters/
      google_chat_adapter.py  # Google Chat event handler
    data/
      customers.json          # customers synced from Square (gitignored — PII)
      product_catalog.json    # wine SKUs with MSRP
      pricing_tiers.json      # tier multipliers
  agents/
    invoice_graph.py          # LangGraph invoice workflow  ← main file
    case_desk_graph.py        # current Gmail tasting room reservation workflow
    tastingroom_graph.py      # legacy/smoke-test tasting room workflow
    supervisor_graph.py       # intent routing types
  services/
    gateway.py              # channel normalization (NormalizedMessage)
    tool_registry.py        # Square/Gmail/Supabase tool wrappers with risk labels
    invoice_hooks.py        # lifecycle event bus (pre/post LLM, pre/post tool, etc.)
    control_layer.py        # case lifecycle, trace logging, failure labeling
    guardrail_service.py    # pre/post input checks (injection, rate limit, amount sanity)
    skill_service.py        # Mem0 skill read/write + "same as last time" resolver
    square_service.py       # Square API: customer, order, invoice draft, publish
    customer_service.py     # customer lookup (email/phone/fuzzy name)
    product_service.py      # product search + deterministic pricing
    gmail_service.py        # Gmail OAuth: intake labels, send receipt
    pdf_service.py          # PDF → text extraction
    tastingroom_service.py  # tasting room reservation logic
    tastingroom_mailbox.py  # Gmail poll for tasting room emails → case_desk_graph
  db/
    schema.sql              # all tables: invoice_logs, reservations, agent_cases,
                            #   trace_events, failure_labels, availability_claims, etc.
    models.py               # dataclasses: InvoiceLog, Case, Reservation, etc.
    repository.py           # Supabase read/write for all tables
    eval_runner.py          # regression eval suite runner
    eval_cases/             # golden eval cases (JSON)
  scripts/
    google_auth.py          # generate Gmail OAuth token.json
    tastingroom_*.py        # tasting room smoke tests and utilities
  bot.py                    # Telegram invoice bot (long polling, primary interface)
  tastingroom_bot.py        # Telegram tasting room bot
  requirements.txt
  fly.toml                  # Fly.io deployment (web + bot + tastingroom processes)
  .env.example
```

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in required vars (see table below)

# Run the Telegram invoice bot
python bot.py

# Or start the API server
uvicorn app.main:app --reload
```

## Environment variables

| Variable | Required | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | console.anthropic.com |
| `SQUARE_PROD_ACCESS_TOKEN` | yes | developer.squareup.com → Production |
| `SQUARE_PROD_LOCATION_ID` | yes | Square dashboard → Locations |
| `SQUARE_ACCESS_TOKEN` | dev only | developer.squareup.com → Sandbox |
| `SQUARE_LOCATION_ID` | dev only | Square dashboard → Sandbox Locations |
| `SQUARE_ENVIRONMENT` | dev only | `sandbox` or `production` (default: sandbox) |
| `TELEGRAM_BOT_TOKEN` | yes (bot) | @BotFather on Telegram |
| `TELEGRAM_TASTINGROOM_BOT_TOKEN` | yes (tasting) | @BotFather on Telegram |
| `TELEGRAM_APPROVAL_CHAT_ID` | yes (tasting) | Telegram chat ID for approval messages |
| `TELEGRAM_TASTINGROOM_AUTHORIZED_CHAT_IDS` | optional | comma-separated Telegram chat IDs; defaults to `TELEGRAM_APPROVAL_CHAT_ID` |
| `TELEGRAM_TASTINGROOM_AUTHORIZED_USER_IDS` | optional | comma-separated Telegram user IDs allowed to use tasting bot |
| `SUPABASE_URL` | yes | Supabase dashboard → Settings → API |
| `SUPABASE_SERVICE_KEY` | yes | Supabase dashboard → Settings → API → service_role |
| `POSTGRES_CONNECTION_STRING` | yes | Supabase dashboard → Settings → Database (port 6543) |
| `GMAIL_TOKEN_JSON_B64` | yes (gmail) | `base64 -i token.json` after running `scripts/google_auth.py` |
| `GOOGLE_SERVICE_ACCOUNT_JSON_B64` | preferred (gmail) | Google Cloud service account JSON with domain-wide delegation enabled |
| `GOOGLE_DELEGATED_USER_EMAIL` | preferred (gmail) | Workspace mailbox to impersonate, e.g. `lisa@winefornia.com` |
| `MEM0_API_KEY` | optional | app.mem0.ai → API Keys |
| `TASTINGROOM_SAFE_MODE` | optional | `true` = send emails to test address only (default: true) |
| `TASTINGROOM_TEST_RECIPIENT` | optional | email to receive test messages in safe mode |
| `GOOGLE_AUTHORIZED_ACCOUNTS` | optional | comma-separated emails allowed to access Google Chat |

## API endpoints

```
POST /intake                      — text intake (email forward, Zapier, n8n)
POST /intake/pdf                  — direct PDF upload
POST /webhooks/email              — Mailgun / SendGrid inbound parse
POST /webhooks/gmail/poll         — poll Gmail "To Invoice" label
POST /webhooks/gmail/tastingroom/poll  — poll Gmail for tasting room emails
POST /webhooks/google-chat        — Google Chat HTTP app events
GET  /invoices/recent             — last N invoice logs from Supabase
GET  /reservations/recent         — last N tasting room reservations
GET  /health
```

## Gmail auth stability

Preferred production auth is Google Workspace domain-wide delegation. This avoids
per-user OAuth refresh tokens for the server process.

1. In Google Cloud, create a service account for this app and enable domain-wide delegation.
2. Copy the service account OAuth client ID from its advanced settings.
3. In Google Admin Console, as a super admin, go to Security → Access and data control → API controls → Manage Domain Wide Delegation.
4. Add the service account client ID with these scopes:

```
https://mail.google.com/
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/contacts.readonly
https://www.googleapis.com/auth/chat.spaces.readonly
https://www.googleapis.com/auth/chat.messages.readonly
```

5. Deploy the service account key and delegated mailbox:

```bash
flyctl secrets set \
  GOOGLE_SERVICE_ACCOUNT_JSON_B64="$(base64 -i service-account.json)" \
  GOOGLE_DELEGATED_USER_EMAIL="lisa@winefornia.com"
```

The app still supports `GMAIL_TOKEN_JSON_B64` and account-specific
`GOOGLE_TOKEN_JSON_B64_*` as fallback auth, but service-account delegation is the
stable production path.

## Pricing tiers

| Tier | Discount | Notes |
|---|---|---|
| FOB/Export | ~50–60% off | export/distributor pricing |
| Wholesale | ~30–35% off | standard wholesale |
| Corporate | ~20–25% off | |
| Club Member | ~15–20% off | |
| Employee | varies | |
| Direct | 0% | retail/MSRP |

Loaded from `app/data/pricing_tiers.json`. Pricing is fully deterministic — no LLM.
Shipping waived for orders over $1,500.

## Observability

Every agent run opens a `Case` in Supabase `agent_cases`. All LLM calls, tool calls, interrupts, and human decisions are written to `trace_events`. Production failures are labeled in `failure_labels` for human review. Eval cases in `db/eval_cases/` guard against regressions.

## Deployment (Fly.io)

```bash
fly deploy
```

Four processes run on Fly.io:

| Process | Command | Purpose |
|---|---|---|
| `web` | `uvicorn app.main:app` | FastAPI server (HTTP endpoints, activity page) |
| `bot` | `python bot.py` | Telegram invoice bot (long polling) |
| `tastingroom_bot` | `python tastingroom_bot.py` | Telegram tasting room bot (approval callbacks) |
| `tastingroom_watcher` | `python scripts/tastingroom_mail_watcher.py` | Gmail poller for tasting room emails |

Secrets are set via `fly secrets set KEY=value`. The `tastingroom_bot` requires `TELEGRAM_TASTINGROOM_BOT_TOKEN` (separate from the invoice bot token). All timestamps in the system are stored as UTC in Supabase and converted to Pacific time for display.
