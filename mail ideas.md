
2.however we still have to use claude code, inside the claude app in order to actually touch the code base. it has the machinery to actual run test and open code - make the system to whatever direction you want it to be like. 

the project file inside claude is where all system folders are being organized.
github (where the code base is  https://github.com/winefornia/innovatus-agent ) is integrated, so when you have questions you can ask claude to understand how the file and database is constructed. 

In order to ideate or tweak some features based on your taste, you could also ask claude for general ideas and then fix on seperately on claude code tab. It has the machinery to actually run test and build sytem around it.

---

# Whole Picture

## 1. The cast

```
                        ┌───────────────────────┐
                        │          YOU          │
                        │   two yes buttons:    │
                        │   card-tap and Merge  │
                        └───────────┬───────────┘
                                    │
                  ┌─────────────────┴──────────────────┐
                  │       THE HEAD — CLAUDE CODE       │
                  │        (in your Claude app)        │
                  │                                    │
                  │  knows how the whole system is     │
                  │  built, and is the ONLY thing      │
                  │  that can rewrite the two agents   │
                  │  below.                            │
                  │   • ask anything (Project chat)    │
                  │   • change anything (Code tab)     │
                  │   • morning checkup (runs daily,   │
                  │     reports back to you)           │
                  └────────┬─────────────────┬─────────┘
                           │    controls  &  rewrites    │
                ┌──────────┴─────────┐  ┌────────┴───────────┐
                │   INVOICE AGENT    │  │ TASTING ROOM AGENT │
                │                    │  │                    │
                │ lives in Google    │  │ lives in Google    │
                │ Chat. You give it  │  │ Chat. Watches the  │
                │ an order — it      │  │ booking inbox 24/7 │
                │ prices it, drafts  │  │ (every 60 sec) and │
                │ the Square invoice │  │ walks each visit   │
                │ after your yes,    │  │ step-by-step with  │
                │ and tracks it      │  │ approval cards —   │
                │ until it's paid    │  │ from request to    │
                │                    │  │ confirmed visit    │
                └─────────┬──────────┘  └─────────┬──────────┘
                          │                       │
                          │  run in PARALLEL, 24/7, on the
                          │  server — never share a case,
                          │  act only after your ✅
                          ▼                       ▼
             ┌────────────────────┐   ┌────────────────────────┐
             │     THE TOOLS      │   │      THE RECORDS       │
             │   Gmail · Square   │   │    (cloud database)    │
             │  Google Calendar   │   │ every booking, invoice,│
             └────────────────────┘   │ decision, step — ever  │
                                      └────────────────────────┘
```

The chain of command flows only downward. The head — Claude Code — holds the
knowledge and is the only thing that can rewrite the two agents; the agents can
never change the head. And every arrow that touches the outside world (an
email, an invoice, a calendar invite) passes through **you** first: nothing
changes and nothing is sent without one of your two yeses.

## 2. The two agents, running in parallel

Both agents live on the always-on server and appear to you as two separate
chat apps inside Google Chat. They never share a case — a wine order can never
turn into a tasting booking or the other way around.

**The invoice agent** (the *Winefornia_Invoice* chat). You type or paste an
order — or forward an email, or attach a PDF — and it extracts the details,
finds the customer, prices everything by the right tier, and shows you the
total. Only after your explicit yes does it create the Square invoice, and
even then it doesn't trust "OK" from Square: it waits for Square's own
confirmation email before it considers the job done, and tracks the invoice
until it's paid.

**The tasting room agent** (the *Winefornia Tasting Room* chat). Its ears are
the winery inbox, which it checks every 60 seconds around the clock. When a
website booking form arrives it opens a *case* within a minute and posts a
card in your Chat. From there it walks the whole visit with you, one approval
card per step — internal check, asking Josh, offering the client a slot, the
deposit invoice, the final confirmation with calendar invites. If an email
isn't clearly a booking, it never guesses — it sets it aside in a review pile
for a human glance.

**Above them, the head — Claude Code.** The head doesn't handle bookings or
invoices itself; it *governs*. It holds the full knowledge of how both agents
are built (the code, the docs, the records), and it's your one place to ask
anything and change anything. It works for you in three shapes, all inside
your Claude app, all on your subscription:

- **ask** — *"what's waiting on me?"*, *"why did the Kim booking pause?"*,
  *"how does the deposit step work?"* — answered from the live records and
  the system's own documentation;
- **change** — you describe it in plain English; the head rewrites the agents'
  instructions (the code), proves the change is safe with the automatic tests,
  and hands you the Merge button;
- **the morning checkup** — the head wakes daily on its own, looks across both
  agents for anything stuck or unusual, and messages you *only if something
  needs you*. You change its habits just by telling it — no code involved.

All three layers stay in step because they share one memory: **the records**.
The two agents write there minute by minute; the head reads it whenever you
ask, and every morning.

## 3. Where everything is saved

Your data lives in **one cloud database** (the service is called Supabase — a
professionally hosted, backed-up database that is always on, independent of
any one computer or inbox). In plain words, it holds:

| What | In plain words |
|---|---|
| Reservations | every tasting booking: who, when, how many guests, current step |
| Invoices | every invoice the system drafted, and whether Square confirmed it created / paid |
| Cases | one file per client journey, from "request received" to "confirmed" or "cancelled" |
| The trail | who approved what, when, and what the system did about it — permanent |
| Every email fetched | a copy of each mail the watcher picked up, so nothing is lost even if a step fails |
| The review pile | emails the worker set aside because they weren't clearly a booking — waiting for a human glance |

Two consequences worth knowing. First, **your inbox is not the system's
memory** — even if an email is archived or deleted, the record survives.
Second, **any question about the past has an exact answer** — "what happened
with Mira's booking?" is a lookup, not a memory test.

## 4. Server-side facts (for the technically curious)

You never need this day-to-day — it exists so any future helper, human or AI,
can orient in one minute.

| | |
|---|---|
| The server | a small rented machine at Fly.io, app name `winefornia-agent`, in a data center in Ashburn, Virginia |
| Running on it | the two agents (invoice + tasting room), powered by two programs: the **listener** (receives your card-taps and chat messages) and the **mail watcher** (checks the inbox every 60 seconds) |
| If it crashes | it restarts itself; a standby machine waits on separate hardware; a health check restarts it if it wedges |
| If the watcher goes silent | a heartbeat monitor notices and posts an alert in Google Chat — silence is never invisible |
| The code's home | GitHub: `github.com/winefornia/innovatus-agent` — the recipe book; every version ever written is kept |
| How changes go live | automatically: after you press Merge, 305 safety tests run once more, then the server updates itself (~2 minutes). Nobody deploys anything by hand |
| The database | Supabase (cloud Postgres) — see section 3 |
| The server's own AI | a small Claude model reads incoming emails into tidy fields (name, date, guests). It extracts; it never decides or sends |
| Accounts it acts through | the winery Gmail, Square, Google Calendar — always only after your yes |
| Rough running cost | a few dollars a month for the server + database; the intelligence runs on your Claude subscription |

## 5. Example guide — one booking, start to finish

Say a client, Mira, books a tasting for 4 through the website:

| When | What happens | Who did it |
|---|---|---|
| 9:02 | Mira submits the booking form; it arrives as an email | the website |
| 9:03 | Watcher spots it, opens case `TASTING-…-MIRA`, posts in your Chat: *"🍷 New tasting request — Mira, June 7, 2pm, 4 guests"* | the worker |
| 9:03 | A card asks: *"The caves look free. Does this work for our schedule?"* | the worker |
| 9:41 | You tap ✅ | **you** |
| 9:42 | Card: *"Ask Josh about availability?"* → ✅ → email goes to Josh; his later reply attaches itself to the same case | you + the worker |
| 11:15 | Card: *"Offer Mira the 2pm slot?"* → ✅ → offer email sent | you + the worker |
| next day | Mira replies "perfect!" — it attaches to the case; card: *"Send the tasting deposit invoice?"* → ✅ → Square invoice created and sent | you + the worker |
| later | Square's own email confirms the invoice was paid — the case updates itself | Square + the worker |
| final | Card: *"Send final confirmation?"* → ✅ → confirmation email + calendar invites to everyone | you + the worker |
| done | Case closed. Every step above is in the records, forever | the records |

Notice the shape: **the system did all the chasing, remembering, and typing;
every decision was a tap by you.** If you never tap, nothing is ever sent — a
case simply waits, and the morning checkup will remind you it's waiting.

## 6. Example guide — asking, changing, and being looked after

**Asking (Project chat, anytime, any device):**

> **You:** what's pending right now?
> **Claude:** Two things are waiting on you: ① Mira's deposit card from
> yesterday, ② a July 22 request that needs the internal schedule check.
> One thing to know: Josh replied on the July 22 case — his suggested time
> differs from what the client asked for.

**Changing (Code tab — feels like the same chat):**

> **You:** deposit invoices should say NET-30 instead of due on receipt
> **Claude:** Done — I changed the payment term the deposit invoice uses and
> updated the wording of the email that goes with it. All 305 safety checks
> pass. Review and press Merge when ready; the system updates itself about
> two minutes later.

*(Merge is your second yes button. Old versions are kept forever — "undo
yesterday's change" is a perfectly good instruction.)*

**Being looked after (the morning checkup, arrives on its own):**

> ☀️ Morning check: 1 thing needs you — the Kim visit is in 3 days and the
> final confirmation hasn't gone out (the card is waiting in Chat).
> Everything else is healthy: inbox watched, last booking processed in 58
> seconds, review pile empty.

## 7. The two yes buttons (the whole control story in four lines)

- A **card in Chat** approves *one action for one client* — send this email, this invoice.
- The **Merge button** approves *a change to how the system itself behaves*.
- Nothing external and nothing permanent happens without one of the two.
- Everything is visible on request, written down permanently, and undoable.


