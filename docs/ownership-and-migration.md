# Ownership & Migration Guide

*Who owns every account the system depends on, and how to move control to the
winery. Written for humans and for AI agents doing the migration work.
Facts below were verified live on 2026-07-13; re-verify anything marked ❓.*

## 1. Who controls what today

| Provider | What it holds | Controlled by (verified 2026-07-13) | Migration needed? |
|---|---|---|---|
| **Fly.io** | The server (`winefornia-agent` app: web + mail watcher, all runtime secrets) | `haeinej@gmail.com` **personal** org — sole owner | **Yes — highest priority.** If this account is lost, nobody can deploy, read logs, or change secrets |
| **Supabase** | The database (org `winefornia`, project `zlbixpklvejcuxifqzjk`, us-east-1) | Org already named `winefornia`; project created 2026-05-24 under cecil.park@winefornia.com's name. ❓ Verify member list & roles in dashboard | Verify: Cecil = Owner, Haein = Administrator, billing on a winery card |
| **GitHub** | The code (`winefornia/innovatus-agent`) + the auto-deploy pipeline | `winefornia` is a **personal User account**, not an organization. ❓ Who holds its password/2FA? | **Yes** — convert to a real organization with two owners |
| **GitHub Actions secret** | `FLY_API_TOKEN` (lets CI deploy) | Minted from haeinej's Fly account | **Yes** — must be re-issued after the Fly move |
| **Google Cloud** | Two projects: invoice chat app (`338702309220`) and tasting-room chat app (`275073979299` / `winefornia-tastingroom-499611`), each with its bot service account; plus the Gmail/Calendar/Drive service account with domain-wide delegation | ❓ Verify IAM owners of both projects | Add Cecil as Owner on both; ideally attach projects to the `innovatuswine.com` Workspace organization |
| **Google Workspace** | The winery mailbox (`contact@innovatuswine.com` mail, `INNOVATUS` label), Chat spaces, the domain-wide-delegation grants in Admin console | The winery (already company-owned) | No — this is the model everything else should match |
| **Square** | Production access token used for all invoices | ❓ Verify which Square login minted `SQUARE_PROD_ACCESS_TOKEN` | If minted from a personal developer login: re-mint from the winery's Square account and rotate |
| **Anthropic** | API key the server uses for extraction (`ANTHROPIC_API_KEY`) | ❓ Verify which console/org | Move to a winery Anthropic workspace; rotate |
| **Mem0** | Operator skill memory (`MEM0_API_KEY`) | ❓ Verify | Low stakes; rotate at leisure |
| **Squarespace** | The booking form and where it emails submissions | The winery site | No (verify form destination stays the watched mailbox) |

## 2. The migration principle

For each provider, "migrated" means **all four** of:

1. **Account/org ownership** — a winery-controlled account is Owner; no single personal account is a single point of failure.
2. **Billing** — charged to the winery, not a personal card.
3. **Credentials rotated** — the token the *server actually uses* was minted by the winery-owned account. Ownership transfer without rotation still leaves the old personal account able to mint valid credentials.
4. **Secrets updated** — new values set via `flyctl secrets set` (server) and GitHub → Settings → Secrets → Actions (CI). `app/config.py` lists most of what the server reads (a few vars — `GCHAT_VERIFY`, `GOOGLE_SERVICE_ACCOUNT_JSON_B64`, `GOOGLE_DELEGATED_USER_EMAIL`, `GOOGLE_TOKEN_JSON_B64_*` — are read directly in `app/main.py` / `services/gmail_service.py`).

## 3. Step-by-step

Recommended order (least-risky first). After **every** step: `curl https://winefornia-agent.fly.dev/health` must return 200, and after the Fly and GitHub steps, run one end-to-end test (a test booking form, or merge a trivial PR and watch it deploy).

### 3.1 Supabase (verification, likely small)

1. Dashboard → org `winefornia` → **Team**: Cecil's account = Owner; Haein = Administrator. Remove any stray members.
2. Org → Billing: winery card.
3. Nothing on the server changes — `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` / `POSTGRES_CONNECTION_STRING` are project-scoped and unaffected by team changes. (Optional hardening later: rotate the service key in Project Settings → API, then `flyctl secrets set SUPABASE_SERVICE_KEY=…`.)

### 3.2 GitHub (convert the personal account into an organization)

The `winefornia` account is a User. Two options:

- **Preferred:** GitHub Settings (of the `winefornia` account) → *Convert to organization*. Requires a separate personal account to become the org's first owner. Make **both** Cecil's and Haein's personal accounts org Owners.
- Alternative: create a fresh org and *Transfer* the repo into it.

Either way, afterwards:
1. Re-add the Actions secret `FLY_API_TOKEN` (transfers/conversions can drop secrets — verify before assuming).
2. Re-install the Claude GitHub app / Claude Code GitHub connection on the new org so claude.ai/code sessions still see the repo.
3. Update local remotes if the URL changed: `git remote set-url origin <new-url>`.
4. Re-check branch protection on `main` (require the **Test** check).

### 3.3 Google Cloud (IAM, no downtime)

1. In both GCP projects: IAM → add `cecil.park@winefornia.com` as **Owner**.
2. Ideally migrate both projects under the `innovatuswine.com` organization (GCP → *No organization* → migrate). Existing service-account keys keep working; nothing to rotate for this step.
3. The domain-wide-delegation grants live in Workspace Admin (already winery-controlled) — no change.

### 3.4 Fly.io (the actual server move)

Do this at a quiet hour; the app keeps its name, hostname, secrets, and machines, but expect a brief control-plane blip.

```
fly orgs create winefornia                 # from haeinej's account
fly orgs invite winefornia cecil.park@winefornia.com
fly apps move winefornia-agent --org winefornia
```

Then:
1. New org → Billing → winery card (the app will stop if the new org has no payment method — do this immediately).
2. Mint a fresh deploy token **scoped to the app**: `fly tokens create deploy -a winefornia-agent` → paste into GitHub secret `FLY_API_TOKEN`.
3. Revoke old personal tokens: `fly tokens list` / `fly tokens revoke <id>` from the personal account.
4. Verify: `fly status -a winefornia-agent`, `/health`, then merge any trivial PR and watch CI deploy end-to-end.

### 3.5 Square, Anthropic, Mem0 (rotate-and-swap, one at a time)

Same pattern for each: create the winery-owned account/workspace → mint a new key there → `flyctl secrets set KEY=…` (this restarts the machines — watch `/health`) → revoke the old key. Do them on different days so a regression is attributable.

## 4. Known loose ends (found during the 2026-07-13 audit)

- **The weekly Square→Supabase sync (`scripts/sync.py`) has no scheduler in this repo or on Fly.** It has been running Sundays 09:00 UTC from somewhere external (likely a personal machine — a single point of failure this guide exists to remove) and **did not run on 2026-07-12**. Recommended fix: a GitHub Actions scheduled workflow, or a third Fly process. Track down and disable whatever ran it before.
- **The invoice half of that sync is broken:** `sync_state` claims invoices synced successfully, but the `square_invoices` table has **0 rows** while Square itself shows real invoices. Needs a code fix; until then, "invoice history" features read from `invoice_logs` only.
- **`unresolved_reservation_events` (the review pile) is write-only today** — 80 rows and no process empties it. The planned morning-checkup agent should surface new entries.

## 5. Done-when checklist

- [ ] Two humans can each independently: log into Fly, Supabase dashboard, GitHub, both GCP projects.
- [ ] All billing on winery payment methods.
- [ ] `fly secrets list` names map 1:1 to keys minted by winery-owned accounts (Square, Anthropic, Mem0 rotated).
- [ ] GitHub `FLY_API_TOKEN` re-minted post-move; a PR merged after the move deployed successfully.
- [ ] Old personal tokens revoked (Fly, Square, Anthropic).
- [ ] The sync scheduler runs from shared infrastructure, not a personal machine.
