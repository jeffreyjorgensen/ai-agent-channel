"""An upload has a total size cap; sealing and reading are unaffected."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import db, server


def test_the_cap_is_eight_mebibytes():
    assert db.MAX_UPLOAD_BYTES == 8 * 1024 * 1024


def test_a_first_piece_over_the_cap_is_refused(db_path: Path, as_role, monkeypatch):
    as_role("frontend")
    monkeypatch.setattr(db.blobs, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(ValueError, match="at most 10 bytes"):
        server.upload_content(text="x" * 11)


def test_an_append_that_would_cross_the_cap_is_refused_and_changes_nothing(
    db_path: Path, as_role, monkeypatch
):
    as_role("frontend")
    monkeypatch.setattr(db.blobs, "MAX_UPLOAD_BYTES", 10)
    up = server.upload_content(text="12345")["upload_id"]
    # the cap counts UTF-8 bytes, not characters: two 2-byte characters = 4
    server.upload_content(text="éé", upload_id=up)
    with pytest.raises(ValueError, match="would make it 11"):
        server.upload_content(text="ab", upload_id=up)
    state = server.get_content(upload_id=up, with_body=True)
    assert state["body"] == "12345éé"
    assert state["length_bytes"] == 9
    sealed = server.seal_content(upload_id=up)
    assert sealed["sealed"] is True and sealed["length_bytes"] == 9


def test_an_upload_exactly_at_the_cap_is_accepted(db_path: Path, as_role, monkeypatch):
    as_role("frontend")
    monkeypatch.setattr(db.blobs, "MAX_UPLOAD_BYTES", 10)
    up = server.upload_content(text="x" * 4)["upload_id"]
    assert server.upload_content(text="y" * 6, upload_id=up)["length_bytes"] == 10


def test_only_the_uploader_appends_or_seals(db_path: Path, as_role):
    as_role("frontend")
    up = server.upload_content(text="mine")["upload_id"]
    as_role("backend")
    with pytest.raises(PermissionError, match="belongs to 'frontend', not 'backend'"):
        server.upload_content(text=" and yours", upload_id=up)
    with pytest.raises(PermissionError, match="belongs to 'frontend', not 'backend'"):
        server.seal_content(upload_id=up)
    state = server.get_content(upload_id=up, with_body=True)
    assert state["body"] == "mine" and state["sealed"] is False


def test_an_unknown_upload_is_named(db_path: Path, as_role):
    as_role("frontend")
    for call in (
        lambda: server.upload_content(text="x", upload_id=999),
        lambda: server.seal_content(upload_id=999),
        lambda: server.get_content(upload_id=999),
        lambda: server.send_message(to="backend", topic="t", body_ref=999),
    ):
        with pytest.raises(ValueError, match="upload 999 not found"):
            call()
