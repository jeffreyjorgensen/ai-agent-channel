"""The cleanup pass: what the preview shows is what the apply accepts, and
only its parties can take it back."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import db, server


def _legacy(**kw) -> int:
    kw.setdefault("from_role", "frontend")
    kw.setdefault("to_role", "backend")
    kw.setdefault("topic", "old proposal")
    kw.setdefault("body", "draft")
    with db.open_db() as conn:
        return db.insert_message(conn, action_required=False, reply_to=None, kind="proc", **kw)[
            "id"
        ]


@pytest.fixture
def history(db_path: Path, as_role):
    """A settled 'notes' round with a leftover draft, and a nudge whose target
    was deleted before deletion cascaded."""
    as_role("infra")
    server.send_message(to="frontend", topic="hello", body="present")
    draft = _legacy(pin_key="notes", body="draft 1")
    approved = _legacy(pin_key="notes", body="final")
    as_role("backend")
    server.acknowledge(message_id=approved, decision="agree")
    as_role("frontend")
    server.pin_set(key="notes", title="n", version="1", body="final", approved_by=approved)
    target = _legacy(body="abandoned")
    nudge = _legacy(body="please vote", about_message_id=target)
    with db.open_db() as conn:
        conn.execute("UPDATE messages SET superseded_at = NULL")
        conn.execute("DELETE FROM message_events WHERE event = 'superseded'")
        conn.execute(
            "UPDATE messages SET deleted_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (target,),
        )
    word = server.send_message(to="backend", topic="cleanup notes", body="go")["id"]
    return {"draft": draft, "nudge": nudge, "word": word}


def test_a_preview_by_key_can_be_applied_with_the_same_arguments(history, as_role):
    as_role("frontend")
    preview = server.backfill_superseded(key="notes")
    ids = [r["id"] for r in preview["would_retire"]]
    assert set(ids) == {history["draft"], history["nudge"]}

    applied = server.backfill_superseded(
        dry_run=False,
        key="notes",
        word_message_id=history["word"],
        ids=ids,
        expect_count=len(ids),
    )
    assert sorted(applied["retired"]) == sorted(ids)


def test_only_the_applier_or_the_key_owner_can_undo(history, as_role):
    """backend applies a pass over 'notes' but does not own the key; frontend
    owns it (it set the current version); infra is neither. The case where
    one role is both is the acceptance contract (T-24)."""
    as_role("backend")
    server.backfill_superseded(
        dry_run=False,
        key="notes",
        word_message_id=history["word"],
        ids=[history["draft"], history["nudge"]],
        expect_count=2,
    )
    as_role("infra")
    for retired in (history["draft"], history["nudge"]):
        with pytest.raises(PermissionError, match="only that role or the key's owner"):
            server.undo_backfill(ids=[retired])
    # the applier can undo its own pass
    as_role("backend")
    assert server.undo_backfill(ids=[history["nudge"]])["restored"] == [history["nudge"]]
    # and so can the key's owner
    as_role("frontend")
    assert server.undo_backfill(ids=[history["draft"]])["restored"] == [history["draft"]]


def test_filter_keeps_keyless_rows_when_a_key_is_named():
    rows = [
        {"id": 1, "match_kind": "key", "claimed_by": ["notes"]},
        {"id": 2, "match_kind": "key", "claimed_by": ["glossary"]},
        {"id": 3, "match_kind": "target", "claimed_by": []},
        {"id": 4, "match_kind": "reference", "claimed_by": []},
    ]
    assert [r["id"] for r in db.filter_candidates(rows, keys=["notes"])] == [1, 3]
    assert [
        r["id"] for r in db.filter_candidates(rows, keys=["notes"], include_by_reference=True)
    ] == [1, 3, 4]
