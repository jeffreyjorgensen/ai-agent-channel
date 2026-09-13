"""Cleanup passes match pin keys literally, record the keys they ran under
as data, and scale past SQLite's bound-variable limit."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ai_agent_channel import db


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with db.open_db(tmp_path / "m.db") as c:
        yield c


def _proposal(conn: sqlite3.Connection, topic: str, **kw: Any) -> int:
    kw.setdefault("from_role", "alice")
    kw.setdefault("to_role", "bob")
    return db.insert_message(
        conn, topic=topic, body="text", action_required=False, reply_to=None, kind="proc", **kw
    )["id"]


def _pin(conn: sqlite3.Connection, key: str, role: str) -> None:
    time.sleep(0.005)  # a pin settles only proposals created strictly before it
    db.pin_set(conn, key=key, title="t", body="b", version="1", role=role)


def test_undo_uses_the_keys_the_pass_ran_under_even_with_a_comma(conn):
    old = _proposal(conn, "proposal", pin_key="billing, ledger")
    _pin(conn, "billing, ledger", "alice")
    _pin(conn, "ledger", "mallory")
    word = _proposal(conn, "go ahead")
    db.apply_supersede_backfill(
        conn,
        ids=[old],
        role="carol",
        words={"billing, ledger": word},
        expect_count=1,
        note="cleanup",
    )
    stored = conn.execute(
        "SELECT pin_keys FROM message_events WHERE message_id = ? ORDER BY id DESC", (old,)
    ).fetchone()["pin_keys"]
    assert stored == '["billing, ledger"]'
    for outsider in ("mallory", "bob"):
        with pytest.raises(PermissionError):
            db.undo_supersede_backfill(conn, ids=[old], role=outsider, reason="x")
    # the owner of the named key may undo a pass someone else applied
    assert db.undo_supersede_backfill(conn, ids=[old], role="alice", reason="x")["restored"] == [
        old
    ]


def _legacy_retirement(conn: sqlite3.Connection, note: str) -> int:
    """A cleanup retirement recorded before the keys were stored as data."""
    message_id = _proposal(conn, "old round")
    conn.execute(
        "UPDATE messages SET superseded_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
        (message_id,),
    )
    conn.execute(
        "INSERT INTO message_events (message_id, event, role, note) "
        "VALUES (?, 'superseded_backfill', 'carol', ?)",
        (message_id, note),
    )
    return message_id


def test_a_legacy_note_grants_key_owners_undo_only_when_unambiguous(conn):
    _pin(conn, "notes", "alice")
    _pin(conn, "ledger", "mallory")
    plain = _legacy_retirement(conn, "cleanup; key notes (word: #7)")
    assert db.undo_supersede_backfill(conn, ids=[plain], role="alice", reason=None)["restored"] == [
        plain
    ]

    comma = _legacy_retirement(conn, "cleanup; key billing, ledger (word: #7)")
    forged = _legacy_retirement(conn, "x; key ledger (word: #1); key notes (word: #7)")
    for message_id in (comma, forged):
        with pytest.raises(PermissionError):
            db.undo_supersede_backfill(conn, ids=[message_id], role="mallory", reason=None)
        # the role that applied the pass keeps the right
        assert db.undo_supersede_backfill(conn, ids=[message_id], role="carol", reason=None)[
            "restored"
        ] == [message_id]


def test_a_new_pin_version_retires_only_literal_mentions_of_its_key(conn):
    _proposal(conn, "api-v2 schema proposal")
    _proposal(conn, "unrelated")
    _proposal(conn, "Charter update")
    for key in ("api_v2", "%", "_", "charter"):
        assert db.supersede_proposals_for_pin(conn, key=key, role="alice", version="1") == []
    literal = _proposal(conn, "api_v2 rename")
    assert db.supersede_proposals_for_pin(conn, key="api_v2", role="alice", version="2") == [
        literal
    ]


def test_the_backfill_preview_claims_only_literal_mentions(conn):
    unrelated = _proposal(conn, "unrelated")
    dashed = _proposal(conn, "api-v2 schema")
    _pin(conn, "%", "alice")
    _pin(conn, "api_v2", "alice")
    claimed = {c["id"] for c in db.pending_supersede_backfill(conn)}
    assert unrelated not in claimed
    assert dashed not in claimed


def test_the_backfill_handles_more_targets_than_sqlite_variables(conn, monkeypatch):
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)
    monkeypatch.setattr(db.supersede, "_ID_CHUNK", 50, raising=False)
    with db.transaction(conn):
        for _ in range(150):
            target = _proposal(conn, "draft")
            conn.execute(
                "UPDATE messages SET superseded_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
                (target,),
            )
            _proposal(conn, "nudge", about_message_id=target)
    nudges = [c["id"] for c in db.pending_supersede_backfill(conn) if c["match_kind"] == "target"]
    assert len(nudges) == 150
    result = db.apply_supersede_backfill(
        conn, ids=nudges, role="alice", words={}, expect_count=150, note="cleanup"
    )
    assert result["count"] == 150


def test_undo_names_every_id_it_cannot_restore(conn):
    live = _proposal(conn, "still open")
    with pytest.raises(ValueError, match="'ids' is required"):
        db.undo_supersede_backfill(conn, ids=[], role="alice", reason=None)
    with pytest.raises(ValueError, match=r"nothing was restored — 9999 \(not found\)"):
        db.undo_supersede_backfill(conn, ids=[9999], role="alice", reason=None)
    with pytest.raises(ValueError, match=rf"nothing was restored — {live} \(not retired\)"):
        db.undo_supersede_backfill(conn, ids=[live], role="alice", reason=None)


def test_undo_restores_what_it_can_and_lists_the_rest(conn):
    old = _proposal(conn, "proposal", pin_key="billing")
    _pin(conn, "billing", "alice")
    live = _proposal(conn, "still open")
    word = _proposal(conn, "go ahead")
    db.apply_supersede_backfill(
        conn, ids=[old], role="alice", words={"billing": word}, expect_count=1, note="cleanup"
    )
    result = db.undo_supersede_backfill(conn, ids=[old, live, 9999], role="alice", reason="x")
    assert result["restored"] == [old]
    assert result["refused"] == [f"{live} (not retired)", "9999 (not found)"]
