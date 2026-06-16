# Tasting Room — Architecture & Goals (saved before the goal-oriented rebuild)

This captures the **intent, goals, and the legacy LangGraph design** so the rebuild
preserves behavior. The legacy pipeline is being replaced by a goal-oriented agent
(`vertex_agent/`); this document is the source of truth for *what it must still do*.

---

## 1. Mission / goal

Coordinate a wine-tasting reservation end-to-end, entirely over **Gmail** (the
coordination channel with the facility coordinator "Josh", internal staff, and the
client) with **Google Chat** as the human-approval surface. No outbound email is
ever sent without a human approving a card.

**The coordination goal for one reservation:** schedule a visit by coordinating up
to three parties — **Cecil/Winefornia** (our side / winemaker), the **Customer**
(the visiting guest; the reservation's `client_*` fields), and **Josh** (facility)
— then invoice, take payment, and confirm.

**Two case types** (from the Squarespace form):
- **production_tour** — production tour + tasting WITH the winemaker; Cecil
  *participates*, so the slot must align all THREE parties.
- **standard** — normal tasting; Cecil does NOT participate, she only *approves*
  (the Google Chat card is the approval gate); the slot is coordinated between
  Josh + customer.

**Party priority** when resolving what's next — always: **1) Cecil → 2) Customer → 3) Josh.**

This goal is the new organizing principle — see §6.

---

## 2. Legacy architecture (being replaced)

```
Gmail → tastingroom_mail_watcher.py (60s poll)
      → services/tastingroom_mailbox.py        (candidate filter, dedup, labels)
      → agents/case_desk_graph.py  (9-node LangGraph):
          store_raw_event → extract_claims → resolve_case → persist_claims
          → build_case_bundle → judge_case → save_case_judgment
          → update_reservation_cache → validate_and_act
      → services/case_judge.py  (Claude Sonnet judgment)
      → services/safety_guards.py  (hard rules)
      → create_action_request → Google Chat approval card → process_action_decision → Gmail send
```

Durable state in Supabase: `reservations`, `availability_claims`, `reservation_events`,
`reservation_action_requests`, `raw_email_events`, `case_judgments`.

---

## 3. The state machine (the part we are removing)

`TASTING_STATES` (services/tastingroom_service.py) — 23 discrete states the case
could be in. Routing was driven by deriving and matching `current_state`:

Happy path: `REQUEST_RECEIVED → NEEDS_FACILITY_CHECK → WAITING_FOR_JOSH →
FACILITY_AVAILABLE → NEEDS_INTERNAL_CHECK → INTERNAL_AVAILABLE →
READY_TO_OFFER_CLIENT → SLOT_OFFERED_TO_CLIENT → CLIENT_ACCEPTED_SLOT →
TENTATIVELY_BOOKED → INVOICE_SENT → WAITING_FOR_PAYMENT → PAYMENT_RECEIVED →
FINAL_CONFIRMED`.

Branches/errors: `CLIENT_REQUESTED_ALTERNATIVE, JOSH_UNAVAILABLE,
INTERNAL_UNAVAILABLE, NO_COMMON_SLOT, WAITING_FOR_CLIENT_REPLY, PAYMENT_OVERDUE,
AMBIGUOUS_REPLY, HUMAN_REVIEW_REQUIRED, CANCELLED_OR_DEFERRED`.

**Why it's going:** brittle — every new situation needs a new state and new routing.
Replaced by goal sub-conditions (§6) that the agent reasons over.

---

## 4. The judgment (intent we KEEP, in agent form)

`CaseJudgment` (services/case_judge.py) — the structured output Claude produced:
- `case_summary` — 1–3 sentence plain-English status
- `current_truth` — client_intent / facility_status / payment_status / confirmation_status
- `blockers` — concrete things preventing the next action
- `risks` — problems if we act now
- `uncertainties` — unconfirmed facts (distinct from blockers)
- `confidence` — 0–1
- `next_best_action` — `ToolPlan{tool_name, reason, requires_human_approval}`
- `evidence` — source-backed claims (`direct` vs `inferred_match`)
- `interrupt_level` — `none | digest | immediate`

`ToolPlan.tool_name` ∈ { none, draft_client_reply, draft_josh_availability_request,
draft_josh_booking_request, draft_invoice_message, draft_final_confirmation,
flag_for_staff_review }.

This evidence-backed, confidence-scored judgment is **kept** — it becomes the
agent's reasoning contract, not a discrete state transition.

---

## 5. Safety rules (KEEP — non-negotiable for a money/client flow)

- `safety_guards.py`: if `confidence < 0.6`, the action is downgraded to
  `flag_for_staff_review` with `interrupt_level="immediate"`.
- Every facility/client/payment action goes through a Google Chat **approval card**;
  no autonomous outbound email.
- `SAFE_ACTIONS` (the only actions the system may take): ask_internal_availability,
  ask_josh_availability, ask_client_alternatives, offer_client_slot,
  send_tentative_invoice, review_payment_status, send_final_confirmation,
  close_case, escalate, wait_for_josh.

---

## 6. Target: goal-oriented design (the rebuild)

Replace the 23-state enum with **goal sub-conditions** (`vertex_agent/goal_model.py`),
party-named and case-type aware:

```
case_type           : production_tour | standard   (from experience_type)
cecil_status        : unknown | ok | blocked        (ok = available for tour, OR approved for standard)
customer_commitment : none | offered | accepted | declined
josh_availability   : unknown | confirmed | unavailable
invoice             : not_sent | sent | paid
confirmation        : not_sent | sent
```

Gaps are emitted in **party priority order: Cecil → Customer → Josh**. For
`standard`, Cecil is approval-only (no scheduling); for `production_tour`, Cecil's
availability must align with the slot (3-party).

The agent: load case → derive goal state → take the FIRST (priority-ordered) GAP →
propose ONE action (from SAFE_ACTIONS) via the approval card → human approves →
Gmail send. No state machine; the "state" is a derived view of these conditions.

Powered by **Claude** (ADK + LiteLLM), running in Google Chat + Gmail.

---

## 7. KEEP vs REMOVE boundary (for the rebuild)

**REMOVE (legacy orchestration — tasting room only):**
- `agents/case_desk_graph.py` (the 9-node LangGraph)
- the `TASTING_STATES` machine + state-derivation/routing in `tastingroom_service.py`
- `services/case_judge.py`, `services/case_memory.py`, `services/safety_guards.py`
  (folded into the agent's instructions + a thin guard)
- the mailbox → graph wiring

**KEEP (reused as agent tools / unchanged):**
- Gmail intake + sending, Google Chat add-on + **approval cards**
- `db/repository.py`, the Supabase schema, `create_action_request` /
  `process_action_decision` (the channel-agnostic decision seam)
- email classification + fact extraction helpers (become tools)
- the **invoice** pipeline (`agents/invoice_graph.py`, `bot.py`) — SEPARATE system, untouched

**SEQUENCE (safe):** build the agent to parity → validate on real cases in parallel
→ *then* delete the legacy pipeline. Never delete first.
