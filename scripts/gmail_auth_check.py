"""Verify Gmail auth works — service account or OAuth token.

Usage:
    # Test service account delegation (reads env vars)
    python scripts/gmail_auth_check.py

    # Test with a specific service account JSON file
    python scripts/gmail_auth_check.py --sa-file /path/to/service-account.json --user lisa@winefornia.com
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SCOPES = ["https://mail.google.com/"]


def check_service_account(sa_info: dict, user_email: str) -> bool:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES, subject=user_email
    )
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    print(f"  Email: {profile['emailAddress']}")
    print(f"  Messages: {profile.get('messagesTotal', '?')}")
    print(f"  Threads: {profile.get('threadsTotal', '?')}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Verify Gmail auth.")
    parser.add_argument("--sa-file", type=Path, help="Service account JSON file")
    parser.add_argument("--user", type=str, help="Delegated user email")
    args = parser.parse_args()

    # Service account check
    sa_info = None
    user_email = args.user or os.environ.get("GOOGLE_DELEGATED_USER_EMAIL", "")

    if args.sa_file:
        sa_info = json.loads(args.sa_file.read_text())
    else:
        sa_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
        if sa_b64:
            sa_info = json.loads(base64.b64decode(sa_b64).decode())

    if sa_info and user_email:
        print(f"Testing service account delegation as {user_email}...")
        try:
            check_service_account(sa_info, user_email)
            print("SUCCESS: Service account auth works.")
            return
        except Exception as e:
            print(f"FAILED: {e}")
            sys.exit(1)

    # OAuth token fallback
    print("No service account config found. Testing OAuth token...")
    try:
        from services.gmail_service import _get_service
        svc = _get_service()
        profile = svc.users().getProfile(userId="me").execute()
        print(f"  Email: {profile['emailAddress']}")
        print("SUCCESS: OAuth token auth works.")
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
