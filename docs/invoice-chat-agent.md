# Winefornia Invoicing — Conversational Chat Agent

A walkthrough of the invoicing chat assistant: what it is, how it copies the
tasting-room chat pipeline, the actions it can take, the confirm-first safety
model, the pricing-write rule, and the smoke-test steps (local + deploy).

---

## 1. What it is, in one paragraph

The invoicing chat agent is a **conversational control surface over Google Chat**.
Staff type in plain language — "what's wholesale on the 2021 Cabernet Franc?",
"set the FOB price to $41", "make Corporate 25% off", "invoice Acme for 3 cases at
wholesale and send it" — or drop in an order PDF, and the agent understands the
intent and acts. It can **only** do a tight, allow-listed set of things; it can't
improvise. Anything that touches money or live pricing is **confirm-first**: the
agent stages the action, shows a one-line "reply yes" summary, and only mutates on
the user's affirming reply. It is a **sibling to the existing LangGraph invoice
graph** (`agents/invoice_graph.py`) — the graph keeps running; this is the chat
brain that wraps the same services (`product_service`, `square_service`,
`pdf_service`). It is a direct copy of the tasting-room chat pipeline
(`vertex_agent/chat_agent.py` + `chat_actions.py`).

---

## 2. Where it runs

| Piece | File | Role |
|---|---|---|
| Webhook endpoint | `app/main.py` → `/webhooks/google-chat/invoice-chat` | Verifies the Chat JWT, dispatches to the adapter |
| Adapter | `app/adapters/google_chat_invoice_chat.py` | Ack-then-post, dedup, per-space lock, auth gate, PDF attachment digest |
| Agent | `vertex_agent/invoice_chat_agent.py` | `discuss(text, user)` — one ADK `LlmAgent` (Claude) + tight system prompt |
| Tools | `vertex_agent/invoice_chat_actions.py` | Per-user pending store + read tools + confirm-gated write tools |
| Config | `app/config.py` → `GOOGLE_CHAT_INVCHAT_*` | Config-gated; dormant until set |

It runs in the same `web` process and image as everything else. The service
account for PDF download + async result posting falls back to the tasting-room /
shared invoice key, so a single-project setup works without a new bot.

---

## 3. The pipeline (user texts something → action)

1. **Inbound** — a Google Chat MESSAGE hits `/webhooks/google-chat/invoice-chat`.
   The adapter verifies the JWT, checks the sender is an authorized approver
   (`GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS`), and drops duplicate/retried messages.
2. **PDF digest** — if the message has a PDF attachment, the adapter downloads it
   with the bot token and runs `extract_invoice_fields_from_pdf` to turn it into a
   natural-language order summary, appended to the message text as input state.
3. **Understand** — `discuss()` runs the ADK agent. Read tools answer directly;
   for an action, the agent picks the matching `stage_*` tool.
4. **Stage** — the `stage_*` tool validates inputs, records the intent in a
   per-user pending store (10-min TTL), and returns a one-line "reply yes to
   confirm" summary. **Nothing is mutated.**
5. **Confirm** — the next message is re-injected with a `[pending confirmation]`
   note. On "yes" the agent calls `confirm_pending_action()`, which pops the
   pending entry and runs the real mutation through the shared services. On "no"
   it calls `cancel_pending_action()`.
6. **Ack-then-post** — if a turn runs past ~20s (LLM loop, PDF, Square calls), the
   adapter acks Google Chat and posts the result to the space when it lands.

---

## 4. What it can do (and nothing else)

**Read (immediate, no confirmation):**

- `find_products(query)` — look up wines by name/alias.
- `get_pricing(product, vintage)` — MSRP + every per-channel price for one wine.
- `list_tiers()` — pricing tiers, discount %, multiplier.
- `recent_invoices(limit)` — recent invoices and status.
- `price_order(...)` — a priced quote; nothing created.
- `client_lookup(customer)` — a client's profile: contact info, pricing tier,
  type, notes (from the `customers` table).
- `client_history(customer, limit)` — the client's past invoices and orders:
  dates, totals, paid status, items. Reads the synced Square history
  (`square_invoices` + `square_orders`, falling back to orders alone when the
  invoice sync has gaps) plus recent agent-created `invoice_logs`.
- `usual_order(customer)` — the client's usual (most recent) order, re-priced
  at today's prices for their tier — powers recommendations and "same as usual".
- `client_notes(customer)` — remembered facts about a client (preferences,
  watch-outs) from Mem0 skill memory + the profile's notes field.

**Act (confirm-first):**

- `stage_set_channel_price(product, channel, price, vintage)` — wholesale / fob /
  club_member / ex_cellar.
- `stage_set_msrp(product, price, vintage)` — retail MSRP.
- `stage_set_tier(tier, discount_percent, msrp_multiplier)` — a whole tier; affects
  every product on it. Pass -1 to leave a field unchanged.
- `stage_set_availability(product, tier, available, vintage)` — tier availability
  (`tier_unavailable`).
- `stage_invoice(customer, email, tier, items, schedule, send)` — create (and
  optionally send) a Square invoice. `send=true` only when staff clearly want it
  sent; otherwise it's a draft.

---

## 5. The pricing-write rule (both sources, in lockstep)

Price/category edits write to **both Supabase and the `app/data` JSON**:

- **Supabase first.** If the Supabase write fails, the JSON is left untouched and
  nothing changes — a chat edit never introduces drift on its own.
- **Then JSON.** If Supabase succeeds but the JSON write fails, the agent reports a
  drift warning so the file can be synced.

Editable values: per-channel price (`tier_prices`), MSRP (`msrp_bottle_cents`),
tier discount %/multiplier (`pricing_tiers`), and tier availability
(`tier_unavailable`). The Supabase `products` table carries a `tier_prices` jsonb
column (note: `db/schema.sql` is stale and omits it).

> **Caveat:** on Fly's ephemeral filesystem the JSON write is not durable across
> deploys — Supabase is the durable source. Treat the JSON half as best-effort
> until/unless we commit it back.

---

## 6. Hardening

- **Idempotency** — each staged invoice carries a stable token, passed as
  idempotency keys to the Square customer/order/draft/publish calls, so a retried
  confirm dedupes instead of creating duplicates. The confirm gate also pops the
  pending entry *before* executing, so a second "yes" can't double-fire.
- **Input parsing** — currency strings (`$58`, `1,200`) and percents (`25%`) are
  parsed defensively rather than crashing.
- **Range validation** — price > 0; discount 0–100; multiplier 0–2; payment
  schedule normalized to `UPON_RECEIPT | NET_7 | NET_14 | NET_30`.
- **No silent product guess** — ambiguous names ("cabernet" → Sauvignon vs Franc)
  and multiple vintages prompt; resolution prefers 750ml / non-variable.
- **Authorization** — only `GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS` may act (empty =
  open to any space member).

---

## 7. Smoke tests

### 7a. Local (no deploy, no prod writes, no Square)

Read tools hit live Supabase read-only; the write-path logic runs against stubbed
Supabase + a temp JSON dir. The suite covers: currency/percent/schedule parsing;
read lookups + ambiguity prompts; staging input validation; the confirm-gate
lifecycle; invoice staging (needs-price, email guard, idempotency token, schedule
normalization); lockstep orchestration (SB-ok+JSON-ok, SB-fail-aborts-JSON,
drift-warning); and real JSON mutation against a temp catalog copy. Also: adapter
plumbing (welcome on add, unauthorized blocked, dedup).

**Live LLM-loop verification (opt-in).** The real ADK + Claude tool-selection loop
is verifiable locally once `google-adk` + `litellm` are installed (they're in
`requirements.txt`) and a real `ANTHROPIC_API_KEY` is in `.env`. It's gated off by
default so the normal `pytest` run makes no API calls:

```
RUN_LLM_TESTS=1 PYTHONPATH=. .venv/bin/python -m pytest \
    tests/integration/test_invoice_agent_loop.py -q
```

It asserts a read query answers without staging, and a price-edit stages a
confirm-first action without writing (write paths are mocked, so it can't mutate).
`vertex_agent/invoice_chat_agent._ensure_anthropic_key()` backfills the key from
`app.config`/`.env` so the agent runs regardless of import order.

### 7b. Deploy (the LLM loop)

1. Deploy; confirm `google-adk` / `litellm` are in the image (`pip show google-adk`).
2. Set `GOOGLE_CHAT_INVCHAT_*` and add the Chat app to a space (or reuse the
   shared / tasting-room service account).
3. In the space, run this sequence:
   - `what's wholesale on the 2021 Cabernet Franc?` → a read answer with the real
     price (no confirmation).
   - `set the 2021 Cabernet Franc wholesale to $90` → a "…reply yes to confirm"
     line; **nothing written yet**.
   - `yes` → "Updated Supabase + catalog JSON ✅"; verify with `get_pricing`.
   - `make Corporate 25% off` → confirm-gated tier edit.
   - Attach an order PDF → a digested draft proposal + confirm gate.
   - `invoice Acme for 1 case Cabernet Franc 2021 at wholesale` (no "send") → draft
     only; then `send it` → publishes (use a Square sandbox token first for a dry
     run).
4. Check logs for `[inv:gc:auth]` and `[inv:gc]` lines.
