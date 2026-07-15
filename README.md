# winefornia-agent

Invoice agent for Winefornia / Innovatus Wine, built with LangGraph + Claude API.

Cecil or Audrey sends a raw order (Google Chat message, forwarded email, PDF) and the agent extracts the details, looks up the customer, calculates the invoice, asks for approval, and creates a draft in Square. The invoice is **never sent to the client** without an explicit confirmation tap.

## Architecture

```
Cecil / Audrey
  ↓  (Google Chat)
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
| Google Chat (invoice wizard) | `app/adapters/google_chat_adapter.py` → `services/gateway.py` | `gc_{space_id}` |
| Google Chat (invoicing assistant) | `app/adapters/google_chat_invoice_chat.py` | per Chat thread |

All channels normalize to `NormalizedMessage` before reaching the invoice graph. Adding a new channel requires zero changes to business logic.

Replies land in the thread the message came from — including async "working on it" results, which are posted with `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` so flat ("conversation view") spaces gracefully get a normal message instead of an error.

## Tasting room agent

A separate Gmail watcher and reservation coordinator handle tasting-room emails end-to-end:

```
Gmail inbox
  ↓  (tastingroom_mail_watcher.py polls every 60s)
tastingroom_mailbox.py  — candidate filtering, dedup, label management
  ↓
vertex_agent/intake.py  — stores the raw event, extracts facts, resolves/updates the case
  ↓
vertex_agent/goal_model.py + tastingroom_service.py  — derive gaps and propose the next action
  ↓
Google Chat approval card  — staff approve/reject/revise the action
  ↓
Staff taps a button → process_action_decision() → sends email via Gmail
```

Claude-powered coordination proposes the next best reservation action, but outbound emails still pass through the existing human approval card. All reservation state lives in Supabase (`reservations`, `availability_claims`, `reservation_events`, `reservation_action_requests`, `raw_email_events`).

## Repo structure

```
winefornia-agent/
  app/
    config.py               # env vars
    main.py                 # FastAPI: /webhooks/google-chat*, /webhooks/gmail/*/poll, /mcp/invoice
    mcp_invoice.py          # read-only MCP operator console for Claude (fail-closed)
    adapters/
      google_chat_adapter.py       # invoice wizard cards (legacy front-end)
      google_chat_invoice_chat.py  # conversational invoicing assistant (default front-end)
      google_chat_tastingroom.py   # tasting-room approval cards
    data/
      customers.json          # customers synced from Square (gitignored — PII)
      product_catalog.json    # wine SKUs with MSRP
      pricing_tiers.json      # tier multipliers
  agents/
    invoice_graph.py          # LangGraph invoice workflow  ← main file
    supervisor_graph.py       # intent routing types
  vertex_agent/
    intake.py                 # tasting room Gmail intake and coordination entry point
    goal_model.py             # derived reservation goal state
    agent.py                  # optional ADK agent runtime
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
    tastingroom_mailbox.py  # Gmail poll for tasting room emails → vertex_agent/intake.py
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
  requirements.txt
  fly.toml                  # Fly.io deployment (web + tastingroom watcher)
  .env.example
```

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in required vars (see table below)

# Start the API server (serves the Google Chat webhooks)
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
POST /webhooks/google-chat             — invoicing assistant (default front-end)
POST /webhooks/google-chat/graph       — legacy card wizard
POST /webhooks/google-chat/tastingroom — tasting-room approval cards + staff chat
POST /webhooks/gmail/tastingroom/poll  — on-demand poll for tasting-room emails
POST /webhooks/gmail/invoice-validation/poll — on-demand poll for Square confirmation emails
POST /mcp/invoice[/<secret>]           — read-only MCP operator console (Claude)
GET  /invoices/recent                  — last N invoice logs from Supabase
GET  /reservations/recent              — last N tasting room reservations
GET  /activity?key=…                   — operator activity page
GET  /health                           — 200 healthy, 503 = watcher heartbeat stale
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

Two processes run on Fly.io:

| Process | Command | Purpose |
|---|---|---|
| `web` | `uvicorn app.main:app` | FastAPI server (HTTP endpoints, activity page) |
| `tastingroom_watcher` | `python scripts/tastingroom_mail_watcher.py` | Gmail poller for tasting room emails |

Secrets are set via `fly secrets set KEY=value`. Tasting-room approvals use the Google Chat tasting-room app configured by `GOOGLE_CHAT_TR_SPACE`. All timestamps in the system are stored as UTC in Supabase and converted to Pacific time for display.
