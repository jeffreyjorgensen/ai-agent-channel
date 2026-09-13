"""Read-only views for the human-facing web board. Role-agnostic: they read the
whole channel and deliberately ignore the per-role rules of the MCP tools."""

from __future__ import annotations

import sqlite3
from typing import Any

from .fields import row_to_dict
from .messages import expand_recipients, fetch_events, fetch_thread
from .pins import acks_by_message, pin_list
from .summary import channel_summary


def board_thread(conn: sqlite3.Connection, *, message_id: int) -> list[dict[str, Any]]:
    """A conversation with its bodies, votes and transitions, for reading.

    Tombstones are kept (as everywhere thread-shaped) so a deleted message
    cannot silently remove a turn from the conversation.
    """
    thread = fetch_thread(conn, message_id=message_id)
    acks = acks_by_message(conn, message_ids=[m["id"] for m in thread])
    for message in thread:
        message["acks"] = [
            {"role": r, "decision": d} for r, d in sorted(acks.get(message["id"], {}).items())
        ]
        message["events"] = fetch_events(conn, message_id=message["id"])
    return thread


def board_feed(conn: sqlite3.Connection, *, limit: int = 30) -> list[dict[str, Any]]:
    """Recent traffic WITH bodies — the channel read as correspondence."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return expand_recipients(conn, [row_to_dict(r) for r in rows])


def board_snapshot(
    conn: sqlite3.Connection, *, roles: list[str], recent_limit: int = 40
) -> dict[str, Any]:
    """Everything the read-only web board renders, in one pass."""
    per_role = {r: channel_summary(conn, role=r)["counts"] for r in roles}
    debts = [
        row_to_dict(r)
        for r in conn.execute(
            "SELECT id, from_role, to_role, topic, created_at, kind, work_status, "
            "action_required, status FROM messages "
            "WHERE action_required = 1 AND status = 'open' AND deleted_at IS NULL "
            "ORDER BY id ASC"
        ).fetchall()
    ]
    recent = [
        row_to_dict(r)
        for r in conn.execute(
            "SELECT id, from_role, to_role, topic, created_at, read_at, kind, "
            "work_status, action_required, status FROM messages "
            "WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?",
            (recent_limit,),
        ).fetchall()
    ]
    return {
        "roles": roles,
        "counts": per_role,
        "open_obligations": debts,
        "pins": pin_list(conn),
        "recent": recent,
        "totals": {
            "messages": conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE deleted_at IS NULL"
            ).fetchone()["c"],
        },
    }
