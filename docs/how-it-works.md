# How the Winefornia Agent Works

*A plain-language guide for the people who use it. Last updated July 2026.*

One server runs two completely separate assistants, both living in Google Chat:

| | Invoice pipeline | Tasting-room pipeline |
|---|---|---|
| What it does | Turns an order into a Square invoice and tracks it to paid | Turns a website booking request into a confirmed visit |
| Where you talk to it | **Winefornia_Invoice** chat app | **Winefornia Tasting Room** chat app |
| What opens a case | You, typing/pasting an order in Chat | A client, submitting the Squarespace booking form |
| What closes a case | Square's own email confirms created / paid | Final confirmation sent (or cancelled) |

The two pipelines never share cases. Square's notification emails belong to the
invoice pipeline only; the Squarespace form belongs to the tasting room only.

---

## Part 1 — The invoice pipeline

### Two ways to talk to it

**The assistant (free-form chat).** Type naturally: *"what's wholesale on the
2023 Viognier?"*, *"set the FOB price to $41"*, *"invoice Christina Yoo, 1 case
Viognier, 15% off"* — or paste a forwarded email or attach an order PDF. It
understands intent and works the order with you.

**The wizard (step-by-step cards).** The original card-driven flow: paste an
order, then confirm customer → tier → payment schedule → shipping → approve,
each as a button card. Same pricing, same Square calls underneath.

### The conversation is a case

Everything you say in the space is one ongoing **case** that runs until the
invoice is drafted/sent or you cancel. Practical consequences:

- Short replies work. If the assistant asks "which vintage, which tier?" you
  can answer "2023 / Other" — it combines your answer with everything you
  already told it (customer, email, discount, shipping). It will not re-ask
  for facts you already gave.
- The case survives across messages and hours (up to ~2 days of quiet).
- Coming back later works: say *"send Christina's invoice"* days after
  drafting — it finds the draft in the records and in Square itself, so this
  works even after a server restart.
- Replies stay in the thread. If you ask in a thread, the answer comes back
  in that same thread — including when the assistant says *"working on it"*
  first and posts the real answer a moment later. In a space without threads
  it simply posts normally.

### Money is always confirm-first

Nothing touches Square or live pricing without your explicit "yes":

1. You ask for an invoice → it prices the order and shows the total.
2. If shipping isn't settled it asks: *free, or what amount?* (The shipping
   line becomes a real line item on the Square order.)
3. It stages the action and shows one line: *"Reply yes to confirm."*
4. Only your **yes** creates (or sends) anything. "No" / "never mind" discards
   it. Staged actions expire on their own after 10 minutes.

The same applies to price changes, tier changes, and sending drafts.

### The validation loop — how a case actually closes

Creating an invoice does **not** close the case. The Square API saying "OK" is
treated as a claim, not proof. The case stays **open (pending verification)**
until Square's own notification email arrives in the mailbox and the validator
matches it by invoice number:

```
you: "yes"                        case opens: pending_verification
  └→ Square draft created (#202512)
        └→ email: "A new invoice was created … (#202512)"
              └→ case verified: created_confirmed   → workflow closed
        └→ email: "An invoice was paid … (#202512)"
              └→ case upgraded: paid_confirmed      → completed_paid
```

The validator runs every minute. Square mail that matches no known invoice
(e.g. one made by hand in the Square dashboard) is labeled
**Invoice Validation/Unmatched** in Gmail for a human to glance at. Ask the
assistant for *recent invoices* any time — each one shows its verification
state, so "still open" is always visible.

### Checking on the system from Claude

Besides Google Chat, the invoice pipeline has a **read-only console for
Claude** (claude.ai custom connector or Claude Code): recent invoices, open
cases, per-case traces, staged-but-unconfirmed actions, and watcher health.
It can look at everything and change nothing — money and outbound email stay
behind the Chat confirm-first flow. It's off until the `MCP_INVOICE_SECRET`
secret is set, and denies everything without it.

---

## Part 2 — The tasting-room pipeline

Every case follows the same path (internally we call it "the Mira Park path,"
after the first booking that ran it end to end). There is exactly one way a
case is born, and every step to confirmation is approval-gated in Chat.

### 1. Intake — the Squarespace form, and only the Squarespace form

A client books through the website → Squarespace emails the form submission →
the mail watcher (checks every ~60s) opens a case and **immediately posts a
notification** to the tasting-room space:

> 🍷 **New tasting request** — Mira Park
> • Date: 2026-06-07 at 14:00 · Guests: 4 · Experience: Tasting
> • Email: mirasopa@gmail.com
> Case TASTING-20260607-4G-MIRA-PARK opened from the Squarespace form.

Nothing else can open a case. Square invoice emails, marketing mail, sales
summaries — anything that isn't an identity-bearing booking form is either
routed to its own pipeline or quarantined for human review. (This matters:
wine-order Square emails once created phantom "tasting cases" for wine
customers. That entire class of confusion is now structurally impossible.)

### 2. Coordination — approval cards for every step

The coordinator reads the case, decides the next step, and posts a card. You
decide; it acts. The typical sequence:

| Step | Card you see | What happens on approval |
|---|---|---|
| Internal check | "The caves are available. Does this work for our schedule?" | Recorded — nothing sent |
| Facility check | "Ask Josh about availability?" | Email goes to Josh; his reply attaches to the same case |
| Offer | "Offer the client this slot?" | Email to the client with the slot |
| Client accepts | (their reply attaches to the case) | Coordinator moves to payment |
| Deposit | "Send the tasting deposit invoice?" | Square invoice created + sent |
| Payment | "Invoice sent / Already paid?" | You confirm payment status |
| Finish | "Send final confirmation?" | Confirmation email + calendar invites |

Every inbound email (client reply, Josh reply) is matched to its case by the
email thread, so the whole story lives on one case from form to confirmation.

### 3. What keeps it honest

- **Safe mode** (`TASTINGROOM_SAFE_MODE`): when on, all outbound email goes to
  the test recipient instead of real clients.
- **One decision per card**: once someone approves/rejects a card, a second
  click gets "already processed" — no double sends.
- **Stuck cases surface**: cases waiting too long produce a follow-up card;
  unparseable mail lands in a quarantine list instead of silently vanishing.
- Deposit payment status is confirmed by **you** (card click) and can be
  audited against the Square API — never guessed from emails.

---

## Part 3 — Adding another person (and the group-chat question)

> *"If I want another person rather than Cecil to use this Google Chat app,
> how do we migrate? Could we make a group chat? Or does the API get wired to
> both — wouldn't that make two weird conflicting decisions?"*

Short answers: **adding a person is config, not migration; group chat works
today; and no — two people cannot produce conflicting decisions**, because of
how state is keyed. Details below.

### Who is allowed to do what

| Surface | Gate | Where it's set |
|---|---|---|
| Invoicing assistant | Email allowlist — non-listed users are refused | `GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS` |
| Tasting-room cards & chat | Email allowlist — non-listed users can't act | `GOOGLE_CHAT_TR_AUTHORIZED_EMAILS` |
| Invoice wizard | Google-signed webhooks + space membership | (whoever is in the space) |

Both allowlists **fail closed**: if the variable is empty or malformed, everyone
is denied rather than everyone allowed.

### To add a person (e.g. Audrey)

1. **Allowlist them** — one command per app, comma-separated full list:

   ```
   fly secrets set GOOGLE_CHAT_INVCHAT_AUTHORIZED_EMAILS="cecil.park@winefornia.com,lisa@innovatuswine.com,lisa@winefornia.com,audrey@winefornia.com"
   fly secrets set GOOGLE_CHAT_TR_AUTHORIZED_EMAILS="cecil.park@winefornia.com,lisa@innovatuswine.com,lisa@winefornia.com,audrey@winefornia.com"
   ```

   (Machines restart automatically; takes effect in under a minute.)

2. **Give them the app in Google Chat** — either they start a DM with the app
   (Chat → search the app name → message it), or you add the app to a group
   space they're in. If they can't find the app, its **visibility** is managed
   in the app's Google Cloud project (Chat API → Configuration → visibility) —
   set it to the domain or add their address.

That's it. Nothing about the server, webhooks, or "wiring" changes — the same
endpoint serves every space and every user; each message arrives stamped with
the sender's identity and the space it came from, and the allowlist decides.

### Group chat vs separate DMs — and why decisions can't collide

Yes, you can make a group space with the app + several people. Whether you
should depends on the surface, because each one keys its state differently:

**Tasting room → use one shared group space (recommended).** Cards post to a
single configured space (`GOOGLE_CHAT_TR_SPACE`). Put Cecil, Lisa, and anyone
new in that space: everyone sees the same cards, whoever gets there first
decides, and the card then locks ("already processed"). Every decision records
*who* made it. Two people cannot approve the same thing twice — the action is
one-shot by construction.

**Invoicing assistant → per person, even in a shared space.** Conversations
are keyed by **space + sender**. If Cecil and Audrey both talk to it in one
group space, each has their own private case: Cecil's "yes" can only ever
confirm *Cecil's* staged action (pending confirmations are keyed per user,
too). There is no shared draft two people could push in different directions.
The tradeoff is the flip side: in a group space, Audrey *cannot* answer a
question the assistant asked Cecil. If they'd ever want to hand orders to each
other mid-conversation, give them separate DMs instead — otherwise a group
space is fine.

**Invoice wizard → one space = one order at a time.** The wizard's state is
keyed by the space itself, so a shared wizard space means everyone is driving
the *same* order — useful for four-eyes review, chaotic for parallel work. Its
guards (buttons validated against the current step, duplicate clicks dropped,
Square writes idempotent) mean even simultaneous clicks can't double-create an
invoice — the second decision is simply ignored. For parallel work, use one
wizard space per person.

So the "two weird decisions" scenario is designed out three different ways:
per-user staging (assistant), one-shot cards (tasting room), and per-space
single state + idempotent writes (wizard). The worst case of two people acting
at once is a polite "already processed," never a second invoice or a second
email.
