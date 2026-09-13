from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import server
from helpers import L


def _send_proposal(as_role) -> int:
    as_role("frontend")
    sent = server.send_message(
        to="backend",
        topic="proposal",
        body="rename field X -> Y",
        kind="proc",
        pin_key=None,
        about_message_id=None,
    )
    return sent["id"]


def test_acknowledge_and_get(db_path: Path, as_role):
    mid = _send_proposal(as_role)
    as_role("backend")
    ack = server.acknowledge(message_id=mid, decision="agree", note="ok")
    assert ack["message_id"] == mid
    assert ack["role"] == "backend"
    assert ack["decision"] == "agree"
    assert ack["note"] == "ok"
    assert ack["created_at"]

    state = server.get_acknowledgements(message_id=mid)
    assert len(state["acknowledgements"]) == 1
    assert state["acknowledgements"][0]["decision"] == "agree"
    assert state["agreed"] == 1 and state["missing"] == []


def test_acknowledge_overwrites_previous(db_path: Path, as_role):
    mid = _send_proposal(as_role)
    as_role("backend")
    server.acknowledge(message_id=mid, decision="needs_changes", note="naming")
    server.acknowledge(message_id=mid, decision="agree")

    acks = server.get_acknowledgements(message_id=mid)["acknowledgements"]
    assert len(acks) == 1
    assert acks[0]["decision"] == "agree"
    assert acks[0]["note"] is None


def test_acknowledge_same_decision_idempotent(db_path: Path, as_role):
    mid = _send_proposal(as_role)
    as_role("backend")
    server.acknowledge(message_id=mid, decision="agree")
    again = server.acknowledge(message_id=mid, decision="agree")
    assert again["decision"] == "agree"
    assert len(server.get_acknowledgements(message_id=mid)["acknowledgements"]) == 1


def test_self_ack_rejected(db_path: Path, as_role):
    mid = _send_proposal(as_role)
    as_role("frontend")
    with pytest.raises(ValueError, match="own message"):
        server.acknowledge(message_id=mid, decision="agree")


def test_invalid_decision_rejected(db_path: Path, as_role):
    mid = _send_proposal(as_role)
    as_role("backend")
    with pytest.raises(ValueError, match="'decision'"):
        server.acknowledge(message_id=mid, decision="ok")


def test_acknowledge_missing_message(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="not found"):
        server.acknowledge(message_id=9999, decision="agree")


def test_agreement_is_machine_checkable(db_path: Path, as_role):
    mid = _send_proposal(as_role)

    # acceptance criterion: agreement by the other side is determinable by id
    def agreed_by(role: str) -> bool:
        return any(
            a["role"] == role and a["decision"] == "agree"
            for a in server.get_acknowledgements(message_id=mid)["acknowledgements"]
        )

    assert agreed_by("backend") is False
    as_role("backend")
    server.acknowledge(message_id=mid, decision="reject", note="breaks types")
    assert agreed_by("backend") is False
    server.acknowledge(message_id=mid, decision="agree", note="ok after fix")
    assert agreed_by("backend") is True


def test_awaiting_ack_surfaces_unanswered_proposals(db_path: Path, as_role):
    mid = _send_proposal(as_role)
    as_role("frontend")
    server.send_message(to="backend", topic="fyi", body="x")  # not a proposal

    as_role("backend")
    waiting = L(server.awaiting_ack())
    assert [m["id"] for m in waiting] == [mid]

    server.acknowledge(message_id=mid, decision="needs_changes", note="naming")
    assert L(server.awaiting_ack()) == []  # any decision answers the proposal


def test_awaiting_ack_explicit_role(db_path: Path, as_role):
    as_role("backend")
    sent = server.send_message(
        to="frontend", topic="p", body="...", kind="proc", pin_key=None, about_message_id=None
    )
    waiting = L(server.awaiting_ack(to_role="frontend"))
    assert [m["id"] for m in waiting] == [sent["id"]]


def test_delete_message_cleans_acks(db_path: Path, as_role):
    mid = _send_proposal(as_role)
    as_role("backend")
    server.acknowledge(message_id=mid, decision="agree")
    as_role("frontend")  # a proposal is withdrawn by its author
    server.delete_message(message_id=mid)
    with pytest.raises(ValueError, match="not found"):
        server.get_acknowledgements(message_id=mid)
