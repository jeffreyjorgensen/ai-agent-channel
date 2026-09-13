"""Field projection: asking a listing for headers instead of whole records.

Bodies dominate a mailbox by volume, so a listing that always returns them
stops fitting in an MCP response long before the history stops being useful.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import db, server
from helpers import L

BIG = "x" * 5000


@pytest.fixture
def history(db_path: Path, as_role):
    as_role("frontend")
    ids = []
    for n in range(3):
        ids.append(
            server.send_message(
                to="backend",
                topic=f"proposal {n}",
                body=BIG,
                kind="proc",
                action_required=(n == 0),
                pin_key=None,
                about_message_id=None,
            )["id"]
        )
    for n in range(3):
        server.pin_set(key="notes", title="Notes", body=BIG + str(n), version=f"v{n}")
    return ids


def _total_body_chars(rows) -> int:
    return sum(len(r.get("body", "")) for r in rows)


def test_default_shape_is_unchanged(history):
    """The projection must be opt-in: a caller that asks for nothing keeps
    getting everything, or old clients would silently lose fields."""
    rows = L(server.list_messages())
    assert _total_body_chars(rows) == 3 * len(BIG)
    assert set(rows[0]) >= {"id", "from", "to", "topic", "body", "created_at"}


def test_headers_preset_drops_the_bodies(history):
    rows = L(server.list_messages(fields=["headers"]))
    assert _total_body_chars(rows) == 0
    assert "body" not in rows[0]
    # a subset: the preset also names fields only some tools compute
    # (snippet/age_days), and project() skips what a row does not have
    assert set(rows[0]) <= set(db.MESSAGE_HEADERS)
    assert {"id", "from", "to", "topic", "created_at", "kind"} <= set(rows[0])
    # the same messages, just narrower
    assert {r["id"] for r in rows} == set(history)


def test_explicit_field_list(history):
    rows = L(server.list_messages(fields=["id", "topic"]))
    assert all(set(r) == {"id", "topic"} for r in rows)


def test_projection_on_every_listing_tool(history):
    as_backend = server.open_obligations
    assert "body" not in L(server.search_messages(query="proposal", fields=["headers"]))[0]
    assert "body" not in L(server.pin_history(key="notes", fields=["headers"]))[0]
    assert "body" not in L(as_backend(to_role="backend", fields=["headers"]))[0]
    assert "body" not in L(server.awaiting_ack(to_role="backend", fields=["headers"]))[0]


def test_projection_keeps_tool_specific_extras(history):
    """age_days / snippet are computed by the tool, not stored — a header
    projection must not throw them away."""
    debts = L(server.open_obligations(to_role="backend", fields=["headers"]))
    assert "age_days" in debts[0]
    hits = L(server.search_messages(query="proposal", fields=["headers"]))
    assert "snippet" in hits[0] and hits[0]["match"] == "fts"


def test_unknown_field_is_rejected_with_the_allowed_list(history):
    with pytest.raises(ValueError, match="unknown field"):
        L(server.list_messages(fields=["id", "boddy"]))
    with pytest.raises(ValueError, match="cannot be empty"):
        L(server.list_messages(fields=[]))


def test_pin_history_headers_carry_the_digest(history):
    rows = L(server.pin_history(key="notes", fields=["headers"]))
    assert "body" not in rows[0]
    # the whole point of a header listing over pins: verify your copies
    # without pulling three 5 KB documents back
    assert len(rows[0]["body_sha256"]) == 64
    assert rows[0]["body_length_bytes"] == len(BIG) + 1
