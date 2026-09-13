"""Free-text arguments other than bodies are bounded, and the refusal names the
field and its limit."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import server
from ai_agent_channel.tools.common import (
    FILTER_MAX,
    LABEL_MAX,
    NOTE_MAX,
    TITLE_MAX,
    VERSION_MAX,
    check_length,
)


def test_check_length_names_the_field_and_the_limit():
    check_length(None, "note", 3)
    check_length("abc", "note", 3)
    with pytest.raises(ValueError, match=r"'note' must be <= 3 characters \(got 4\)"):
        check_length("abcd", "note", 3)


def _debt(as_role) -> int:
    as_role("frontend")
    sent = server.send_message(to="backend", topic="t", body="b", action_required=True)["id"]
    as_role("backend")
    return sent


def test_pin_title_and_version(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="'title'"):
        server.pin_set(key="k", title="t" * (TITLE_MAX + 1), version="v", body="b")
    with pytest.raises(ValueError, match="'version'"):
        server.pin_set(key="k", title="t", version="v" * (VERSION_MAX + 1), body="b")
    assert server.pin_set(key="k", title="t" * TITLE_MAX, version="v" * VERSION_MAX, body="b")


def test_notes_and_reasons(db_path: Path, as_role):
    long = "n" * (NOTE_MAX + 1)
    debt = _debt(as_role)
    with pytest.raises(ValueError, match="'resolution_note'"):
        server.resolve_message(message_id=debt, resolution_note=long)
    with pytest.raises(ValueError, match="'note'"):
        server.set_work_status(message_id=debt, work_status="in_progress", note=long)
    with pytest.raises(ValueError, match="'note'"):
        server.acknowledge(message_id=debt, decision="agree", note=long)
    server.resolve_message(message_id=debt, resolution_note="n" * NOTE_MAX)
    as_role("frontend")
    with pytest.raises(ValueError, match="'note'"):
        server.confirm_resolution(message_id=debt, note=long)
    with pytest.raises(ValueError, match="'reason'"):
        server.reopen_message(message_id=debt, reason=long)
    with pytest.raises(ValueError, match="'note'"):
        server.revise_message(message_id=debt, body="b2", note=long)
    with pytest.raises(ValueError, match="'reason'"):
        server.undo_backfill(ids=[debt], reason=long)


def test_addenda_label_and_filters(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="addenda"):
        server.send_message(
            to="backend", topic="t", body="b", addenda={"backend": "a" * (NOTE_MAX + 1)}
        )
    with pytest.raises(ValueError, match="'label'"):
        server.upload_content(text="x", label="l" * (LABEL_MAX + 1))
    with pytest.raises(ValueError, match="'topic'"):
        server.list_messages(topic="t" * (FILTER_MAX + 1))
    with pytest.raises(ValueError, match="'text'"):
        server.list_messages(text="t" * (FILTER_MAX + 1))
    assert server.list_messages(text="t" * FILTER_MAX)["result"] == []
