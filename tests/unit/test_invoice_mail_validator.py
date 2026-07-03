"""Tests for the Square-email validation loop (services/invoice_mail_validator).

The contract: an invoice case is OPEN (pending_verification) from creation
until Square's own notification email confirms it — "created" closes the case
as verified, "paid" upgrades it. Wine-order mail finally has its correct home:
it validates the INVOICE pipeline, and never touches the tasting room.
"""

import pytest

import services.invoice_mail_validator as imv


# ── subject classification ───────────────────────────────────────────────────

class TestClassifySquareSubject:
    def test_created(self):
        kind, num = imv.classify_square_subject(
            "A new invoice was created for Christina Yoo (#202468)")
        assert (kind, num) == ("created", "202468")

    def test_paid(self):
        kind, num = imv.classify_square_subject(
            "An invoice was paid by Christina Yoo! (#202468)")
        assert (kind, num) == ("paid", "202468")

    def test_payment_processed_variant(self):
        kind, num = imv.classify_square_subject(
            "Payment processed: Invoice #202447 to Ina Lee")
        assert (kind, num) == ("paid", "202447")

    def test_noise_is_ignored(self):
        for subject in ("Your Square sales summary for June 12",
                        "Payment initiated: #202447 to Ina Lee",
                        "Reminder: invoice #202447 is due soon" ,
                        ""):
            kind, _ = imv.classify_square_subject(subject)
            assert kind is None, subject


# ── poll_once flow ────────────────────────────────────────────────────────────

@pytest.fixture
def harness(mocker):
    """Stub Gmail + repository; returns dicts the test can inspect."""
    state = {"emails": [], "labels": {}, "verifications": [], "workflow": []}
    mocker.patch("services.gmail_service.list_emails",
                 side_effect=lambda **k: {"messages": state["emails"]})
    mocker.patch("services.gmail_service.apply_message_labels",
                 side_effect=lambda mid, add_labels=None, **k:
                 state["labels"].setdefault(mid, []).extend(add_labels or []))
    mocker.patch("db.repository.mark_invoice_verification",
                 side_effect=lambda tid, **k: state["verifications"].append((tid, k)))
    mocker.patch("db.repository.update_workflow_record_status",
                 side_effect=lambda eid, status, summary="": state["workflow"].append((eid, status)))
    state["find"] = mocker.patch("db.repository.find_invoice_log_by_number", return_value=None)
    return state


def _mail(mid, subject):
    return {"message_id": mid, "subject": subject, "from": "Square <invoicing@messaging.squareup.com>"}


class TestPollOnce:
    def test_created_email_confirms_and_closes_the_case(self, harness):
        harness["emails"] = [_mail("m1", "A new invoice was created for Christina Yoo (#202468)")]
        harness["find"].return_value = {"thread_id": "chat_1", "square_invoice_id": "inv_9",
                                        "verification_status": "pending"}
        out = imv.poll_once()
        assert out["processed"][0]["result"] == "created_confirmed"
        assert harness["verifications"] == [("chat_1", {"status": "created_confirmed",
                                                        "stamp_field": "verified_created_at"})]
        assert harness["workflow"] == [("inv_9", "completed_draft_saved")]
        assert imv._LABEL_PROCESSED in harness["labels"]["m1"]

    def test_paid_email_upgrades_to_paid(self, harness):
        harness["emails"] = [_mail("m2", "An invoice was paid by Christina Yoo! (#202468)")]
        harness["find"].return_value = {"thread_id": "chat_1", "square_invoice_id": "inv_9",
                                        "verification_status": "created_confirmed"}
        out = imv.poll_once()
        assert out["processed"][0]["result"] == "paid_confirmed"
        assert harness["workflow"] == [("inv_9", "completed_paid")]

    def test_unmatched_invoice_number_is_labeled_for_humans(self, harness):
        harness["emails"] = [_mail("m3", "A new invoice was created for Someone Manual (#999999)")]
        out = imv.poll_once()
        assert out["processed"][0]["result"] == "unmatched"
        assert imv._LABEL_UNMATCHED in harness["labels"]["m3"]
        assert not harness["verifications"]

    def test_noise_mail_marked_processed_without_touching_cases(self, harness):
        harness["emails"] = [_mail("m4", "Your Square sales summary")]
        out = imv.poll_once()
        assert out["processed"][0]["result"] == "ignored"
        assert imv._LABEL_PROCESSED in harness["labels"]["m4"]
        assert not harness["verifications"] and not harness["workflow"]

    def test_gmail_outage_returns_error_without_raising(self, mocker):
        mocker.patch("services.gmail_service.list_emails", side_effect=RuntimeError("no creds"))
        out = imv.poll_once()
        assert out["ok"] is False

    def test_repo_failure_leaves_mail_unlabeled_for_retry(self, harness):
        harness["emails"] = [_mail("m5", "An invoice was paid by X! (#111)")]
        harness["find"].side_effect = RuntimeError("db down")
        out = imv.poll_once()
        assert out["processed"][0]["result"] == "error"
        assert "m5" not in harness["labels"]     # untouched → retried next poll
