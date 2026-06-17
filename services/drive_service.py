"""Google Drive file download for chat-attached / linked PDFs.

When staff attach a PDF in Google Chat it can arrive one of two ways:
  - an uploaded Chat file  → attachmentDataRef.resourceName (Chat media API, handled
    in the adapter)
  - a Google Drive file    → driveDataRef.driveFileId, OR just a pasted Drive link
    like https://drive.google.com/open?id=<ID>

This module handles the Drive case. It authenticates exactly like gmail_service —
service account + domain-wide delegation (GOOGLE_SERVICE_ACCOUNT_JSON_B64 +
GOOGLE_DELEGATED_USER_EMAIL) — so it impersonates the workspace user (who owns the
file) and reads it via the Drive API. That delegation must have the
drive.readonly scope authorized for the SA's client ID in the Workspace admin
console (Security → API controls → Domain-wide delegation); without it the token
mints fine but the download returns 403.

Everything is best-effort and defensive: any failure returns None / [] so the
caller falls back to "paste the order text instead."
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)

_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Matches the Drive file id in the common link shapes:
#   /file/d/<ID>/view   ?id=<ID>   &id=<ID>   /open?id=<ID>   /uc?id=<ID>
#   /document/d/<ID>     /spreadsheets/d/<ID>  (native docs — download still tried)
_LINK_RE = re.compile(
    r"(?:/d/|[?&]id=|/open\?id=|/uc\?id=)([A-Za-z0-9_-]{20,})"
)


def extract_drive_file_ids(text: str) -> list[str]:
    """Return the Drive file ids referenced by any Drive links in `text` (deduped,
    order-preserving). Empty list if none."""
    if not text or "drive.google.com" not in text and "docs.google.com" not in text:
        return []
    seen: dict[str, None] = {}
    for m in _LINK_RE.finditer(text):
        seen.setdefault(m.group(1), None)
    return list(seen.keys())


def _impersonation_subject(as_user: str = "") -> str | None:
    """Who to impersonate via domain-wide delegation.

    Prefer the file owner (the Chat sender's email) so we read files THEY own —
    impersonating a fixed mailbox that doesn't have the file returns 404. Fall back
    to GOOGLE_DELEGATED_USER_EMAIL when no sender email is available.
    """
    if as_user and "@" in as_user:
        return as_user.strip()
    fallback = os.environ.get("GOOGLE_DELEGATED_USER_EMAIL")
    return fallback or None


def _delegated_creds(as_user: str = ""):
    """Service account + domain-wide delegation creds scoped for Drive read, or None.

    Impersonates `as_user` (the file owner) when given, else GOOGLE_DELEGATED_USER_EMAIL.
    """
    sa_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    subject = _impersonation_subject(as_user)
    if not sa_b64 or not subject:
        log.warning("[drive] no SA delegation configured "
                    "(need GOOGLE_SERVICE_ACCOUNT_JSON_B64 + a subject to impersonate)")
        return None
    try:
        from google.oauth2 import service_account
        sa_info = json.loads(base64.b64decode(sa_b64).decode())
        return service_account.Credentials.from_service_account_info(
            sa_info, scopes=_DRIVE_SCOPES, subject=subject
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[drive] could not build delegated creds: %s", exc)
        return None


def _access_token(as_user: str = "") -> str | None:
    creds = _delegated_creds(as_user)
    if not creds:
        return None
    try:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        return creds.token
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[drive] token refresh failed: %s", exc)
        return None


def download_drive_file(file_id: str, as_user: str = "") -> bytes | None:
    """Download a Drive file's bytes (binary, e.g. an uploaded PDF). None on failure.

    `as_user` is the file owner's email (the Chat sender) — we impersonate them so
    we can read files they own. Uses files.get?alt=media, which works for
    uploaded/binary files. Native Google Docs/Sheets aren't binary and would error
    here — those aren't order PDFs, so we return None and let the caller ask for text.
    """
    if not file_id:
        return None
    subject = _impersonation_subject(as_user)
    token = _access_token(as_user)
    if not token:
        return None
    log.info("[drive] downloading %s as %s", file_id, subject)
    url = (
        f"https://www.googleapis.com/drive/v3/files/{file_id}"
        "?alt=media&supportsAllDrives=true"
    )
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            return r.content
        if r.status_code == 404:
            log.warning("[drive] 404 downloading %s as %s — that user can't see the file "
                        "(wrong impersonation target, or the file isn't shared with them)",
                        file_id, subject)
        elif r.status_code == 403:
            log.warning("[drive] 403 downloading %s — the SA's domain-wide delegation "
                        "likely lacks the drive.readonly scope (authorize it in the "
                        "Workspace admin console)", file_id)
        else:
            log.warning("[drive] download %s failed: %s %s", file_id, r.status_code, r.text[:200])
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[drive] download error for %s: %s", file_id, exc)
    return None
