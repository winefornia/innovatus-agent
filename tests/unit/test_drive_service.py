"""Tests for services.drive_service — Drive link parsing + defensive download."""

import services.drive_service as ds


class TestExtractDriveFileIds:
    def test_open_id_link(self):
        assert ds.extract_drive_file_ids(
            "order: https://drive.google.com/open?id=11ATmxJK8BFvSAhbfzpoO-gy0Ckhmmfd-"
        ) == ["11ATmxJK8BFvSAhbfzpoO-gy0Ckhmmfd-"]

    def test_file_d_link(self):
        assert ds.extract_drive_file_ids(
            "https://drive.google.com/file/d/11ATmxJK8BFvSAhbfzpoO-gy0Ckhmmfd-/view?usp=sharing"
        ) == ["11ATmxJK8BFvSAhbfzpoO-gy0Ckhmmfd-"]

    def test_no_link_returns_empty(self):
        assert ds.extract_drive_file_ids("just set the wholesale price to 53") == []

    def test_dedupes_same_id_across_shapes(self):
        out = ds.extract_drive_file_ids(
            "drive.google.com/open?id=ABCDEFGHIJKLMNOPQRSTUV and "
            "drive.google.com/file/d/ABCDEFGHIJKLMNOPQRSTUV/view"
        )
        assert out == ["ABCDEFGHIJKLMNOPQRSTUV"]

    def test_ignores_non_drive_urls(self):
        assert ds.extract_drive_file_ids("see https://example.com/file/d/xxxxxxxxxxxxxxxxxxxx/view") == []


class TestImpersonationSubject:
    def test_prefers_sender_email(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_DELEGATED_USER_EMAIL", "bot@winefornia.com")
        # the file owner (Chat sender) wins over the fixed delegated mailbox
        assert ds._impersonation_subject("cecil.park@winefornia.com") == "cecil.park@winefornia.com"

    def test_falls_back_to_delegated_user(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_DELEGATED_USER_EMAIL", "bot@winefornia.com")
        assert ds._impersonation_subject("") == "bot@winefornia.com"
        assert ds._impersonation_subject("not-an-email") == "bot@winefornia.com"

    def test_none_when_nothing_available(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_DELEGATED_USER_EMAIL", raising=False)
        assert ds._impersonation_subject("") is None


class TestDownload:
    def test_no_delegation_returns_none(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", raising=False)
        monkeypatch.delenv("GOOGLE_DELEGATED_USER_EMAIL", raising=False)
        assert ds.download_drive_file("someid") is None
        assert ds.download_drive_file("someid", "cecil@winefornia.com") is None  # no SA key

    def test_empty_id_returns_none(self):
        assert ds.download_drive_file("") is None
