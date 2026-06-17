import base64
import importlib
import json


def _reload_gmail(monkeypatch, **env):
    keys = [
        "GOOGLE_SERVICE_ACCOUNT_JSON_B64",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_DELEGATED_USER_EMAIL",
        "GOOGLE_ACCOUNT_EMAIL",
        "GMAIL_TOKEN_JSON_B64",
        "GMAIL_TOKEN_JSON",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import services.gmail_service as gmail_service

    gmail_service = importlib.reload(gmail_service)
    gmail_service._service = None
    return gmail_service


def _encode_sa(**fields):
    return base64.b64encode(json.dumps(fields).encode()).decode()


def test_service_account_creds_built_with_delegated_subject(monkeypatch, mocker):
    """SA JSON + delegated user → creds built with the delegated user as subject."""
    encoded = _encode_sa(client_email="agent@example.iam.gserviceaccount.com")

    gmail_service = _reload_gmail(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON_B64=encoded,
        GOOGLE_DELEGATED_USER_EMAIL="lisa@winefornia.com",
    )

    sentinel = object()
    from_info = mocker.patch.object(
        gmail_service.service_account.Credentials,
        "from_service_account_info",
        return_value=sentinel,
    )

    creds = gmail_service._get_service_account_creds()

    assert creds is sentinel
    _, kwargs = from_info.call_args
    assert kwargs["subject"] == "lisa@winefornia.com"


def test_service_account_returns_none_without_delegated_user(monkeypatch):
    """SA JSON present but no delegated user → None (DWD requires a subject)."""
    encoded = _encode_sa(client_email="agent@example.iam.gserviceaccount.com")

    gmail_service = _reload_gmail(monkeypatch, GOOGLE_SERVICE_ACCOUNT_JSON_B64=encoded)

    assert gmail_service._get_service_account_creds() is None


def test_service_account_returns_none_without_sa_json(monkeypatch):
    """No SA JSON → None, so _get_service falls back to OAuth token."""
    gmail_service = _reload_gmail(
        monkeypatch, GOOGLE_DELEGATED_USER_EMAIL="lisa@winefornia.com"
    )

    assert gmail_service._get_service_account_creds() is None
