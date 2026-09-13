from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import server
from helpers import L


def _send_obligation(as_role, *, to: str = "backend") -> int:
    as_role("frontend" if to == "backend" else "backend")
    sent = server.send_message(to=to, topic="need fix", body="please", action_required=True)
    return sent["id"]


def test_new_message_status_defaults(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="ar", body="x", action_required=True)
    server.send_message(to="backend", topic="plain", body="x")

    as_role("backend")
    inbox = {m["topic"]: m for m in L(server.read_inbox())}
    assert inbox["ar"]["status"] == "open"
    assert inbox["plain"]["status"] is None


def test_resolve_happy(db_path: Path, as_role):
    mid = _send_obligation(as_role)
    as_role("backend")
    res = server.resolve_message(message_id=mid, resolution_note="fixed, see #12")
    assert res["status"] == "resolved"
    assert res["resolved_by"] == "backend"
    assert res["resolved_at"]
    assert res["already_resolved"] is False

    msg = L(server.list_messages(status="resolved"))[0]
    assert msg["id"] == mid
    assert msg["resolution_note"] == "fixed, see #12"
    assert L(server.list_messages(status="open")) == []


def test_resolve_by_sender_allowed(db_path: Path, as_role):
    mid = _send_obligation(as_role)
    as_role("frontend")
    res = server.resolve_message(message_id=mid, resolution_note="self-resolved")
    assert res["resolved_by"] == "frontend"


def test_resolve_third_party_rejected(db_path: Path, as_role):
    mid = _send_obligation(as_role)
    as_role("ops")
    with pytest.raises(PermissionError):
        server.resolve_message(message_id=mid)


def test_resolve_non_action_required_rejected(db_path: Path, as_role):
    as_role("frontend")
    sent = server.send_message(to="backend", topic="fyi", body="x")
    as_role("backend")
    with pytest.raises(ValueError, match="action_required"):
        server.resolve_message(message_id=sent["id"])


def test_resolve_missing_message(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="not found"):
        server.resolve_message(message_id=9999)


def test_resolve_idempotent(db_path: Path, as_role):
    mid = _send_obligation(as_role)
    as_role("backend")
    server.resolve_message(message_id=mid, resolution_note="first")
    as_role("frontend")
    again = server.resolve_message(message_id=mid, resolution_note="second")
    assert again["already_resolved"] is True
    assert again["resolved_by"] == "backend"  # original resolution kept

    # no-op resolve leaves a single event in the history
    assert len(L(server.message_history(message_id=mid))) == 1


def test_reopen_and_history(db_path: Path, as_role):
    mid = _send_obligation(as_role)
    as_role("backend")
    server.resolve_message(message_id=mid, resolution_note="fixed, see #12")
    as_role("frontend")
    res = server.reopen_message(message_id=mid, reason="null case still broken")
    assert res["status"] == "open"
    assert res["already_open"] is False

    msg = L(server.list_messages(status="open"))[0]
    assert msg["id"] == mid
    assert msg["resolved_by"] is None
    assert msg["resolved_at"] is None
    assert msg["resolution_note"] is None

    events = L(server.message_history(message_id=mid))
    assert [(e["event"], e["role"], e["note"]) for e in events] == [
        ("resolve", "backend", "fixed, see #12"),
        ("reopen", "frontend", "null case still broken"),
    ]
    assert all(e["created_at"] for e in events)


def test_reopen_already_open_noop(db_path: Path, as_role):
    mid = _send_obligation(as_role)
    as_role("backend")
    res = server.reopen_message(message_id=mid, reason="?")
    assert res["already_open"] is True
    assert L(server.message_history(message_id=mid)) == []


def test_message_history_missing_message(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="not found"):
        L(server.message_history(message_id=9999))


def test_open_obligations_default_role(db_path: Path, as_role):
    as_role("frontend")
    ar = server.send_message(to="backend", topic="debt", body="x", action_required=True)
    server.send_message(to="backend", topic="fyi", body="x")

    as_role("backend")
    debts = L(server.open_obligations())
    assert [m["id"] for m in debts] == [ar["id"]]

    server.resolve_message(message_id=ar["id"], resolution_note="done")
    assert L(server.open_obligations()) == []


def test_open_obligations_explicit_role(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="for-be", body="x", action_required=True)
    as_role("backend")
    server.send_message(to="frontend", topic="for-fe", body="x", action_required=True)

    debts = L(server.open_obligations(to_role="frontend"))
    assert len(debts) == 1
    assert debts[0]["topic"] == "for-fe"


def test_resolve_surfaces_to_author_until_confirmed(db_path: Path, as_role):
    mid = _send_obligation(as_role)  # frontend → backend
    as_role("backend")
    server.resolve_message(message_id=mid, resolution_note="fixed, see #42")

    # the author sees the closed debt until they verify it
    as_role("frontend")
    status = server.channel_status()
    assert [r["id"] for r in status["resolved_for_you"]] == [mid]
    assert status["counts"]["resolved_for_you"] == 1
    assert status["resolved_for_you"][0]["resolved_by"] == "backend"
    assert status["resolved_for_you"][0]["resolution_note"] == "fixed, see #42"

    # the resolver does NOT see their own resolve as pending verification
    as_role("backend")
    assert server.channel_status()["resolved_for_you"] == []

    # confirming clears it and lands in the audit trail
    as_role("frontend")
    res = server.confirm_resolution(message_id=mid, note="checked, fine")
    assert res["already_confirmed"] is False
    assert server.channel_status()["resolved_for_you"] == []
    events = L(server.message_history(message_id=mid))
    assert [(e["event"], e["role"]) for e in events] == [
        ("resolve", "backend"),
        ("resolution_confirmed", "frontend"),
    ]


def test_reopen_clears_resolved_for_you(db_path: Path, as_role):
    mid = _send_obligation(as_role)
    as_role("backend")
    server.resolve_message(message_id=mid)
    as_role("frontend")
    server.reopen_message(message_id=mid, reason="not fixed")
    # back to an open debt — nothing pending verification
    assert server.channel_status()["resolved_for_you"] == []

    # a re-resolve needs a FRESH confirmation even after an earlier confirm
    as_role("backend")
    server.resolve_message(message_id=mid)
    as_role("frontend")
    server.confirm_resolution(message_id=mid)
    server.reopen_message(message_id=mid, reason="another case turned up")
    as_role("backend")
    server.resolve_message(message_id=mid)
    as_role("frontend")
    assert [r["id"] for r in server.channel_status()["resolved_for_you"]] == [mid]
    assert server.confirm_resolution(message_id=mid)["already_confirmed"] is False


def test_self_resolve_surfaces_to_addressee(db_path: Path, as_role):
    # cancelling your own request: resolve by the AUTHOR surfaces at the
    # addressee — verification is symmetric, a resolve is never silent
    mid = _send_obligation(as_role)  # frontend → backend
    as_role("frontend")
    server.resolve_message(message_id=mid, resolution_note="cancelling, not needed")
    assert server.channel_status()["resolved_for_you"] == []  # not at the canceller

    as_role("backend")
    status = server.channel_status()
    assert [r["id"] for r in status["resolved_for_you"]] == [mid]
    assert status["resolved_for_you"][0]["resolution_note"] == "cancelling, not needed"
    res = server.confirm_resolution(message_id=mid, note="understood, dropping it")
    assert res["already_confirmed"] is False
    assert server.channel_status()["resolved_for_you"] == []


def test_confirm_resolution_validation(db_path: Path, as_role):
    mid = _send_obligation(as_role)
    as_role("frontend")
    with pytest.raises(ValueError, match="not resolved"):
        server.confirm_resolution(message_id=mid)
    with pytest.raises(ValueError, match="not found"):
        server.confirm_resolution(message_id=9999)

    as_role("backend")
    server.resolve_message(message_id=mid)
    # the resolver cannot certify their own resolution
    with pytest.raises(PermissionError, match="own resolution"):
        server.confirm_resolution(message_id=mid)
    # third parties cannot confirm
    as_role("ops")
    with pytest.raises(PermissionError):
        server.confirm_resolution(message_id=mid)

    as_role("frontend")
    server.confirm_resolution(message_id=mid)
    again = server.confirm_resolution(message_id=mid)
    assert again["already_confirmed"] is True
    # idempotent confirm leaves a single event
    events = [e["event"] for e in L(server.message_history(message_id=mid))]
    assert events.count("resolution_confirmed") == 1


def test_confirm_resolution_non_action_required(db_path: Path, as_role):
    as_role("frontend")
    sent = server.send_message(to="backend", topic="fyi", body="x")
    as_role("backend")
    with pytest.raises(ValueError, match="nothing to confirm"):
        server.confirm_resolution(message_id=sent["id"])


def test_delete_message_cleans_events(db_path: Path, as_role):
    mid = _send_obligation(as_role)
    as_role("backend")
    server.resolve_message(message_id=mid, resolution_note="done")
    as_role("frontend")
    server.confirm_resolution(message_id=mid)  # close the verification window
    as_role("backend")
    server.delete_message(message_id=mid)
    with pytest.raises(ValueError, match="not found"):
        L(server.message_history(message_id=mid))


def test_delete_in_verification_window_rejected(db_path: Path, as_role):
    # resolve → delete must not silently clear the partner's resolved_for_you
    mid = _send_obligation(as_role)  # frontend → backend
    as_role("backend")
    server.resolve_message(message_id=mid, resolution_note="done")
    for actor in ("backend", "frontend"):
        as_role(actor)
        with pytest.raises(ValueError, match="not yet confirmed"):
            server.delete_message(message_id=mid)
    # reopen + re-resolve reopens the window even after an earlier confirm
    as_role("frontend")
    server.confirm_resolution(message_id=mid)
    server.reopen_message(message_id=mid, reason="a case turned up")
    as_role("backend")
    server.resolve_message(message_id=mid, resolution_note="fixed the rest")
    with pytest.raises(ValueError, match="not yet confirmed"):
        server.delete_message(message_id=mid)
