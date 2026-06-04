from services import tastingroom_mailbox


def test_process_gmail_message_uses_reservation_state_for_labels(monkeypatch):
    applied = {}

    monkeypatch.setattr(tastingroom_mailbox, "message_already_processed", lambda *args, **kwargs: False)

    def fake_read_email(message_id):
        return {
            "body": "Client accepted the offered slot.",
            "subject": "Availability Check",
            "from": "josh@thecavesatsodacanyon.com",
            "to": "cecil.park@winefornia.com",
            "thread_id": "19e123abc456",
        }

    def fake_apply_message_labels(message_id, *, remove_labels, add_labels):
        applied["message_id"] = message_id
        applied["remove_labels"] = remove_labels
        applied["add_labels"] = add_labels

    import services.gmail_service as gmail_service
    import agents.case_desk_graph as case_desk_graph_module

    monkeypatch.setattr(gmail_service, "read_email", fake_read_email)
    monkeypatch.setattr(gmail_service, "apply_message_labels", fake_apply_message_labels)
    monkeypatch.setattr(
        case_desk_graph_module.case_desk_graph,
        "invoke",
        lambda *args, **kwargs: {
            "message_type": "josh_availability_reply",
            "reservation_id": "res_1",
            "_reservation": {"current_state": "READY_TO_OFFER_CLIENT"},
            "final_response": "Ready to offer client.",
        },
    )

    result = tastingroom_mailbox.process_gmail_message("msg_1", labels=[])

    assert result["state"] == "READY_TO_OFFER_CLIENT"
    assert "Tasting Room/Action Needed" in applied["add_labels"]
