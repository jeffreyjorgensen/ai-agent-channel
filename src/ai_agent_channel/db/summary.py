"""What a role owes right now: the channel_summary counters, the ids behind
the actionable ones, and the queue of debts that can be started."""

from __future__ import annotations

import sqlite3
from typing import Any

from .fields import row_to_dict
from .messages import ADDRESSED_TO, expand_recipients
from .pins import AWAITING_ACK, pin_list

# The channel_summary() counts that mean "there is something to triage before
# you finish". One definition, because the Stop hook (blocks a stop) and
# wait_for_mail (wakes a sleeping agent) must agree on it. 'blocked' (nothing
# you can do), 'in_progress' (legitimately spans many turns) and the full
# open_obligations are out: only its 'untaken' subset, where the ball is at
# the addressee, is actionable.
ACTIONABLE_COUNTS = (
    "unread",
    "open_obligations_untaken",
    "awaiting_ack",
    "unblocked",
    "needs_you",
    "awaiting_done",
    "resolved_for_you",
)

# A blocker still blocks while it exists (not deleted) and is not resolved.
_BLOCKER_ALIVE = (
    "EXISTS (SELECT 1 FROM messages b WHERE b.id = m.blocked_by "
    "AND b.deleted_at IS NULL AND (b.status IS NULL OR b.status = 'open'))"
)

_UNREAD_FROM = (
    "FROM deliveries d JOIN messages m ON m.id = d.message_id "
    "WHERE d.to_role = ? AND d.read_at IS NULL AND m.deleted_at IS NULL"
)

# A debt nags its addressee on stops only while the BALL IS AT THEM.
# It is "taken" (not nagging) when the LAST work_status transition was
# made by the addressee themselves — in_progress/blocked (working /
# waiting), needs_you (thrown to the partner), done_local (awaiting
# their confirmation) — AND that transition is newer than the last
# reopen: a reopen resets takenness, so the executor's stop-hook says
# "take it again" instead of staying silent. Untouched debts (no
# events; a send-time status counts as the sender's move) and debts
# where the partner moved last keep blocking. Parameters: role, role.
_UNTAKEN_FROM = (
    "FROM messages m "
    "WHERE m.to_role = ? AND m.action_required = 1 AND m.status = 'open' "
    "AND m.deleted_at IS NULL "
    "AND NOT EXISTS ("
    "    SELECT 1 FROM message_events e "
    "    WHERE e.message_id = m.id AND e.event LIKE 'work_status:%' "
    "    AND e.role = ? "
    "    AND e.id = (SELECT MAX(w.id) FROM message_events w "
    "        WHERE w.message_id = m.id AND w.event LIKE 'work_status:%') "
    "    AND e.id > COALESCE((SELECT MAX(r.id) FROM message_events r "
    "        WHERE r.message_id = m.id AND r.event = 'reopen'), 0)"
    ")"
)


def ready_work(conn: sqlite3.Connection, *, role: str, limit: int) -> list[dict[str, Any]]:
    """Open debts addressed to `role` that are not behind a live blocker —
    "what can you start", as opposed to channel_summary's "how much do you
    owe". A 'blocked' debt whose blocker is resolved or deleted IS ready:
    unblocking is surfaced, never automatic."""
    rows = conn.execute(
        f"""
        SELECT * FROM messages m
        WHERE m.to_role = ? AND m.action_required = 1 AND m.status = 'open'
          AND m.deleted_at IS NULL
          -- IFNULL, not a bare comparison: work_status is NULL on most
          -- messages, and `NULL = 'blocked'` is NULL, so `NOT (...)` would
          -- be NULL too and silently drop every untagged debt.
          AND NOT (
              IFNULL(m.work_status, '') = 'blocked'
              AND (m.blocked_by IS NULL OR {_BLOCKER_ALIVE})
          )
        ORDER BY m.id ASC LIMIT ?
        """,
        (role, limit),
    ).fetchall()
    return expand_recipients(conn, [row_to_dict(r) for r in rows])


def last_movement(conn: sqlite3.Connection, *, message_ids: list[int]) -> dict[int, str]:
    """When each message last MOVED — its newest lifecycle event, or its
    creation if nothing has happened to it yet.

    Age alone cannot distinguish a debt that is being worked from one nobody
    has touched in weeks; this is the number that can.
    """
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"""
        SELECT m.id AS id,
               COALESCE(MAX(e.created_at), m.created_at) AS moved_at
        FROM messages m LEFT JOIN message_events e ON e.message_id = m.id
        WHERE m.id IN ({marks})
        GROUP BY m.id
        """,
        message_ids,
    ).fetchall()
    return {r["id"]: r["moved_at"] for r in rows}


def actionable_ids(
    conn: sqlite3.Connection, *, role: str, summary: dict[str, Any] | None = None
) -> dict[str, set[int]]:
    """The message ids behind each ACTIONABLE_COUNTS counter.

    A count can stay level while its contents change (one item read, one
    arrives); the ids cannot, which is what a waiter needs to notice.
    Pass `summary` (channel_summary for the same role, on the same
    connection) when the caller already has it, to avoid computing it twice.
    """
    if summary is None:
        summary = channel_summary(conn, role=role)

    def ids(sql: str, params: tuple[Any, ...] | dict[str, str]) -> set[int]:
        return {r[0] for r in conn.execute(sql, params).fetchall()}

    return {
        "unread": ids(f"SELECT m.id {_UNREAD_FROM}", (role,)),
        "open_obligations_untaken": ids(f"SELECT m.id {_UNTAKEN_FROM}", (role, role)),
        "awaiting_ack": ids(f"SELECT m.id FROM messages m WHERE {AWAITING_ACK}", {"role": role}),
        **{
            key: {item["id"] for item in summary[key]}
            for key in ("unblocked", "needs_you", "awaiting_done", "resolved_for_you")
        },
    }


def channel_summary(conn: sqlite3.Connection, *, role: str) -> dict[str, Any]:
    # Two numbers, because one cannot be honest: 'unread' counts messages
    # with no mark_read, which conflates "never saw it" with "read it and
    # did not mark it". The split says which is which; 'unread' stays the
    # sum so nothing that reads it today changes meaning.
    seen = conn.execute(
        "SELECT COUNT(*) AS c, "
        "COALESCE(SUM(CASE WHEN d.opened_at IS NULL THEN 1 ELSE 0 END), 0) "
        f"  AS unopened {_UNREAD_FROM}",
        (role,),
    ).fetchone()
    unread = seen["c"]
    unopened = seen["unopened"]
    opened_unmarked = unread - unopened
    # action_required is point-to-point by construction (broadcast refuses
    # it), so a plain to_role match is exact here — no deliveries join.
    open_obligations = conn.execute(
        "SELECT COUNT(*) AS c FROM messages "
        "WHERE to_role = ? AND action_required = 1 AND status = 'open' "
        "AND deleted_at IS NULL",
        (role,),
    ).fetchone()["c"]
    # The stop-hook blocks on this counter; open_obligations stays the full
    # triage count.
    open_obligations_untaken = conn.execute(
        f"SELECT COUNT(*) AS c {_UNTAKEN_FROM}", (role, role)
    ).fetchone()["c"]
    awaiting_ack = conn.execute(
        f"SELECT COUNT(*) AS c FROM messages m WHERE {AWAITING_ACK}",
        {"role": role},
    ).fetchone()["c"]
    # Per-task lists are keyed to the author of the LAST transition, not to
    # the addressee — "whose move is it" must not depend on who happens to
    # read the status. in_progress / blocked / unblocked are yours when YOU
    # made the last transition (your unfinished work to resume);
    # needs_you / awaiting_done are yours when the OTHER side made it (the
    # ball is at you). A work_status set at send time counts as set by the
    # sender (there is no event row for it).
    party = f"(m.from_role = ? OR {ADDRESSED_TO})"

    def _last_setter(event: str) -> str:
        return (
            "COALESCE((SELECT e.role FROM message_events e "
            f"WHERE e.message_id = m.id AND e.event = '{event}' "
            "ORDER BY e.id DESC LIMIT 1), m.from_role)"
        )

    def _by_setter(status: str, *, mine: bool) -> list[dict[str, Any]]:
        op = "=" if mine else "!="
        sql = (
            "SELECT m.id, m.topic FROM messages m "
            f"WHERE {party} AND m.work_status = ? AND m.deleted_at IS NULL "
            f"AND {_last_setter('work_status:' + status)} {op} ? ORDER BY m.id ASC"
        )
        return [dict(r) for r in conn.execute(sql, (role, role, role, status, role)).fetchall()]

    # Tasks split into: blocked (blocker alive, or no message blocker at
    # all — lifted manually) and unblocked (blocker gone — the agent should
    # resume them via set_work_status; nothing is automatic).
    base = (
        "SELECT m.id, m.topic, m.blocked_by FROM messages m "
        f"WHERE {party} AND m.work_status = 'blocked' AND m.deleted_at IS NULL "
        f"AND {_last_setter('work_status:blocked')} = ? AND {{cond}} "
        "ORDER BY m.id ASC"
    )
    blocked = [
        dict(r)
        for r in conn.execute(
            base.format(cond=f"(m.blocked_by IS NULL OR {_BLOCKER_ALIVE})"),
            (role, role, role, role),
        ).fetchall()
    ]
    unblocked = [
        dict(r)
        for r in conn.execute(
            base.format(cond=f"(m.blocked_by IS NOT NULL AND NOT {_BLOCKER_ALIVE})"),
            (role, role, role, role),
        ).fetchall()
    ]
    in_progress = _by_setter("in_progress", mine=True)
    needs_you = _by_setter("needs_you", mine=False)
    awaiting_done = _by_setter("done_local", mine=False)
    # Debts the other side closed that you have not verified yet: stay listed
    # until you confirm_resolution (a confirm newer than the latest resolve)
    # or reopen_message. This is the author's half of the verification loop —
    # surfaced mechanically, not left to discipline.
    resolved_for_you = [
        dict(r)
        for r in conn.execute(
            "SELECT m.id, m.topic, m.resolved_by, m.resolved_at, m.resolution_note "
            f"FROM messages m WHERE {party} AND m.action_required = 1 "
            "AND m.status = 'resolved' AND m.resolved_by IS NOT NULL "
            "AND m.resolved_by != ? AND m.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM message_events c "
            "    WHERE c.message_id = m.id AND c.event = 'resolution_confirmed' "
            "    AND c.id > COALESCE((SELECT MAX(rv.id) FROM message_events rv "
            "        WHERE rv.message_id = m.id AND rv.event = 'resolve'), 0)) "
            "ORDER BY m.id ASC",
            (role, role, role, role),
        ).fetchall()
    ]
    return {
        "role": role,
        "counts": {
            "unread": unread,
            "unopened": unopened,
            "opened_unmarked": opened_unmarked,
            "open_obligations": open_obligations,
            "open_obligations_untaken": open_obligations_untaken,
            "awaiting_ack": awaiting_ack,
            "blocked": len(blocked),
            "unblocked": len(unblocked),
            "in_progress": len(in_progress),
            "needs_you": len(needs_you),
            "awaiting_done": len(awaiting_done),
            "resolved_for_you": len(resolved_for_you),
        },
        "unread": unread,
        "open_obligations": open_obligations,
        "awaiting_ack": awaiting_ack,
        "blocked": blocked,
        "unblocked": unblocked,
        "in_progress": in_progress,
        "needs_you": needs_you,
        "awaiting_done": awaiting_done,
        "resolved_for_you": resolved_for_you,
        "pins": pin_list(conn),
    }
