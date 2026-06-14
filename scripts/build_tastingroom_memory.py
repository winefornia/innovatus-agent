"""Build tasting room case memory from historical Gmail form-submission threads.

This script reads recent Gmail messages matching the winery visit form and
replays their full threads through the tastingroom graph. The DB timeline
becomes the memory: reservations, events, claims, and thread IDs.

Default is dry-run. Pass --apply to write to Supabase.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_APPROVAL_CHAT_ID", "")
os.environ.setdefault("POSTGRES_CONNECTION_STRING", "")

from agents.case_desk_graph import case_desk_graph
from services.gmail_service import _decode_body, _get_service
import services.tastingroom_service as tastingroom_service


def _headers(msg: dict) -> dict[str, str]:
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _thread_messages(service, thread_id: str) -> list[dict]:
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    return thread.get("messages", [])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default='subject:"Form Submission - Wine tasting Booking" newer:2026/5/1')
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    if args.no_llm:
        tastingroom_service.llm_extract_email = lambda *a, **k: {}
        import services.case_judge as case_judge
        case_judge.judge_case = lambda **k: case_judge._fallback_judgment("no-llm mode")

    try:
        service = _get_service()
    except Exception as exc:
        raise SystemExit(
            "Gmail auth failed. Set GMAIL_TOKEN_FILE to Lisa's token or provide "
            f"a valid GMAIL_TOKEN_JSON_B64. Details: {exc}"
        ) from exc
    found = service.users().messages().list(userId="me", q=args.query, maxResults=args.limit).execute().get("messages", [])
    thread_ids = []
    for msg in found:
        meta = service.users().messages().get(userId="me", id=msg["id"], format="metadata").execute()
        tid = meta.get("threadId")
        if tid and tid not in thread_ids:
            thread_ids.append(tid)

    print(f"threads {len(thread_ids)}")
    for thread_id in thread_ids:
        messages = _thread_messages(service, thread_id)
        print(f"THREAD {thread_id} messages={len(messages)}")
        for msg in messages:
            hs = _headers(msg)
            body = _decode_body(msg.get("payload", {}))
            subject = hs.get("Subject", "")
            sender = hs.get("From", "")
            to_email = hs.get("To", "")
            if not args.apply:
                msg_type = tastingroom_service.classify_email(subject, sender, body)
                facts = tastingroom_service.extract_email_facts(subject, sender, body, msg_type)
                print(" ", msg["id"], msg_type, facts.get("client_name"), facts.get("requested_date"), facts.get("guest_count"))
                continue
            raw_email = f"Subject: {subject}\nFrom: {sender}\nTo: {to_email}\n\n{body}"
            out = case_desk_graph.invoke(
                {
                    "raw_email": raw_email,
                    "sender_id": sender,
                    "subject": subject,
                    "from_email": sender,
                    "to_email": to_email,
                    "body": body,
                    "gmail_message_id": msg["id"],
                    "gmail_thread_id": thread_id,
                    "disable_actions": True,
                },
                config={"configurable": {"thread_id": f"history-{msg['id']}"}},
            )
            print(
                " ",
                msg["id"],
                out.get("message_type"),
                out.get("reservation_id"),
                out.get("current_state"),
                out.get("recommended_action"),
            )


if __name__ == "__main__":
    main()
