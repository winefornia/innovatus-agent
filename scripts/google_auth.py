"""Re-run Google OAuth to get Google Workspace tokens.

Run this locally (one time) then deploy the token to Fly.io:

    python scripts/google_auth.py --account lisa@innovatuswine.com
    flyctl secrets set GMAIL_TOKEN_JSON_B64=$(base64 -i token.json)

Uses the OAuth credentials from the innovatus api project.
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

CREDS_FILE = Path("/Users/jeonghaein/Desktop/2026 DT/innovatus api/googledrive.json")
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_TOKEN_FILE = PROJECT_ROOT / "token.json"
AUTHORIZED_ACCOUNTS = (
    "lisa@innovatuswine.com",
    "cecil.park@winefornia.com",
    "haeinej@gmail.com",
)

SCOPES = [
    "https://mail.google.com/",                       # full Gmail read/send/modify/delete
    "https://www.googleapis.com/auth/calendar",       # full Google Calendar access
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",    # list & read Chat spaces
    "https://www.googleapis.com/auth/chat.messages.readonly",  # read Chat messages
]


def _token_file_for(account: str | None) -> Path:
    if not account:
        return DEFAULT_TOKEN_FILE
    safe = account.lower().replace("@", "-").replace(".", "-")
    return PROJECT_ROOT / f"token-{safe}.json"


def _secret_name_for(account: str | None) -> str:
    if not account:
        return "GMAIL_TOKEN_JSON_B64"
    safe = account.upper().replace("@", "_").replace(".", "_").replace("-", "_")
    return f"GOOGLE_TOKEN_JSON_B64_{safe}"


def main():
    parser = argparse.ArgumentParser(description="Create a Google OAuth token for Gmail and Calendar.")
    parser.add_argument(
        "--account",
        choices=AUTHORIZED_ACCOUNTS,
        help="Google account to authorize. Use one of the approved Winefornia accounts.",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="Optional output path. Defaults to token.json or token-<account>.json.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8099,
        help="Local callback port for the OAuth flow.",
    )
    args = parser.parse_args()

    if not CREDS_FILE.exists():
        sys.exit(f"ERROR: credentials file not found: {CREDS_FILE}")

    from google_auth_oauthlib.flow import InstalledAppFlow

    token_file = args.token_file or _token_file_for(args.account)
    secret_name = _secret_name_for(args.account)

    print(f"Using credentials: {CREDS_FILE}")
    if args.account:
        print(f"Authorizing account: {args.account}")
    print(f"Requesting scopes: {SCOPES}")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
    auth_kwargs = {"prompt": "consent"}
    if args.account:
        auth_kwargs["login_hint"] = args.account

    creds = flow.run_local_server(port=args.port, **auth_kwargs)

    token_file.write_text(creds.to_json())
    print(f"\nToken saved to: {token_file}")

    b64 = base64.b64encode(token_file.read_bytes()).decode()
    print("\n--- Deploy to Fly.io ---")
    print("Run this command:")
    print(f'  flyctl secrets set {secret_name}="{b64}"')
    print()
    print("Or from the token file:")
    print(f"  flyctl secrets set {secret_name}=$(base64 -i {token_file})")


if __name__ == "__main__":
    main()
