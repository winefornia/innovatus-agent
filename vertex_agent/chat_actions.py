"""Write-capable tools for the tasting-room Google Chat assistant.

The conversational assistant (vertex_agent.chat_agent) is mostly read-only, but
staff also want to DRIVE a case from chat — send/revise the drafted email, mark
payment, hand a case to a human, end/remove a case, or revoke a decision —
instead of hunting for the right approval card.

Every one of these routes through the SAME service primitives the card buttons
use (services.tastingroom_service.process_action_decision + the repository), so
chat is just an alternate control surface, not a second code path. Authorization
is already enforced upstream: the Google Chat adapter only reaches discuss() for
allow-listed approvers (Cecil/Lisa), so these tools inherit that gate.

Safety model — CONFIRM FIRST for anything that touches the outside world
(sending an email, ending/removing a case, revoking a decision). Because each
chat turn runs a fresh, memory-less agent, confirmation is held server-side in a
small per-user pending store and re-injected into the next message by
chat_agent.discuss(). The staging tools (stage_*) record the intent and return a
one-line "reply yes to confirm" question; the actual mutation only happens when
confirm_pending_action() fires on the user's affirming reply.

Reversible / internal steps (revise a not-yet-sent draft, mark invoice sent, mark
paid, hand to a human) execute immediately — there is nothing to un-send.
"""

from __future__ import annotations

import contextvars
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Set by chat_agent.discuss() at the start of each turn so the tools know which
# allow-listed approver is acting (used for the audit trail and to key the
# pending-confirmation store). Contextvars copy into the asyncio task the ADK
# runner spawns, so tool calls within one turn see the right user.
_CURRENT_USER: "contextvars.ContextVar[str]" = contextvars.ContextVar("tr_chat_user", default="")

# user -> {"kind": str, "params": dict, "summary": str, "ts": float}
# In-memory cache + fallback. The durable source of truth is the
# chat_pending_actions Supabase table (so a staged action survives a web restart);
# this dict mirrors it and is used when the DB is unavailable (e.g. unit tests).
_PENDING: dict[str, dict[str, Any]] = {}
_PENDING_TTL = 600  # seconds; a staged-but-unconfirmed action expires after 10 min


def set_current_user(user: str) -> None:
    _CURRENT_USER.set(user or "")


def _user() -> str:
    return _CURRENT_USER.get() or "gchat_unknown"


# ── durable backing (best-effort; degrades to in-memory) ─────────────────────

def _db_get(user: str) -> dict | None:
    """Read a user's pending action from Supabase, shaped like a _PENDING entry.
    Returns None on a miss OR any DB problem (caller falls back to memory)."""
    try:
        from db.repository import get_chat_pending

        row = get_chat_pending(user)
        if not row:
            return None
        from datetime import datetime, timezone

        created = str(row.get("created_at") or "").replace("Z", "+00:00")
        ts = datetime.fromisoformat(created).timestamp() if created else time.time()
        return {"kind": row["kind"], "params": row.get("params") or {},
                "summary": row.get("summary") or "", "ts": ts}
    except Exception as exc:
        log.debug("[tr:chat-actions] pending DB read unavailable: %s", exc)
        return None


def _db_put(user: str, kind: str, params: dict, summary: str) -> None:
    try:
        from db.repository import upsert_chat_pending

        upsert_chat_pending(user, kind, params, summary)
    except Exception as exc:
        log.debug("[tr:chat-actions] pending DB write unavailable: %s", exc)


def _db_del(user: str) -> None:
    try:
        from db.repository import delete_chat_pending

        delete_chat_pending(user)
    except Exception as exc:
        log.debug("[tr:chat-actions] pending DB delete unavailable: %s", exc)


# ── pending-confirmation store ───────────────────────────────────────────────

def peek_pending(user: str) -> dict | None:
    """Return the live (non-expired) pending action for a user, or None.

    Reads the durable store first (survives restarts), falling back to the
    in-memory mirror. Called by discuss() to decide whether to inject a
    confirmation note.
    """
    user = user or ""
    entry = _db_get(user) or _PENDING.get(user)
    if not entry:
        return None
    if time.time() - entry["ts"] > _PENDING_TTL:
        _PENDING.pop(user, None)
        _db_del(user)
        return None
    _PENDING[user] = entry  # keep the in-memory mirror warm
    return entry


def _stage(kind: str, params: dict, summary: str) -> str:
    user = _user()
    _PENDING[user] = {"kind": kind, "params": params, "summary": summary, "ts": time.time()}
    _db_put(user, kind, params, summary)
    return summary


def confirm_pending_action() -> str:
    """Execute the action the user previously staged and just confirmed.

    Call this ONLY when the user's latest message affirms a pending action
    (e.g. "yes", "send it", "go ahead", "do it"). It performs the real
    mutation — sending the email, cancelling the case, or revoking the decision.
    """
    entry = peek_pending(_user())
    if not entry:
        return "There's nothing waiting for confirmation right now."
    _PENDING.pop(_user(), None)
    _db_del(_user())
    kind, params = entry["kind"], entry["params"]
    try:
        if kind == "send_email":
            return _execute_send_email(params["action_id"])
        if kind == "cancel_case":
            return _execute_cancel_case(params["reservation_id"])
        if kind == "revoke":
            return _execute_revoke(params["reservation_id"])
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[tr:chat-actions] confirm failed (%s): %s", kind, exc)
        return f"That didn't go through — {exc}"
    return "I'm not sure what I was confirming — try again."


def cancel_pending_action() -> str:
    """Discard the staged action when the user declines (e.g. "no", "never mind")."""
    had = peek_pending(_user())
    _PENDING.pop(_user(), None)
    _db_del(_user())
    return "Okay — left it as is." if had else "Nothing was staged, so nothing changed."


# ── resolution helpers ───────────────────────────────────────────────────────

def _resolve(query: str) -> dict:
    """Resolve a name / case-id to one reservation row.

    Returns {"reservation": row} on a unique match, {"error": msg} when nothing
    matches, or {"ambiguous": [...]} when several do (so the tool can ask which).
    """
    from db.repository import get_reservation
    from vertex_agent.chat_agent import find_cases

    q = (query or "").strip()
    if not q:
        return {"error": "Which case? Give me a client name or a case id."}
    if q.upper().startswith("TASTING-"):
        row = get_reservation(q.upper())
        return {"reservation": row} if row else {"error": f"No case {q.upper()}."}
    rows = find_cases(q)
    if not rows:
        return {"error": f"I couldn't find a case matching \"{q}\"."}
    if len(rows) > 1:
        return {"ambiguous": rows}
    row = get_reservation(rows[0]["reservation_id"]) or rows[0]
    return {"reservation": row}


def _ambiguous_msg(rows: list[dict]) -> str:
    names = ", ".join(
        f"{r.get('client_name') or '(no name)'} ({r['reservation_id']})" for r in rows[:6]
    )
    return f"I found a few — which one? {names}"


def _latest_pending(reservation_id: str, preferred_type: str | None = None) -> dict | None:
    from services.tastingroom_chat_service import _latest_pending_action

    return _latest_pending_action(reservation_id, preferred_type)


def _case_label(row: dict) -> str:
    name = row.get("client_name") or "(no name yet)"
    return f"{name} ({row['reservation_id']})"


# ── tools: send / revise an email ────────────────────────────────────────────

def stage_send_email(case: str) -> str:
    """Stage sending the email currently drafted for a case, then ask the user to
    confirm. Does NOT send yet — sending happens on their "yes" via
    confirm_pending_action(). Use this when staff say things like "send the Josh
    email for Mira", "send Audrey the offer", or "send the confirmation".

    Args:
        case: a client name or case id (e.g. "Mira" or "TASTING-...").
    """
    res = _resolve(case)
    if res.get("error"):
        return res["error"]
    if res.get("ambiguous"):
        return _ambiguous_msg(res["ambiguous"])
    row = res["reservation"]
    action = _latest_pending(row["reservation_id"])
    if not action:
        return f"There's no email waiting to send for {_case_label(row)} right now."
    if not action.get("recipient_email"):
        return (
            f"The pending step for {_case_label(row)} is an internal one "
            f"(\"{(action.get('action_type') or '').replace('_', ' ')}\"), not an email to send."
        )
    recipient = action.get("recipient_email")
    subject = action.get("email_subject") or ""
    summary = (
        f"Ready to send to {recipient} for {_case_label(row)}:\n"
        f"*{subject}*\n\nReply *yes* to send, or tell me what to change."
    )
    return _stage("send_email", {"action_id": action["action_id"]}, summary)


def _execute_send_email(action_id: str) -> str:
    from services.tastingroom_service import process_action_decision

    result = process_action_decision(action_id, "approve", decided_by=_user())
    if not result.get("ok"):
        return f"That didn't send — {result.get('error')}"
    msg = "Sent ✅"
    if result.get("next_action_id"):
        msg += " The next step is queued — its card will show up here."
    return msg


def revise_draft(case: str, instruction: str) -> str:
    """Revise the pending email draft for a case per an instruction (e.g. "make it
    warmer", "mention parking"). This edits the not-yet-sent draft and shows the
    new version — it does NOT send, so it runs immediately. Afterwards the user
    can say "send it" to send the revised draft.

    Args:
        case: a client name or case id.
        instruction: how to change the draft.
    """
    res = _resolve(case)
    if res.get("error"):
        return res["error"]
    if res.get("ambiguous"):
        return _ambiguous_msg(res["ambiguous"])
    row = res["reservation"]
    action = _latest_pending(row["reservation_id"])
    if not action:
        return f"There's no draft to edit for {_case_label(row)} right now."
    if not action.get("recipient_email"):
        return f"That pending step for {_case_label(row)} is internal — there's no email draft to edit."

    from db.repository import update_reservation_action
    from services.tastingroom_chat_service import _revise_email_with_llm

    revised = _revise_email_with_llm(action, row, instruction)
    update_reservation_action(
        action["action_id"],
        email_subject=revised["subject"],
        email_body=revised["body"],
        recommendation=(
            f"Revised email for {row.get('client_name') or 'reservation'}\n\n"
            f"To: {action.get('recipient_email')}\n"
            f"Subject: {revised['subject']}\n\n{revised['body'][:1600]}"
        ),
    )
    return (
        f"Updated the draft for {_case_label(row)}:\n"
        f"*{revised['subject']}*\n\n{revised['body'][:1000]}\n\n"
        f"Say *send it* when you're happy with it."
    )


# ── tools: payment markers (immediate, reversible internal state) ─────────────

def mark_invoice_sent(case: str) -> str:
    """Record that the Square invoice has been sent for a case. Immediate.

    Args:
        case: a client name or case id.
    """
    return _payment_decision(case, "invoice_sent", "Marked the invoice as sent")


def mark_paid(case: str) -> str:
    """Record that a case has been paid. Immediate. This also queues the final
    confirmation email as a draft, which you can then send.

    Args:
        case: a client name or case id.
    """
    return _payment_decision(case, "paid", "Marked as paid")


def _payment_decision(case: str, decision: str, ok_label: str) -> str:
    res = _resolve(case)
    if res.get("error"):
        return res["error"]
    if res.get("ambiguous"):
        return _ambiguous_msg(res["ambiguous"])
    row = res["reservation"]
    action = _latest_pending(row["reservation_id"], preferred_type="review_payment_status")
    if not action:
        return (
            f"There's no payment step open for {_case_label(row)} yet — "
            f"that usually appears after the invoice email goes out."
        )
    from services.tastingroom_service import process_action_decision

    result = process_action_decision(action["action_id"], decision, decided_by=_user())
    if not result.get("ok"):
        return f"That didn't work — {result.get('error')}"
    msg = f"{ok_label} for {_case_label(row)}."
    if result.get("next_action_id"):
        msg += " The next step is drafted — say *send it* when you're ready."
    return msg


# ── tool: hand to a human (immediate) ────────────────────────────────────────

def manual_handle(case: str) -> str:
    """Hand a case off for a human to handle manually — flags it for review and
    stops the assistant from auto-advancing it. Immediate.

    Args:
        case: a client name or case id.
    """
    res = _resolve(case)
    if res.get("error"):
        return res["error"]
    if res.get("ambiguous"):
        return _ambiguous_msg(res["ambiguous"])
    row = res["reservation"]
    action = _latest_pending(row["reservation_id"])
    if action:
        from services.tastingroom_service import process_action_decision

        result = process_action_decision(action["action_id"], "escalate", decided_by=_user())
        if not result.get("ok"):
            return f"That didn't work — {result.get('error')}"
        return f"Flagged {_case_label(row)} for you to handle manually."

    # No pending card — just mark the case itself for human review.
    from db.repository import insert_reservation_event, update_reservation
    from db.models import ReservationEvent

    update_reservation(
        row["reservation_id"],
        current_state="HUMAN_REVIEW_REQUIRED",
        recommended_action="escalate",
    )
    insert_reservation_event(ReservationEvent(
        reservation_id=row["reservation_id"],
        event_type="manual_handling_requested",
        actor=_user(),
        source_channel="google_chat",
        summary="Handed to a human via chat",
    ))
    return f"Flagged {_case_label(row)} for you to handle manually."


# ── tools: end / remove a case (confirm-first, soft-cancel) ───────────────────

def stage_cancel_case(case: str) -> str:
    """Stage ending/removing a case (soft-cancel), then ask the user to confirm.
    Use for "end this case", "remove the test case", "cancel Mira". Nothing is
    deleted until the user confirms; even then the record is kept and can be
    revoked/reopened later.

    Args:
        case: a client name or case id.
    """
    res = _resolve(case)
    if res.get("error"):
        return res["error"]
    if res.get("ambiguous"):
        return _ambiguous_msg(res["ambiguous"])
    row = res["reservation"]
    if (row.get("current_state") or "") == "CANCELLED_OR_DEFERRED":
        return f"{_case_label(row)} is already closed."
    summary = (
        f"End and remove {_case_label(row)} from the open board? "
        f"It keeps its history and you can reopen it later.\nReply *yes* to confirm."
    )
    return _stage("cancel_case", {"reservation_id": row["reservation_id"]}, summary)


def _execute_cancel_case(reservation_id: str) -> str:
    from db.models import ReservationEvent
    from db.repository import get_reservation, insert_reservation_event, update_reservation

    update_reservation(
        reservation_id,
        current_state="CANCELLED_OR_DEFERRED",
        recommended_action=None,
    )
    # Drop any pending approval cards so they don't linger.
    _reject_pending_actions(reservation_id)
    insert_reservation_event(ReservationEvent(
        reservation_id=reservation_id,
        event_type="case_cancelled",
        actor=_user(),
        source_channel="google_chat",
        summary="Case ended/removed via chat (soft-cancel)",
    ))
    row = get_reservation(reservation_id) or {"reservation_id": reservation_id}
    return f"Done — {_case_label(row)} is off the board. Say \"revoke {reservation_id}\" to reopen it."


def _reject_pending_actions(reservation_id: str) -> None:
    from datetime import datetime

    from db.repository import _get_client, update_reservation_action

    try:
        client = _get_client()
        rows = (
            client.table("reservation_action_requests")
            .select("action_id")
            .eq("reservation_id", reservation_id)
            .eq("status", "pending")
            .execute()
            .data
        ) or []
        for r in rows:
            update_reservation_action(
                r["action_id"], status="rejected",
                decided_by=_user(), decided_at=datetime.utcnow().isoformat(),
            )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[tr:chat-actions] could not clear pending actions for %s: %s", reservation_id, exc)


# ── tool: revoke a decision (confirm-first, reopen) ───────────────────────────

def stage_revoke_decision(case: str) -> str:
    """Stage revoking the last decision on a case — reopens it for review — then
    ask the user to confirm. Use for "revoke that", "undo the cancel", "reopen
    Mira". Note: it can reopen a case and undo a cancel, but it can't un-send an
    email that already went out.

    Args:
        case: a client name or case id.
    """
    res = _resolve(case)
    if res.get("error"):
        return res["error"]
    if res.get("ambiguous"):
        return _ambiguous_msg(res["ambiguous"])
    row = res["reservation"]
    summary = (
        f"Revoke the last decision on {_case_label(row)} and reopen it for review? "
        f"(I can't recall an email that already went out.)\nReply *yes* to confirm."
    )
    return _stage("revoke", {"reservation_id": row["reservation_id"]}, summary)


def _execute_revoke(reservation_id: str) -> str:
    from db.models import ReservationEvent
    from db.repository import get_reservation, insert_reservation_event, update_reservation

    update_reservation(
        reservation_id,
        current_state="HUMAN_REVIEW_REQUIRED",
        recommended_action="escalate",
    )
    insert_reservation_event(ReservationEvent(
        reservation_id=reservation_id,
        event_type="decision_revoked",
        actor=_user(),
        source_channel="google_chat",
        summary="Last decision revoked via chat; case reopened for review",
    ))
    row = get_reservation(reservation_id) or {"reservation_id": reservation_id}
    return (
        f"Reopened {_case_label(row)} for review. It's back on your plate — "
        f"tell me the next step or hand it to a human."
    )


# Tools exposed to the ADK agent (read-the-docstring order = how staff think).
WRITE_TOOLS = [
    stage_send_email,
    revise_draft,
    mark_invoice_sent,
    mark_paid,
    manual_handle,
    stage_cancel_case,
    stage_revoke_decision,
    confirm_pending_action,
    cancel_pending_action,
]
