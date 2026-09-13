"""A proposal is a shared decision: deleting it tombstones it for everyone and
closes its round, so only its author may delete it."""

from __future__ import annotations

import pytest

from ai_agent_channel import db, server


def _proposal(as_role) -> int:
    as_role("alpha")
    sent = server.send_message(
        to="beta",
        topic="adopt the glossary",
        body="proposed text",
        kind="proc",
        pin_key="glossary",
        about_message_id=None,
    )
    return sent["id"]


def test_a_recipient_cannot_delete_a_proposal(db_path, as_role):
    proposal = _proposal(as_role)
    as_role("beta")
    with pytest.raises(PermissionError, match="only its author"):
        server.delete_message(message_id=proposal)
    with db.open_db() as conn:
        assert db.open_rounds_for_pin(conn, key="glossary")


def test_the_author_can_withdraw_a_proposal(db_path, as_role):
    proposal = _proposal(as_role)
    as_role("alpha")
    server.delete_message(message_id=proposal)
    with db.open_db() as conn:
        assert db.open_rounds_for_pin(conn, key="glossary") == []


def test_a_recipient_can_still_delete_an_ordinary_message(db_path, as_role):
    as_role("alpha")
    note = server.send_message(to="beta", topic="fyi", body="a note")["id"]
    as_role("beta")
    server.delete_message(message_id=note)
    with db.open_db() as conn:
        row = conn.execute("SELECT deleted_at FROM messages WHERE id = ?", (note,)).fetchone()
        assert row["deleted_at"]


def test_done_refusal_does_not_mention_a_merge(db_path, as_role):
    as_role("alpha")
    debt = server.send_message(to="beta", topic="task", body="do it", action_required=True)["id"]
    with pytest.raises(ValueError) as refused:
        server.set_work_status(message_id=debt, work_status="done")
    assert "merge" not in str(refused.value)
    assert "done_local" in str(refused.value)
