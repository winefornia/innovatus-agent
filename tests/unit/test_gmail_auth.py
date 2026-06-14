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


def test_auth_status_prefers_domain_wide_delegation(monkeypatch):
    service_account_info = {
        "client_email": "agent@example.iam.gserviceaccount.com",
        "client_id": "123456789",
    }
    encoded = base64.b64encode(json.dumps(service_account_info).encode()).decode()

    gmail_service = _reload_gmail(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON_B64=encoded,
        GOOGLE_DELEGATED_USER_EMAIL="lisa@winefornia.com",
    )

    status = gmail_service.get_auth_status()

    assert status["mode"] == "domain_wide_delegation"
    assert status["delegated_user"] == "lisa@winefornia.com"
    assert status["service_account_email"] == "agent@example.iam.gserviceaccount.com"
    assert status["service_account_client_id"] == "123456789"


def test_service_account_requires_delegated_user(monkeypatch):
    service_account_info = {"client_email": "agent@example.iam.gserviceaccount.com"}
    encoded = base64.b64encode(json.dumps(service_account_info).encode()).decode()

    gmail_service = _reload_gmail(monkeypatch, GOOGLE_SERVICE_ACCOUNT_JSON_B64=encoded)

    try:
        gmail_service._service_account_credentials()
    except RuntimeError as exc:
        assert "delegated user" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_auth_status_falls_back_to_user_oauth(monkeypatch):
    gmail_service = _reload_gmail(monkeypatch, GOOGLE_ACCOUNT_EMAIL="lisa@winefornia.com")

    status = gmail_service.get_auth_status()

    assert status["mode"] == "user_oauth"
    assert status["delegated_user"] == "lisa@winefornia.com"
