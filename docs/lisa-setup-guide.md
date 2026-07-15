# Lisa's Setup Guide — Working on the Agent from Your Own Computer

This guide is for a non-engineer. It explains, in plain language, how to get the
assistant's code onto your computer, how to ask Claude Code to make a change,
and how that change safely reaches the live system. You never need to touch
passwords, API keys, or the live server to do any of this.

## The one idea to hold onto

**You never edit the live system directly.** You edit a *copy* of the code on
your computer, the tests check your change against a pretend Square/Gmail/
database (no real money or email involved), and only after the change is
reviewed and merged does GitHub automatically put it live. If a change is bad,
the tests catch it *before* it deploys.

## One-time setup (about 30 minutes)

You need three tools and one permission.

1. **Get access to the code.** Ask to be added to the
   `winefornia/innovatus-agent` repository on GitHub (you'll need a free
   GitHub account).

2. **Install the tools** (on a Mac, in the Terminal app):
   - **git** — comes with Apple's command-line tools; running `git --version`
     will offer to install it.
   - **Python 3.13** — from https://www.python.org/downloads/
   - **Claude Code** — follow https://claude.com/claude-code (you'll sign in
     with a Claude account).

3. **Download the code and set it up.** In Terminal, paste these lines one at
   a time:

   ```bash
   git clone https://github.com/winefornia/innovatus-agent.git
   cd innovatus-agent
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Check everything works** by running the test suite:

   ```bash
   pytest -q
   ```

   You should see a stream of dots ending in something like `312 passed`.
   No keys or passwords are needed — the tests fake all the outside services.

That's it. You do **not** need a `.env` file, Fly access, or any API keys to
edit code and run tests.

## The everyday workflow

Every change, big or small, follows the same five steps:

1. **Open Claude Code in the project folder** and describe the problem or the
   change you want, in plain English. The `CLAUDE.md` file in this folder
   already teaches Claude how the whole system works, the safety rules, and
   where to look for each kind of problem — you don't need to explain any of
   that.
2. **Claude makes the change on a branch** (a separate copy, so the main code
   is untouched) and runs `pytest -q` to prove nothing broke.
3. **Claude pushes the branch and opens a pull request (PR)** on GitHub. This
   is the "here's my proposed change" step. GitHub runs the same tests again,
   independently.
4. **Merge the PR** (a green "Merge" button on the GitHub page, only clickable
   once tests pass). Merging automatically deploys the new code to the live
   server — there is nothing else to run.
5. **Check it's healthy.** Open https://winefornia-agent.fly.dev/health in a
   browser. If it says `"status": "ok"`, the live system took the change.

Ask Claude to do steps 2–3 and the health check for you — you only need to
describe the change and click Merge.

## Example: a real change, start to finish

Say clients keep replying to the tasting-room confirmation email asking for
the winery's street address, and you want it added to the confirmation.

**You type into Claude Code:**

> The tasting-room confirmation email we send to clients should include our
> address at the bottom: 1234 Soda Canyon Rd, Napa, CA 94558. Can you add
> that, run the tests, and open a PR?

**What Claude should do (and will narrate as it goes):**

1. Find where the confirmation email text lives (in the tasting-room pipeline,
   under `services/`), and add the address line.
2. Update any test that checks the email wording, and run `pytest -q`.
3. Create a branch, commit, push, and open a PR titled something like
   "Add winery address to tasting confirmation email".

**What you do:**

1. Read Claude's summary. Sanity-check it names the *tasting-room* pipeline
   (not the invoice one) and doesn't mention `db/schema.sql` — see the safety
   notes below.
2. Open the PR link, wait for the green check, click **Merge**.
3. Check https://winefornia-agent.fly.dev/health says ok. Since this change
   touches an email clients receive, also do one test booking and read the
   confirmation that comes back.

Total hands-on time for you: a few minutes.

## Safety notes — when to slow down

Claude knows these rules (they're in `CLAUDE.md`), but you are the second pair
of eyes:

- **If Claude says the change touches `db/schema.sql`**: stop. Database
  changes must be applied to Supabase *by hand* before or with the deploy —
  merging alone is not enough, and skipping this once cost us a booking in
  July 2026. Ask Claude to spell out the exact SQL to run and where.
- **If the change touches money or outbound email** (`square_service.py`,
  `gmail_service.py`, `tool_registry.py`): ask Claude to state plainly what
  could go wrong and what to check after deploy, and do that check.
- **Never paste API keys or passwords into the code or the chat.** Secrets
  live only in Fly and GitHub settings. If you see one in a file, say so.

## What this setup does NOT cover

- **Restarting the server or reading live logs** — needs a Fly.io account
  with access to the `winefornia-agent` app (`flyctl logs`,
  `flyctl machine restart`).
- **Looking at live data or applying database changes** — needs a login to
  the Supabase dashboard (project `zlbixpklvejcuxifqzjk`).
- Neither is required for code changes. Getting these accounts (plus GitHub,
  GCP, and Square) under winery control is covered in
  [ownership-and-migration.md](ownership-and-migration.md).

## If something on the live system looks wrong

You usually don't need to touch code at all — start with the symptom table in
`CLAUDE.md` ("When something breaks — where to look"), or just describe the
symptom to Claude Code and let it investigate first. A missing booking, for
example, is usually a data question (did the email arrive? was it quarantined?)
before it's a code question.
