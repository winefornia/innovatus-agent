# Winefornia Tasting Room — System Architecture Report

A complete walkthrough of how the tasting-room coordinator works: every step, how
the points connect into a decision tree, the programming decisions behind it, the
LLM chat pipeline, what the LLM does at each step, the end states, and the features
built for day-to-day use.

---

## 1. What it is, in one paragraph

The tasting room is an **email-native coordination agent**. A guest requests a visit
through the Squarespace website; the system reads that email, opens a *case*, and
coordinates the three parties — the **Customer**, **Winefornia** (Cecil/Lisa, our
side / the winemaker), and **Josh** (the facility) — entirely over **Gmail**, with
**Google Chat** as the human approval + control surface. It drives the case to one
of two end states: a **confirmed visit** (confirmation email + Square invoice +
Google Calendar invite to all three parties) or a **closed/withdrawn** case. Every
outbound action is approved by a human tapping a card. The *decisions* are made by
deterministic functions; **Claude** is used only to read/understand emails, draft
the wording, and power the conversational assistant.

---

## 2. Where it runs (Fly.io processes, one image)

| Process | Command | Role |
|---|---|---|
| `web` | `uvicorn app.main:app` | FastAPI: Google Chat webhooks, inbound-email webhook, `/activity` |
| `tastingroom_watcher` | `python scripts/tastingroom_mail_watcher.py` | 60s Gmail poller + stale-case sweep |
| `bot` | `python bot.py` | **Invoice** Telegram bot — a separate system |

The tasting room and the invoice system **share only the database and the Gmail
transport** — no business logic is shared, so they can't interfere.

---

## 3. The two ways in

**A. Website request (the main path).** Squarespace form → email to
`lisa@innovatuswine.com` → the watcher (authenticated as Lisa via domain-wide
delegation) picks it up within ~60s → intake → coordinate → a card in Google Chat.

**B. Conversational control.** Staff type in the Google Chat space (e.g. "status",
"send the Josh email for Mira", "make it warmer") → the assistant answers and can
drive the case (confirm-first for anything that touches the outside world).

---

## 4. The pipeline, step by step

### Step 1 — Intake (`services/tastingroom_mailbox.py` → `vertex_agent/intake.py`)
1. `read_email` fetches the message and **decodes the body** (prefers text/plain;
   falls back to HTML with tags stripped — Squarespace sends HTML-only).
2. `classify_email` labels the message: `squarespace_form` (keyed on the
   `form-submission@squarespace.info` sender), `josh_availability_reply`,
   `client_acceptance`, `invoice_payment_message`, etc.
3. `extract_email_facts` (deterministic parse of the form) **+** `llm_extract_email`
   (Claude reads the body and pulls name / date / guests / experience) → merged.
4. `find_or_create_reservation` matches by Gmail thread / facts, or opens a new case.
5. The Squarespace **experience selection** ("$85 Tasting" vs "$110 Production Tour
   and Tasting with Winemaker") is detected and **persisted** to set the case type.
6. Everything is written to Supabase: the reservation, availability claims, events,
   and the raw email (for replay). Unclassified + factless mail → an unresolved event.

### Step 2 — Coordinate (`vertex_agent/intake.coordinate_reservation`) — DETERMINISTIC
1. `derive_goal_state(reservation, claims)` (`vertex_agent/goal_model.py`) computes
   the case's sub-conditions.
2. `gaps()` returns the ordered open gaps (priority **Client → Winefornia → Josh**).
3. `_GAP_TO_ACTION[gaps[0]]` maps the top gap to a `SAFE_ACTION` — **a pure function,
   no LLM**.
4. `create_action_request` drafts the email text (this is where Claude writes the
   wording) and **posts the approval card** to the Google Chat space.
   - Skips if the goal is met, if we're waiting on a reply already requested, if the
     case is terminal, or if a card of that type is already pending (no duplicates).

### Step 3 — Human decision (the Google Chat card)
The card shows the customer name, the **`Case: TASTING-…` id**, the ask, and buttons.
An **allow-listed approver** (Cecil/Lisa) taps. Token-verified upstream; others are refused.

### Step 4 — Execute (`services/tastingroom_service.process_action_decision`) — DETERMINISTIC
The tapped button maps to a deterministic function:

| Button → decision | Function does |
|---|---|
| "Yes, we can do it" → `internal_available` | record availability; advance |
| "No, we can't" / "Suggest other times" → `internal_unavailable` / `suggest_alternatives` | ask the **client** for a new time |
| "Send it" → `approve` | `send_email` (the actual outbound) |
| "Invoice sent" / "Already paid" / "Send confirmation" → `invoice_sent`/`paid`/`queue_final` | payment progression → drafts final confirmation |
| "I'll handle it" → `escalate` | `HUMAN_REVIEW_REQUIRED` |
| "Don't send"/"Ignore" → `reject` | skip |
| Follow-up: `resend` / `ask_client` / `close` | re-post / ask client / cancel |

### Step 5 — Chain forward
- A **status-resolving** tap (yes/no/paid) → the deterministic coordinator re-runs and
  posts the next card.
- A **send** (`approve`) → we then **wait for the reply** (the watcher resumes when it
  arrives) — so a send never loops.
- Handlers that already know the next step hardcode it; the coordinator only fills
  genuine gaps (no duplicate cards).

### Step 6 — Time-based sweep (so nothing hangs)
Every ~30 min the watcher scans open cases; any stuck `WAITING_*` past its threshold
(Josh 48h, client 72h, payment 120h) gets a **follow-up card** — *Resend / Ask client
for a new time / Escalate / Close* — so a human moves it. Self-limiting (won't re-nudge).

### Step 7 — End state
- **Success → `FINAL_CONFIRMED`:** confirmation email + Square invoice + a **Google
  Calendar event inviting all three** (Lisa, customer, Josh — even for a standard
  tasting). Safe-mode routes everything to a test address until switched off.
- **Closed → `CANCELLED_OR_DEFERRED`:** withdrawn or ended; pending cards cleared.

---

## 5. The decision tree

```
NEW REQUEST (form)
  │
  ├─ gap: need_winefornia_availability → CARD "are we available?"
  │     ├─ Yes  → record → (Josh known? offer client : ask Josh)
  │     ├─ No / Suggest → CARD-send "ask client for a new time" → (await client)
  │     └─ I'll handle it → HUMAN_REVIEW_REQUIRED  [end-ish]
  │
  ├─ gap: need_josh_availability → CARD-send "email Josh?"
  │     └─ Send it → email Josh → WAITING_FOR_JOSH
  │            └─ Josh replies (inbound) → available?  yes → offer client
  │                                                    no  → ask client for new time
  │
  ├─ gap: offer_slot_to_client (only when Winefornia ok AND Josh confirmed)
  │     └─ Send it → email client → WAITING_FOR_CLIENT_REPLY
  │            └─ client replies → accepted → invoice
  │                              → declined → ask client for new time
  │
  ├─ gap: send_invoice → CARD-send "send invoice" → Square invoice → WAITING_FOR_PAYMENT
  │     └─ review_payment card: Invoice sent / Paid / Send confirmation
  │            └─ Paid → drafts final confirmation
  │
  └─ gap: send_final_confirmation → CARD-send "confirm"
        └─ Send it → confirmation email + invoice + 3-PARTY CALENDAR INVITE
                                                  → FINAL_CONFIRMED ★ END

ANY "no fit" at any point ───────────────→ ask the CLIENT for a new time (loop back)
ANY stuck WAITING past threshold ────────→ follow-up card (resend/ask/escalate/close)
ANY ambiguity / low confidence ──────────→ escalate → HUMAN_REVIEW_REQUIRED
CLOSE / withdraw ────────────────────────→ CANCELLED_OR_DEFERRED ★ END
```

**Two terminal end states:** `FINAL_CONFIRMED` and `CANCELLED_OR_DEFERRED`. Every
other state is transient and is *always* driven forward — by a human tap, an inbound
reply, or the time-based sweep.

---

## 6. The goal model (the brain) — `vertex_agent/goal_model.py`

Instead of a brittle state-machine enum, the case is described as the **sub-conditions
of the goal**, derived from the data:

```
case_type           : standard | production_tour   (from the Squarespace dropdown)
cecil_status        : unknown | ok | blocked        (Winefornia approval/availability)
josh_availability   : unknown | confirmed | unavailable
customer_commitment : none | offered | accepted | declined
invoice             : not_sent | sent | paid
confirmation        : not_sent | sent
```

`gaps()` returns what's still open, **in priority order Client → Winefornia → Josh**,
and only offers the client a slot once it's actually available. `is_goal_met()` is the
success test. This is a **pure function** — the heart of the deterministic harness.

---

## 7. Key programming decisions (and why)

1. **Goal-driven, not a state machine.** The old design routed on a 23-state enum and
   was brittle. The goal model derives the next step from conditions, so new
   situations don't need new states.
2. **Deterministic coordinator; LLM out of the decisions.** What to do next is a pure
   function (`gaps → action`). The LLM can *influence* (a better email) but **cannot
   decide or change state**. This is the core harness.
3. **Human-in-the-loop, always.** No email/invite is ever sent without a human tapping
   a card. The agent *proposes*; the human *decides*; a function *executes*.
4. **LLM scoped to language only:** extract facts from emails, draft email wording, and
   power the chat assistant. Never coordination decisions, state changes, or sends.
5. **Confirm-first** for outside-world chat actions (send / cancel / revoke): staged,
   then executed only on an explicit "yes".
6. **Time-based sweep** so a non-responsive party can never leave a case hung.
7. **Hardening:** intake/coordination **never raise** (a bad email can't crash the
   watcher); failures are recorded so nothing is reprocessed forever; **no duplicate
   cards**; bounded agent runs.
8. **Auth built for stability:** Gmail/Calendar via **domain-wide delegation** (service
   account acting as Lisa — no token expiry); the Google Chat app verifies its own
   signed token; only allow-listed approvers (Cecil/Lisa) can act.
9. **Clean separation** from the invoice system (shared DB + Gmail transport only).
10. **Replayable + idempotent:** raw emails are stored; processed mail is de-duped.

---

## 8. The LLM chat pipeline (conversational control + editability)

`vertex_agent/chat_agent.py` + `vertex_agent/chat_actions.py`.

**Input:** free text in the Google Chat space (e.g. "what's open?", "send the Josh
email for Mira", "make the offer warmer and mention parking", "cancel the test case").

**Flow (`discuss(text, user)`):**
1. Identify the acting approver (a contextvar the tools read — used for the audit
   trail and the per-user confirmation store).
2. If the user has a **staged** confirm-first action, re-inject it into the prompt so
   their "yes" resolves to exactly that.
3. Run the assistant (Claude via ADK) with these tools:

| Read | Write (route through the same `process_action_decision` as the cards) |
|---|---|
| `open_cases_status` (the status board) | `stage_send_email` → confirm → send |
| `find_cases` (name → case) | `revise_draft` (edit the unsent draft — **editability**) |
| `get_case` (full detail) | `mark_invoice_sent` / `mark_paid` |
| `list_open_cases` | `manual_handle` (hand to a human) |
| | `stage_cancel_case` / `stage_revoke_decision` (confirm-first) |
| | `confirm_pending_action` / `cancel_pending_action` |

**Editability** specifically: "make it warmer / mention parking" → `revise_draft` →
`_revise_email_with_llm` rewrites the **not-yet-sent** draft, stores it, and shows the
new version; then "send it" sends the revised draft. So staff can shape any outbound
email conversationally before it goes.

**Safety:** reversible/internal actions (revise, mark paid, manual handle) run
immediately; outside-world actions (send, cancel, revoke) are **confirm-first** —
staged in a per-user store, executed only on "yes". The assistant uses Google Chat
formatting (not Markdown) so messages read cleanly.

---

## 9. What the LLM handles at each step (vs. what's deterministic)

| Step | LLM does | Deterministic function does |
|---|---|---|
| Intake | extract name/date/guests/experience from the email | classify, resolve, persist |
| Coordinate | — | `gaps → action`, post the card |
| Draft | write the email wording | choose recipient, build the card |
| Human decision | — | the button → `process_action_decision` |
| Chat | answer questions, revise drafts, interpret requests | every state change + send routes through the same functions |

The LLM never decides the next step, changes state, or sends — those are functions
gated by a human.

---

## 10. End states

- **`FINAL_CONFIRMED`** — the success end state: confirmation email to the client, the
  Square invoice (sent earlier in the flow), and a **Google Calendar event inviting
  all three parties** (Lisa, customer, Josh), created via the calendar service on the
  same domain-wide-delegation auth. Even a standard tasting invites all three.
- **`CANCELLED_OR_DEFERRED`** — the close/withdraw end state; any pending cards are
  cleared. Reversible via "revoke" from chat.

---

## 11. Features built for day-to-day use

- **Status board** — type "status" / "what's open?" → every open case by name + case
  id, who's confirmed, and what each is waiting on.
- **Conversational assistant** — ask anything about a case and drive it from chat
  (send/revise/mark paid/hand off/cancel/revoke), confirm-first for risky actions.
- **Draft editing in chat** — reshape any outbound email in plain language before it
  sends.
- **Case-id labels on every card** — concurrent cases are never confused.
- **Time-based follow-ups** — stuck cases surface themselves; nothing is forgotten.
- **Approver allow-list** — only Cecil/Lisa can approve or act.
- **Safe-mode** — emails + calendar invites route to a test address until you flip it.
- **Stable mailbox auth** — domain-wide delegation means no OAuth-token expiry.
- **Smoke-test filtering** — test cases never clutter the board or the assistant.

---

## 12. Data model (Supabase)

`reservations` (the case), `availability_claims` (source-backed claims per party),
`reservation_events` (audit trail), `reservation_action_requests` (the cards +
decisions), `raw_email_events` (replay), `unresolved_reservation_events` (mail that
couldn't be matched).

---

## 13. Key files

| File | Role |
|---|---|
| `scripts/tastingroom_mail_watcher.py` | 60s poller + stale sweep |
| `services/tastingroom_mailbox.py` | Gmail ingestion, labels, `sweep_stale_cases` |
| `vertex_agent/intake.py` | intake + **deterministic coordinator** |
| `vertex_agent/goal_model.py` | the goal model (gaps, conditions) |
| `services/tastingroom_service.py` | actions, drafting, `process_action_decision`, calendar |
| `services/calendar_service.py` | 3-party calendar invite |
| `app/adapters/google_chat_tastingroom.py` | Chat webhook: cards, clicks, chat routing |
| `vertex_agent/chat_agent.py` + `chat_actions.py` | conversational assistant + write tools |
| `db/repository.py`, `db/models.py` | Supabase data layer |
| `services/gmail_service.py` | Gmail read/send + DWD auth |
