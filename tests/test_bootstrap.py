from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai_agent_channel import server
from helpers import L


def test_channel_status_empty(db_path: Path, as_role):
    as_role("backend")
    status = server.channel_status()
    server_block = status.pop("server")
    assert server_block["build"] and server_block["whats_new"]
    assert status == {
        "role": "backend",
        "counts": {
            "unread": 0,
            "unopened": 0,
            "opened_unmarked": 0,
            "open_obligations": 0,
            "open_obligations_untaken": 0,
            "awaiting_ack": 0,
            "blocked": 0,
            "unblocked": 0,
            "in_progress": 0,
            "needs_you": 0,
            "awaiting_done": 0,
            "resolved_for_you": 0,
        },
        "unread": 0,
        "open_obligations": 0,
        "awaiting_ack": 0,
        "blocked": [],
        "unblocked": [],
        "in_progress": [],
        "needs_you": [],
        "awaiting_done": [],
        "resolved_for_you": [],
        "pins": [],
    }


def test_channel_status_counts_and_pins(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="fyi", body="x")
    debt = server.send_message(to="backend", topic="debt", body="x", action_required=True)
    proposal = server.send_message(
        to="backend", topic="prop", body="x", kind="proc", pin_key=None, about_message_id=None
    )
    server.pin_set(key="api-notes", title="Notes", body="...", version="v1")

    as_role("backend")
    status = server.channel_status()
    assert status["role"] == "backend"
    assert status["unread"] == 3
    assert status["open_obligations"] == 1
    assert status["awaiting_ack"] == 1
    server.acknowledge(message_id=proposal["id"], decision="agree")
    assert server.channel_status()["awaiting_ack"] == 0
    assert [p["key"] for p in status["pins"]] == ["api-notes"]
    assert "body" not in status["pins"][0]

    server.mark_read(message_id=debt["id"])
    server.resolve_message(message_id=debt["id"], resolution_note="done")
    status = server.channel_status()
    assert status["unread"] == 2
    assert status["open_obligations"] == 0


def test_channel_status_requires_role(db_path: Path, no_role):
    with pytest.raises(ValueError, match="AI_AGENT_CHANNEL_ROLE"):
        server.channel_status()


# Original schema as shipped before pins/status/ack — used to verify that
# opening a pre-existing DB migrates it in place.
OLD_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    topic TEXT NOT NULL,
    body TEXT NOT NULL,
    action_required INTEGER NOT NULL DEFAULT 0,
    reply_to INTEGER NULL REFERENCES messages(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    read_at TEXT NULL
);
CREATE INDEX idx_to_role_unread ON messages(to_role, read_at);
CREATE INDEX idx_topic ON messages(topic);
CREATE INDEX idx_reply_to ON messages(reply_to);
"""


def test_migration_of_pre_existing_db(db_path: Path, as_role):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO messages (from_role, to_role, topic, body, action_required) "
        "VALUES ('frontend', 'backend', 'old debt', 'x', 1)"
    )
    conn.execute(
        "INSERT INTO messages (from_role, to_role, topic, body) "
        "VALUES ('frontend', 'backend', 'old fyi', 'x')"
    )
    conn.commit()
    conn.close()

    as_role("backend")
    # pre-migration action_required message is backfilled to status=open
    debts = L(server.open_obligations())
    assert [m["topic"] for m in debts] == ["old debt"]
    assert debts[0]["status"] == "open"
    assert debts[0]["kind"] is None

    inbox = {m["topic"]: m for m in L(server.read_inbox())}
    assert inbox["old fyi"]["status"] is None

    # new entities work on the migrated DB
    server.resolve_message(message_id=debts[0]["id"], resolution_note="done")
    assert L(server.open_obligations()) == []
    server.pin_set(key="api-notes", title="Notes", body="...", version="v1")
    assert server.channel_status()["pins"][0]["key"] == "api-notes"
