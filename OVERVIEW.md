# Winefornia Agent — Architecture & Module Overview

A two-part document:
- **Part 1 — Engineering view:** system architecture and a module-by-module map.
- **Part 2 — Project manager view:** a high-level feature list of everything the system does.

---

## What this system is

An AI operations agent for **Winefornia / Innovatus Wine**. It runs two distinct workflows on a shared infrastructure:

1. **Invoice Agent** — Staff (Cecil/Audrey) forward a raw order (Google Chat message, email, or PDF); the agent extracts details, looks up the customer, prices the order by tier, asks for approval, and creates a **Square invoice draft**. Nothing is ever sent to a client without an explicit human tap.
2. **Tasting Room Agent** — Reservation emails (Squarespace forms, client replies, facility coordinator threads) are ingested from Gmail, reasoned over by an LLM "judgment" layer, and surfaced to staff via Google Chat with approve/reject buttons. On approval, the agent replies by email.

Both share one core design philosophy: **a deterministic brain owns every real-world action; the LLM is a sidecar** used only for extraction, clarifying questions, fuzzy matching, and judgment — never for routing or executing actions unsupervised.

---

# Part 1 — Architecture & Modules

## High-level architecture

```
                         STAFF (Cecil / Audrey / Lisa)
                                     │
        ┌────────────────┬──────────┴──────────┬─────────────────┐
        Google Chat (invoice wizard + assistants)    Gmail (tasting room)
        (adapters)                                   (poller)
        └────────────────────────────┬───────────────────────────────┘
                                     ▼
                      Gateway  (services/gateway.py)
                      → normalizes every channel to NormalizedMessage
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                       ▼
        Guardrails            Control Layer            Supervisor / Router
   (pre/post checks)     (case lifecycle + tracing)   (intent classification)
                                     │
              ┌──────────────────────┴───────────────────────┐
              ▼                                               ▼
   INVOICE GRAPH (LangGraph)                    TASTING-ROOM COORDINATOR
   deterministic state machine                 Gmail watcher + goal model
   agents/invoice_graph.py                      vertex_agent/intake.py
              │                                               │
   ┌──────────┴──────────┐                       ┌────────────┴───────────┐
   Tool Registry      Hook Bus              Gmail / Chat Approval    Supabase State
   Skill Memory       Interrupts            (Claude-powered)
              │                                               │
              └──────────────────────┬────────────────────────┘
                                     ▼
       External systems: Square · Gmail · Supabase · Mem0 · Claude (Anthropic)
```

### Three design principles
- **Deterministic brain owns every action.** State machines drive Square calls, DB writes, and approval gates.
- **LLM is a sidecar.** Claude is called only for extraction, clarifying questions, fuzzy-match hints, edit parsing, and case judgment.
- **Learning brain accumulates context.** Mem0 stores per-operator skill facts; Supabase invoice history resolves "same as last time" references.

### Runtime processes (Fly.io)
| Process | Command | Purpose |
|---|---|---|
| `web` | `uvicorn app.main:app` | FastAPI HTTP endpoints + activity page |
| `tastingroom_watcher` | `python scripts/tastingroom_mail_watcher.py` | Gmail poller for reservation emails |

---

## Module map

### `agents/` — LangGraph workflows
| Module | Role |
|---|---|
| `invoice_graph.py` | **Main invoice workflow.** Deterministic ~18-node state machine: classify intent → extract fields → resolve customer → confirm tier/payment → price → preview → approval gate → create Square draft → confirm send → offer receipt. Multiple human **interrupts**; Claude Haiku used only as extraction/edit-parsing sidecar. Checkpointed to PostgreSQL (Supabase). Max 2 edit rounds. |
| `supervisor_graph.py` | Stateless intent router. Keyword fast-path for short messages, Claude Haiku for longer ones; routes to invoice vs tasting room agent. Each agent keeps a separate Mem0 namespace. |
| `router.py` | Legacy router, superseded by `supervisor_graph.py`; kept for backward compatibility. |

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

**Tasting room domain**
| Module | Role |
|---|---|
| `tastingroom_service.py` | Core reservation logic: email classification, fact extraction, reservation state persistence, availability claims, slot matching, LLM draft refinement. |
| `tastingroom_mailbox.py` | Gmail ingestion: candidate filtering (Squarespace forms, facility emails), dedup, thread continuity, label management, routes to `vertex_agent/intake.py`. |
| `tastingroom_chat_service.py` | Natural-language command helpers for tasting-room staff workflows (list pending, show case, mark invoice/payment, escalate, revise draft). |
| `vertex_agent/intake.py` | Current tasting-room coordinator entry point: stores raw events, extracts facts, updates reservation state, derives gaps, and creates approval-gated action requests. |
| `vertex_agent/goal_model.py` | Derived goal-state model for reservation readiness and next-step selection. |

**Channels & messaging**
| Module | Role |
|---|---|
| `gateway.py` | Channel normalization → `NormalizedMessage`; routes through invoice graph; applies guardrails; writes terminal `WorkflowRecord`. |
| `gmail_service.py` | Gmail OAuth / service-account auth: mailbox reading (tasting room), label management, compose + send receipt emails (Claude Haiku). |

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
| `activity_service.py` | Formats activity for operators (HTML activity page, GET /activity) in Pacific time. |
| `tool_registry.py` | Business-action router with validation, **risk labels**, hooks, and error normalization for Square/Gmail/Supabase/customer/pricing tools (plus a separate tasting-room registry). |

### `app/` — HTTP layer & static data
| File | Role |
|---|---|
| `main.py` | FastAPI server: `/webhooks/google-chat` (+ `/invoice-chat`, `/tastingroom`), `/webhooks/gmail/tastingroom/poll`, `/invoices/recent`, `/reservations/recent`, `/activity`, `/health`. |
| `config.py` | Env var loader (API keys, tokens, Supabase, Mem0, safe-mode/prod flags, authorized accounts). |
| `schemas.py` | Pydantic models: `LineItem`, `InvoiceDraft`. |
| `adapters/google_chat_adapter.py` | Google Chat front-end for the invoice wizard (cards, wizards, stale-click guards). |
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
| `cli.py` | Local CLI to drive the invoice graph through interrupts without Google Chat. |
| `scripts/sync.py` | Weekly cursor-based Square → Supabase sync. |
| `scripts/migrate.py` | One-time historical data migration into Supabase. |
| `scripts/google_auth.py` | Generate Gmail OAuth token. |
| `scripts/gmail_auth_check.py` | Validate Gmail auth/mailbox without sending. |
| `scripts/tastingroom_mail_watcher.py` | Always-on Gmail reservation watcher. |
| `scripts/replay_case.py`, `replay_mira_case.py` | Replay historical cases through the graph (shadow/dry-run). |
| `scripts/build_tastingroom_memory.py` | Build case memory from historical Gmail forms. |
| `scripts/eval_tasting_room.py`, `tastingroom_e2e_smoke.py`, `tastingroom_workflow_audit.py` | Tasting room evals, smoke tests, audits. |

### Tech stack
LangGraph · Claude API (Haiku for extraction/composition, Sonnet for judgment/patching) · FastAPI · Supabase/PostgreSQL · Square SDK · Gmail API · Mem0 · Fly.io + supervisord · Docker.

---

# Part 2 — Project Manager Feature List

## A. Order & Invoice Automation (Invoice Agent)
- **Order intake via Google Chat** — typed orders, pasted emails, or PDF attachments, all through the Google Chat wizard/assistant. Gmail is used only for tasting-room intake and for sending invoice receipt emails to customers.
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
- **AI case judgment** — an LLM reads the full case history and proposes the next best action with a confidence score and a required approval level.
- **Availability claim reconciliation** — tracks who claimed what slot (client, facility coordinator, internal staff) and resolves conflicts.
- **Google Chat approval workflow** — staff get notified with approve/reject/escalate buttons; approving sends the email reply automatically.
- **Natural-language staff commands** — staff can list pending cases, view a case, mark invoice/payment status, escalate, or revise a draft in plain language.
- **Safe mode** — can route all outbound email to a test address until enabled for live sending.

## C. Safety, Guardrails & Trust
- **Deterministic guardrails** — prompt-injection detection, rate limiting, invoice-amount sanity checks, and credential-leak prevention — all rule-based, never LLM-decided.
- **Risk-labeled actions** — every external action carries a risk label and passes pre/post checks.
- **Access control** — Google Chat restricted to an allowed email list.
- **Reconciliation alerts** — if Square succeeds but the database write fails, the case is flagged for manual review rather than silently lost.

## D. Observability
- **Full audit trail** — every run opens a "case"; every LLM call, tool call, interrupt, and human decision is logged to the database.
- **Failure labeling** — production failures are categorized by type, severity, and responsible layer for human review.
- **Regression eval suite** — golden, edge-case, regression, and adversarial scenarios guard against regressions; production failures can become new eval cases.
- **Activity dashboard** — operators review recent invoices and reservations via the /activity web page, in Pacific time.

## E. Learning & Memory
- **Per-operator skill memory** — accumulates facts about how each operator works.
- **Per-agent memory isolation** — supervisor and each agent keep separate memory namespaces so context never crosses.

## F. Data, Integrations & Operations
- **System integrations** — Square (invoicing), Gmail (intake + sending), Supabase/PostgreSQL (system of record), Mem0 (memory), Google Chat (staff interface), Claude (AI).
- **Data sync** — scheduled Square → Supabase sync (customers, orders, invoices) with cursor-based incremental updates.
- **Stable Gmail auth** — Google Workspace domain-wide delegation for server-side mailbox access (no fragile per-user refresh tokens).
- **Deployment** — Fly.io with four supervised processes (web, invoice bot, tasting room bot, mail watcher); Docker-packaged; secrets via Fly.
- **Replay & shadow tooling** — historical cases can be replayed through the agent in dry-run/shadow mode for testing without side effects.
