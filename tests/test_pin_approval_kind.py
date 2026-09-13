"""Only a proposal approves a pin; a proposal that merely mentions a key cannot
approve it around that key's open round; a preview does not take the lock."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from ai_agent_channel import server


def _agreed(as_role, message_id: int) -> None:
    as_role("backend")
    server.acknowledge(message_id=message_id, decision="agree")
    as_role("frontend")


def test_a_message_that_is_not_a_proposal_cannot_approve_a_pin(db_path: Path, as_role):
    as_role("frontend")
    question = server.send_message(to="backend", topic="glossary change?", body="glossary v2")
    _agreed(as_role, question["id"])
    with pytest.raises(ValueError, match="not a proposal"):
        server.pin_set(
            key="glossary", title="G", version="v2", body="b", approved_by=question["id"]
        )
    preview = server.pin_set(
        key="glossary", title="G", version="v2", body="b", approved_by=question["id"], dry_run=True
    )
    assert preview["ok"] is False and "kind='proc'" in preview["problem"]


def _unlinked_proposal(as_role) -> int:
    as_role("frontend")
    return server.send_message(
        to="backend",
        topic="glossary v2",
        body="new glossary",
        kind="proc",
        pin_key=None,
        about_message_id=None,
    )["id"]


def test_a_mention_cannot_approve_a_key_that_has_an_open_round(db_path: Path, as_role):
    legacy = _unlinked_proposal(as_role)
    _agreed(as_role, legacy)
    as_role("backend")
    round_id = server.send_message(
        to="frontend",
        topic="glossary v3",
        body="another glossary",
        kind="proc",
        pin_key="glossary",
        about_message_id=None,
    )["id"]
    as_role("frontend")
    with pytest.raises(ValueError, match="open round") as refusal:
        server.pin_set(key="glossary", title="G", version="v2", body="b", approved_by=legacy)
    assert f"#{round_id}" in str(refusal.value)
    assert server.pin_get(key="glossary") is None


def test_a_mention_still_approves_a_key_without_an_open_round(db_path: Path, as_role):
    legacy = _unlinked_proposal(as_role)
    _agreed(as_role, legacy)
    written = server.pin_set(key="glossary", title="G", version="v2", body="b", approved_by=legacy)
    assert written["approved_by"] == legacy


def test_pin_set_dry_run_does_not_wait_for_the_write_lock(db_path: Path, as_role):
    as_role("frontend")
    server.pin_set(key="notes", title="N", version="v1", body="b")
    holder = sqlite3.connect(str(db_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        preview = server.pin_set(key="notes", title="N", version="v2", body="c", dry_run=True)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert preview["ok"] is True
    assert elapsed < 1.0, f"a preview waited {elapsed:.2f}s for the write lock"
