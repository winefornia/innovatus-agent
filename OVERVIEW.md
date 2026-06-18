# Winefornia Agent — Architecture & Module Overview

A two-part document:
- **Part 1 — Engineering view:** system architecture and a module-by-module map.
- **Part 2 — Project manager view:** a high-level feature list of everything the system does.

---

## What this system is

An AI operations agent for **Winefornia / Innovatus Wine**. It runs two distinct workflows on a shared infrastructure:

1. **Invoice Agent** — Staff (Cecil/Audrey) forward a raw order (Telegram text, email, or PDF); the agent extracts details, looks up the customer, prices the order by tier, asks for approval, and creates a **Square invoice draft**. Nothing is ever sent to a client without an explicit human tap.
2. **Tasting Room Agent** — Reservation emails (Squarespace forms, client replies, facility coordinator threads) are ingested from Gmail, reasoned over by a **goal-driven Vertex ADK agent** (Claude via LiteLLM), and surfaced to staff via **Google Chat** with approve/reject buttons. On approval, the agent replies by email. *(This workflow was migrated off LangGraph onto the Vertex ADK in June 2026 — see `vertex_agent/`. The old `case_desk_graph` / `case_judge` / `safety_guards` LangGraph pipeline has been deleted.)*

Both share one core design philosophy: **a deterministic brain owns every real-world action; the LLM is a sidecar** used only for extraction, clarifying questions, fuzzy matching, and judgment — never for routing or executing actions unsupervised.

---

# Part 1 — Architecture & Modules

## High-level architecture

Two independent pipelines share infrastructure (Supabase, Gmail, Claude, Google Chat)
but have separate brains and entry paths.

**Invoice pipeline (LangGraph):**
```
                         STAFF (Cecil / Audrey / Lisa)
                                     │
        ┌────────────────┬──────────┴──────────┬─────────────────┐
   Telegram bot      Google Chat            Email / Gmail        HTTP API
   (bot.py)          (adapters)             (webhooks, pollers)  (/intake, /intake/pdf)
        └────────────────┴──────────┬──────────┴────────────────────┘
                                     ▼
                      Gateway  (services/gateway.py)
                      → normalizes every channel to NormalizedMessage
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                       ▼
        Guardrails            Control Layer            Supervisor / Router
   (pre/post checks)     (case lifecycle + tracing)   (intent classification)
                                     │
                                     ▼
                       INVOICE GRAPH (LangGraph)
                       deterministic state machine
                       agents/invoice_graph.py
                                     │
                       ┌─────────────┴─────────────┐
                  Tool Registry / Hook Bus    Skill Memory / Interrupts
```

**Tasting-room pipeline (Vertex ADK — goal-driven, not a state machine):**
```
   Gmail (Squarespace forms, client + facility threads)
        │  scripts/tastingroom_mail_watcher.py  (~60s poll)
        ▼
   services/tastingroom_mailbox.py   (candidate filter, dedup, labels)
        ▼
   vertex_agent/intake.py → coordinate_email()
   vertex_agent/agent.py  (Claude Sonnet via LiteLLM; goal-state diff → next action)
        ▼
   Google Chat approval card  (/webhooks/google-chat/tastingroom)
        │  staff taps approve / reject / escalate
        ▼
   process_action_decision() → Gmail send → coordinate_reservation() proposes next step

   Staff can also chat the agent directly: vertex_agent/chat_agent.py
       │
       ▼
   External systems: Square · Gmail · Supabase · Mem0 · Claude (Anthropic) · Vertex ADK
```

### Three design principles
- **Deterministic brain owns every action.** State machines drive Square calls, DB writes, and approval gates.
- **LLM is a sidecar.** Claude is called only for extraction, clarifying questions, fuzzy-match hints, edit parsing, and case judgment.
- **Learning brain accumulates context.** Mem0 stores per-operator skill facts; Supabase invoice history resolves "same as last time" references.

### Runtime processes
**Production (Fly.io — see `fly.toml`):**
| Process | Command | Purpose |
|---|---|---|
| `web` | `uvicorn app.main:app` | FastAPI HTTP endpoints, all Google Chat webhooks (invoice + tasting-room approvals), activity page |
| `tastingroom_watcher` | `python scripts/tastingroom_mail_watcher.py` | Gmail poller → Vertex agent → Google Chat cards |

Tasting-room approvals run over Google Chat, served by the `web` process — there is no
longer a separate tasting-room bot process (the Telegram `tastingroom_bot.py` was removed
in the Vertex migration).

**Optional local/self-hosted (supervisord — see `supervisord.conf`):** adds `invoice-bot`
(`python bot.py`, the Telegram invoice long-poller) alongside `web` and `mail-watcher`,
for environments that want the Telegram interface without a public webhook URL.

---

## Module map

### `agents/` — LangGraph workflows (invoice only)
| Module | Role |
|---|---|
| `invoice_graph.py` | **Main invoice workflow.** Deterministic ~18-node state machine: classify intent → extract fields → resolve customer → confirm tier/payment → price → preview → approval gate → create Square draft → confirm send → offer receipt. Multiple human **interrupts**; Claude Haiku used only as extraction/edit-parsing sidecar. Checkpointed to PostgreSQL (Supabase). Max 2 edit rounds. |
| `supervisor_graph.py` | Stateless intent router. Keyword fast-path for short messages, Claude Haiku for longer ones. Each agent keeps a separate Mem0 namespace. |

> The tasting-room agent no longer lives here. It was migrated to the Vertex ADK — see **`vertex_agent/`** below. The former LangGraph modules (`case_desk_graph.py`, `tastingroom_graph.py`, `router.py`) have been deleted.

### `vertex_agent/` — Tasting-room agent (Google Vertex ADK)
| Module | Role |
|---|---|
| `agent.py` | Root coordinator `LlmAgent` (Claude Sonnet via LiteLLM). Reads a case, derives the goal-state gap, proposes the single next action. HITL preserved — every action routes through a Google Chat approval card; it never sends email directly. |
| `goal_model.py` | `derive_goal_state()` — the "anti-state-machine." Goal sub-conditions derived from existing reservation fields (two case types: production_tour vs standard; party priority Cecil → Customer → Josh). No hardcoded routing. |
| `intake.py` | Email intake without LangGraph: `coordinate_email()` / `coordinate_reservation()`. Reuses the existing `tastingroom_service` / `repository` helpers. The Gmail watcher routes here. |
| `tools.py` | ADK tools wrapping repository/service code: `get_case`, `list_open_cases`, `propose_action`. Ports the old `safety_guards` `<0.6` confidence rule (low-confidence → staff escalation). |
| `chat_agent.py` / `chat_actions.py` | Conversational tasting-room assistant for Google Chat: read-only context + confirm-first write tools (pending-action store re-injected each turn). |
| `invoice_chat_agent.py` / `invoice_chat_actions.py` | Conversational invoicing assistant (separate Chat space): read + confirm-first writes (set prices/availability, create invoices), reusing product/Square services. |

### `services/` — Business logic & infrastructure (~28 modules)

**Invoice domain**
| Module | Role |
|---|---|
| `square_service.py` | Square SDK wrapper: customer lookup/create, order create, invoice draft create + publish; idempotency keys derived from case_id. |
| `customer_service.py` | Customer resolution by name/email/phone/company with fuzzy matching. Supabase primary, `customers.json` fallback. |
| `product_service.py` | Product catalog + **deterministic** tier-multiplier pricing; wine-name alias resolution; shipping waived at $1,500+. |
| `history_service.py` | Customer invoice/order history lookup; lists outstanding unpaid invoices for agent context. |
| `pdf_service.py` | PDF → text via Claude's document API (handles digital + scanned/OCR); one-shot invoice field extraction. |
| `approval_service.py` | Formats drafts into human-readable approval messages; logs decisions to `approval_log.json`. |
| `invoice_hooks.py` | Lifecycle event bus (pre/post LLM, pre/post tool, interrupt, human decision) wired to trace events. |
| `invoice_interrupts.py` | Shared logic to detect which human interrupt a graph state is waiting on. |

**Tasting room domain** (the reasoning brain now lives in `vertex_agent/`; these are the supporting services it reuses)
| Module | Role |
|---|---|
| `tastingroom_service.py` | Core reservation logic: email classification, fact extraction, availability claims, slot matching, LLM draft refinement, `process_action_decision()` (approve/reject/escalate → Gmail send). |
| `tastingroom_mailbox.py` | Gmail ingestion: candidate filtering (Squarespace forms, facility emails), dedup, thread continuity, label management; routes each message to `vertex_agent.intake.coordinate_email()`. |
| `tastingroom_chat_service.py` | Natural-language command helpers for the tasting-room Google Chat assistant (list pending, show case, mark invoice/payment, escalate, revise draft). |

**Channels & messaging**
| Module | Role |
|---|---|
| `gateway.py` | Channel normalization → `NormalizedMessage`; routes through invoice graph; applies guardrails; writes terminal `WorkflowRecord`. |
| `telegram_service.py` | Telegram Bot API wrapper for the invoice bot: messages, inline keyboards, PDF download, webhook registration. |
| `gmail_service.py` | Gmail OAuth / service-account auth: read "To Invoice" label, manage labels, compose + send receipt emails (Claude Haiku). |
| `app/adapters/google_chat_*.py` | Google Chat front-ends: `google_chat_adapter.py` (invoice cards/wizards), `google_chat_invoice_chat.py` (invoice chat assistant), `google_chat_tastingroom.py` (tasting-room approval cards + chat). Authorization is per-space allowlist (fail-closed). |

**AI, memory & learning**
| Module | Role |
|---|---|
| `mem0_service.py` | Mem0 persistent memory; per-user facts; best-effort (never crashes the agent). |
| `skill_service.py` | Persistent skill memory + reference resolver ("same as last time" → Supabase history, Mem0 fallback). |

**Control, safety & observability**
| Module | Role |
|---|---|
| `control_layer.py` | Case lifecycle supervisor: opens a Case, traces input/intent/output/tool calls/interrupts/decisions/failures, synthesizes skills, creates eval cases from failures. |
| `guardrail_service.py` | Deterministic (never LLM) pre/post checks: input length, prompt injection, rate limit, amount sanity, tier/schedule validation, error keys, credential-leak detection. |
| `activity_service.py` | Formats activity for operators (Telegram history + HTML activity page) in Pacific time. |
| `tool_registry.py` | Business-action router with validation, **risk labels**, hooks, and error normalization for Square/Gmail/Supabase/customer/pricing tools (plus a separate tasting-room registry). |

### `app/` — HTTP layer & static data
| File | Role |
|---|---|
| `main.py` | FastAPI server: `/intake`, `/intake/pdf`, `/webhooks/email`, `/webhooks/gmail/poll`, `/webhooks/gmail/tastingroom/poll`, `/webhooks/google-chat`, `/invoices/recent`, `/reservations/recent`, `/activity`, `/health`. |
| `config.py` | Env var loader (API keys, tokens, Supabase, Mem0, safe-mode/prod flags, authorized accounts). |
| `schemas.py` | Pydantic models: `LineItem`, `InvoiceDraft`. |
| `adapters/google_chat_adapter.py` | Google Chat invoice front-end (cards, wizards, stale-click guards). Tasting-room and invoice-chat adapters live alongside it (`google_chat_tastingroom.py`, `google_chat_invoice_chat.py`). |
| `static/index.html` | Activity dashboard stub (wine-red theme). |
| `data/*.json` | `product_catalog.json` (SKUs + MSRP), `customers.json` (PII, gitignored), `pricing_tiers.json` (tier multipliers), `approval_log.json` (audit). |

### `db/` — Persistence & evaluation
| File | Role |
|---|---|
| `schema.sql` | All ~20 tables (see below). |
| `models.py` | Dataclasses mirroring tables: `InvoiceLog`, `Case`, `TraceEvent`, `FailureLabel`, `GuardrailDecision`, `Reservation`, `AvailabilityClaim`, `ReservationEvent`, `ReservationActionRequest`, `CaseJudgmentRecord`, `WorkflowRecord`, `EvalCase`, etc. |
| `repository.py` | Supabase data-access layer for every table; best-effort writes. |
| `eval_runner.py` | Deterministic regression eval harness (intent/agent/output/terminal-status grading; no LLM judge). |
| `eval_cases/*.json` | Golden + edge + regression + adversarial eval scenarios. |

**Key tables:** `customers`, `products`, `pricing_tiers`, `square_orders`, `square_invoices`, `invoice_logs`, `sync_state`, `agent_cases`, `trace_events`, `failure_labels`, `workflow_records`, `reservations`, `availability_claims`, `reservation_events`, `reservation_action_requests`, `raw_email_events`, `case_judgments`, `validation_results`, `execution_results`, `unresolved_reservation_events`.

### Entry points & operations
| File | Role |
|---|---|
| `bot.py` | Telegram invoice bot (long polling). Invoice staff also use Google Chat (served by `web`). |
| `cli.py` | Local CLI to drive the invoice graph through interrupts without Telegram. |
| `scripts/tastingroom_mail_watcher.py` | Always-on Gmail reservation watcher → Vertex agent → Google Chat cards. |
| `scripts/tastingroom_workflow_audit.py` | Tasting-room workflow audit/diagnostics. |
| `scripts/backfill_tastingroom_gmail_labels.py` | Backfill Gmail labels on historical tasting-room threads. |
| `scripts/reap_stale_cases.py` | Close out stale/abandoned cases. |
| `scripts/sync.py` | Weekly cursor-based Square → Supabase sync. |
| `scripts/migrate.py` | One-time historical data migration into Supabase. |
| `scripts/update_pricing_from_sheet.py` | Sync pricing from the source sheet into Supabase/JSON. |
| `scripts/google_auth.py` | Generate Gmail OAuth token. |
| `scripts/gmail_auth_check.py` | Validate Gmail auth/mailbox without sending. |

### Tech stack
LangGraph (invoice) · Google Vertex ADK + LiteLLM (tasting room) · Claude API (Haiku for extraction/composition, Sonnet for coordination/judgment) · FastAPI · Supabase/PostgreSQL · Square SDK · Gmail API · Telegram Bot API · Google Chat · Mem0 · Fly.io + supervisord · Docker.

---

# Part 2 — Project Manager Feature List

## A. Order & Invoice Automation (Invoice Agent)
- **Multi-channel order intake** — accept orders from Telegram, Google Chat, forwarded email, Gmail-labeled threads, HTTP API (Zapier/n8n), and direct PDF upload. Adding a channel requires no business-logic change.
- **AI order extraction** — pull customer, line items, quantities, and company from free-form text or PDFs (digital and scanned/OCR).
- **Smart customer matching** — exact match auto-confirms; fuzzy matches surface a confirmation prompt to staff.
- **"Same as last time" memory** — resolves vague references using invoice history and per-operator memory.
- **Deterministic tier pricing** — six pricing tiers (FOB/Export, Wholesale, Corporate, Club Member, Employee, Direct) with automatic discount math; free shipping over $1,500. No LLM in the money path.
- **Guided confirmation wizard** — inline-keyboard flow for tier, payment schedule, and payment methods.
- **Human approval gate** — staff approve / reject / edit before anything is created; up to 2 edit rounds with AI-parsed edit instructions.
- **Square invoice drafting** — creates customer, order, and invoice draft in Square via idempotent calls.
- **Send safety** — invoices are **never** sent to clients without an explicit second confirmation tap; default is keep-as-draft.
- **Receipt emails** — optional AI-composed receipt email sent via Gmail after sending.
- **Crash-safe sessions** — workflows checkpoint to PostgreSQL, so a bot restart mid-approval resumes exactly where it left off.

## B. Tasting Room Reservation Coordination (Tasting Room Agent)
- **Automatic email ingestion** — polls Gmail every ~60s for Squarespace form submissions, client replies, and facility-coordinator threads; dedups and tracks thread continuity.
- **Reservation state tracking** — maintains each reservation's lifecycle (request → availability → counter-offers → confirmation → invoice → final confirmation) in the database.
- **Goal-driven coordination (Vertex ADK)** — a Claude agent reads the full case, derives the gap to the goal state, and proposes the single next action with a confidence score and a required approval level (low-confidence actions are downgraded to staff review).
- **Availability claim reconciliation** — tracks who claimed what slot (client, facility coordinator, internal staff) and resolves conflicts.
- **Google Chat approval workflow** — staff get notified with approve/reject/escalate buttons; approving sends the email reply automatically, and the agent immediately proposes the next step.
- **Natural-language staff commands** — staff can list pending cases, view a case, mark invoice/payment status, escalate, or revise a draft in plain language.
- **Safe mode** — can route all outbound email to a test address until enabled for live sending.

## C. Safety, Guardrails & Trust
- **Deterministic guardrails** — prompt-injection detection, rate limiting, invoice-amount sanity checks, and credential-leak prevention — all rule-based, never LLM-decided.
- **Risk-labeled actions** — every external action carries a risk label and passes pre/post checks.
- **Access control** — Telegram bots restricted to authorized chats/users; Google Chat restricted to an allowed email list.
- **Reconciliation alerts** — if Square succeeds but the database write fails, the case is flagged for manual review rather than silently lost.

## D. Observability
- **Full audit trail** — every run opens a "case"; every LLM call, tool call, interrupt, and human decision is logged to the database.
- **Failure labeling** — production failures are categorized by type, severity, and responsible layer for human review.
- **Regression eval suite** — golden, edge-case, regression, and adversarial scenarios guard against regressions; production failures can become new eval cases.
- **Activity dashboard** — operators review recent invoices and reservations via Telegram history and a web page, in Pacific time.

## E. Learning & Memory
- **Per-operator skill memory** — accumulates facts about how each operator works.
- **Per-agent memory isolation** — supervisor and each agent keep separate memory namespaces so context never crosses.

## F. Data, Integrations & Operations
- **System integrations** — Square (invoicing), Gmail (intake + sending), Supabase/PostgreSQL (system of record), Mem0 (memory), Telegram & Google Chat (staff interfaces), Claude (AI).
- **Data sync** — scheduled Square → Supabase sync (customers, orders, invoices) with cursor-based incremental updates.
- **Stable Gmail auth** — Google Workspace domain-wide delegation for server-side mailbox access (no fragile per-user refresh tokens).
- **Deployment** — Fly.io with four supervised processes (web, invoice bot, tasting room bot, mail watcher); Docker-packaged; secrets via Fly.
- **Replay & shadow tooling** — historical cases can be replayed through the agent in dry-run/shadow mode for testing without side effects.
