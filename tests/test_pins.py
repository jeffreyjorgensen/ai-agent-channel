from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import db, server
from helpers import L, present


def test_pin_set_get_roundtrip(db_path: Path, as_role):
    as_role("backend")
    res = server.pin_set(key="api-notes", title="Notes", body="# Rules\n...", version="v1")
    assert res["key"] == "api-notes"
    assert res["version"] == "v1"
    assert res["updated_by"] == "backend"
    assert res["updated_at"]

    as_role("frontend")
    pin = present(server.pin_get(key="api-notes"))
    assert pin["title"] == "Notes"
    assert pin["body"] == "# Rules\n..."
    assert pin["version"] == "v1"
    assert pin["updated_by"] == "backend"


def test_pin_get_missing_returns_none(db_path: Path, as_role):
    as_role("backend")
    assert server.pin_get(key="nope") is None


def test_pin_set_requires_role(db_path: Path, no_role):
    with pytest.raises(ValueError, match="AI_AGENT_CHANNEL_ROLE"):
        server.pin_set(key="k", title="t", body="b", version="v1")


def test_pin_set_upsert_keeps_history(db_path: Path, as_role):
    as_role("backend")
    server.pin_set(key="api-notes", title="Notes", body="old", version="v1")
    as_role("frontend")
    server.pin_set(key="api-notes", title="Notes", body="new", version="v2")

    pin = present(server.pin_get(key="api-notes"))
    assert pin["body"] == "new"
    assert pin["version"] == "v2"
    assert pin["updated_by"] == "frontend"

    history = L(server.pin_history(key="api-notes"))
    assert len(history) == 2
    assert [h["version"] for h in history] == ["v2", "v1"]
    assert [h["updated_by"] for h in history] == ["frontend", "backend"]
    assert history[1]["body"] == "old"


def test_pin_list_latest_per_key_without_bodies(db_path: Path, as_role):
    as_role("backend")
    server.pin_set(key="api-notes", title="Notes", body="x", version="v1")
    server.pin_set(key="api-notes", title="Notes", body="y", version="v2")
    server.pin_set(key="links", title="Links", body="z", version="v1")

    pins = L(server.pin_list())
    assert len(pins) == 2
    by_key = {p["key"]: p for p in pins}
    assert by_key["api-notes"]["version"] == "v2"
    assert by_key["links"]["version"] == "v1"
    for p in pins:
        assert "body" not in p


def _agreed_proposal(as_role, *, body: str, proposer: str = "backend") -> int:
    other = "frontend" if proposer == "backend" else "backend"
    as_role(proposer)
    proposal = server.send_message(
        to=other, topic="proposal", body=body, kind="proc", pin_key=None, about_message_id=None
    )
    as_role(other)
    server.acknowledge(message_id=proposal["id"], decision="agree")
    as_role(proposer)
    return proposal["id"]


def _bootstrap_pin(as_role, key: str, *, title: str, body: str = "v1") -> None:
    """Reserved keys require approved_by even for the FIRST version."""
    pid = _agreed_proposal(as_role, body=f"{key} v1: {body}")
    server.pin_set(key=key, title=title, body=body, version="v1", approved_by=pid)


def test_reserved_pin_first_set_requires_approval(db_path: Path, as_role):
    # the bootstrap of a contractual document is itself a contract
    as_role("backend")
    with pytest.raises(ValueError, match="creating it requires 'approved_by'"):
        server.pin_set(key="team-charter", title="Charter", body="v1", version="v1")
    # with an agreed proposal the bootstrap works
    pid = _agreed_proposal(as_role, body="team-charter v1: charter text")
    res = server.pin_set(
        key="team-charter", title="Charter", body="v1", version="v1", approved_by=pid
    )
    assert res["approved_by"] == pid


def test_unreserved_pin_first_set_is_free(db_path: Path, as_role):
    as_role("backend")
    res = server.pin_set(key="api-notes", title="Notes", body="v1", version="v1")
    assert res["approved_by"] is None


def test_protected_pin_update_requires_approval(db_path: Path, as_role):
    _bootstrap_pin(as_role, "contract-version", title="Contract", body="x")
    with pytest.raises(ValueError, match="protected"):
        server.pin_set(key="contract-version", title="Contract", body="y", version="openapi-2")
    # unprotected keys stay freely updatable
    server.pin_set(key="api-notes", title="Notes", body="a", version="v1")
    server.pin_set(key="api-notes", title="Notes", body="b", version="v2")
    # glossary is shared truth — protected like the charter
    _bootstrap_pin(as_role, "glossary", title="Glossary", body="x")
    with pytest.raises(ValueError, match="protected"):
        server.pin_set(key="glossary", title="Glossary", body="y", version="v2")


def test_protected_pin_update_with_agreed_proposal(db_path: Path, as_role):
    _bootstrap_pin(as_role, "contract-version", title="Contract", body="x")
    pid = _agreed_proposal(as_role, body="contract-version → openapi-2: add field Y")

    res = server.pin_set(
        key="contract-version",
        title="Contract",
        body="y",
        version="openapi-2",
        approved_by=pid,
    )
    assert res["approved_by"] == pid
    assert present(server.pin_get(key="contract-version"))["approved_by"] == pid


def test_pin_approval_must_mention_key(db_path: Path, as_role):
    _bootstrap_pin(as_role, "team-charter", title="Charter", body="x")
    # agreed, but about something unrelated — cannot be reused for the charter
    pid = _agreed_proposal(as_role, body="contract-version → openapi-9")
    with pytest.raises(ValueError, match="does not name"):
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="y",
            version="v2",
            approved_by=pid,
        )


def test_pin_approval_overwritten_agree_rejected(db_path: Path, as_role):
    # acks are one row per (message, role): re-acking OVERWRITES the decision,
    # so an agree later changed to needs_changes is no consent at pin_set time
    _bootstrap_pin(as_role, "team-charter", title="Charter")
    pid = _agreed_proposal(as_role, body="team-charter: edit to section 3")
    as_role("frontend")
    server.acknowledge(message_id=pid, decision="needs_changes", note="changed my mind")
    as_role("backend")
    with pytest.raises(ValueError, match="no 'agree'"):
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="v2",
            version="v2",
            approved_by=pid,
        )


def test_pin_approval_single_use(db_path: Path, as_role):
    _bootstrap_pin(as_role, "team-charter", title="Charter")
    pid = _agreed_proposal(as_role, body="team-charter: edit to section 2")

    server.pin_set(key="team-charter", title="Charter", body="v2", version="v2", approved_by=pid)
    # same consent cannot push a second change
    with pytest.raises(ValueError, match="already approved"):
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="v3",
            version="v3",
            approved_by=pid,
        )


def test_pin_approval_stale_agree_rejected(db_path: Path, as_role):
    _bootstrap_pin(as_role, "team-charter", title="Charter")
    old_pid = _agreed_proposal(as_role, body="team-charter: edit A")
    # Stamps are milliseconds, so a fast run can give the old consent and the
    # next version the same instant. Place both explicitly: v1 first, then the
    # consent given against it — which is strictly before any version written
    # from here on.
    with db.open_db() as conn:
        conn.execute(
            "UPDATE pinned_entries SET updated_at = '2026-01-01T00:00:00.000Z' "
            "WHERE key = 'team-charter'"
        )
        conn.execute(
            "UPDATE acknowledgements SET created_at = '2026-01-01T00:00:01.000Z' "
            "WHERE message_id = ?",
            (old_pid,),
        )

    # the pin moves on via a different agreed proposal
    new_pid = _agreed_proposal(as_role, body="team-charter: edit B")
    server.pin_set(
        key="team-charter",
        title="Charter",
        body="v2",
        version="v2",
        approved_by=new_pid,
    )

    # consent given against v1 cannot authorise changing v2
    with pytest.raises(ValueError, match="stale"):
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="v3",
            version="v3",
            approved_by=old_pid,
        )


def test_pin_once_approved_stays_protected(db_path: Path, as_role):
    # a non-reserved key starts freely updatable...
    as_role("backend")
    server.pin_set(key="api-notes", title="Notes", body="v1", version="v1")
    # ...but one approved update makes it contractual forever
    pid = _agreed_proposal(as_role, body="api-notes: edit v2")
    server.pin_set(key="api-notes", title="Notes", body="v2", version="v2", approved_by=pid)
    with pytest.raises(ValueError, match="protected"):
        server.pin_set(key="api-notes", title="Notes", body="v3", version="v3")
    # a fresh agreed proposal still works
    pid2 = _agreed_proposal(as_role, body="api-notes: edit v3")
    res = server.pin_set(key="api-notes", title="Notes", body="v3", version="v3", approved_by=pid2)
    assert res["approved_by"] == pid2


def test_pin_first_set_with_provenance(db_path: Path, as_role):
    pid = _agreed_proposal(as_role, body="team-charter v1: charter text")
    res = server.pin_set(
        key="team-charter", title="Charter", body="v1", version="v1", approved_by=pid
    )
    assert res["approved_by"] == pid


def test_delete_approval_message_rejected(db_path: Path, as_role):
    pid = _agreed_proposal(as_role, body="team-charter v1")
    server.pin_set(key="team-charter", title="Charter", body="v1", version="v1", approved_by=pid)
    with pytest.raises(ValueError, match="approval record"):
        server.delete_message(message_id=pid)


def test_protected_pin_update_without_agree_rejected(db_path: Path, as_role):
    _bootstrap_pin(as_role, "team-charter", title="Charter", body="x")
    # names the key structurally, so the only thing missing is consent
    proposal = server.send_message(
        to="frontend",
        topic="change charter",
        body="...",
        kind="proc",
        pin_key="team-charter",
        about_message_id=None,
    )
    # no ack at all
    with pytest.raises(ValueError, match="no 'agree'"):
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="y",
            version="v2",
            approved_by=proposal["id"],
        )
    # explicit disagreement is not approval either
    as_role("frontend")
    server.acknowledge(message_id=proposal["id"], decision="needs_changes", note="no")
    as_role("backend")
    with pytest.raises(ValueError, match="no 'agree'"):
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="y",
            version="v2",
            approved_by=proposal["id"],
        )


def test_pin_approval_foreign_message_rejected(db_path: Path, as_role):
    _bootstrap_pin(as_role, "team-charter", title="Charter", body="x")
    proposal = server.send_message(to="frontend", topic="p", body="...")
    as_role("frontend")
    server.acknowledge(message_id=proposal["id"], decision="agree")

    as_role("ops")
    with pytest.raises(PermissionError):
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="y",
            version="v2",
            approved_by=proposal["id"],
        )


def test_pin_approval_missing_message_rejected(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="not found"):
        server.pin_set(
            key="glossary",
            title="Glossary",
            body="x",
            version="v1",
            approved_by=9999,
        )


def test_pin_set_validation(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="'key'"):
        server.pin_set(key="", title="t", body="b", version="v1")
    with pytest.raises(ValueError, match="'title'"):
        server.pin_set(key="k", title="", body="b", version="v1")
    with pytest.raises(ValueError, match="'body'"):
        server.pin_set(key="k", title="t", body="", version="v1")
    with pytest.raises(ValueError, match="'version'"):
        server.pin_set(key="k", title="t", body="b", version="")
    with pytest.raises(ValueError, match="80"):
        server.pin_set(key="k" * 81, title="t", body="b", version="v1")
