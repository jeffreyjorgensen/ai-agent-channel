"""Messages: addressing (point-to-point and broadcast), sending, delivery and
read state, listing, threads, replies and the event log.

Broadcast is for proposals, where everyone is the genuine addressee.
Obligations stay point-to-point: action_required cannot be broadcast, so the
two-party resolve/confirm loop is never shared among several roles."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .connection import atomic
from .fields import row_to_dict
from .schema import BROADCAST, NOW_SQL


def addressed_to(param: str) -> str:
    """SQL for "message m is addressed to the role bound to :param", over
    both storage shapes."""
    return (
        f"(m.to_role = :{param} OR EXISTS (SELECT 1 FROM message_recipients mr "
        f"WHERE mr.message_id = m.id AND mr.to_role = :{param}))"
    )


# Positional form of addressed_to(), for `?` queries: bind the role twice.
ADDRESSED_TO = addressed_to("role").replace(":role", "?")


def like_contains(text: str) -> str:
    """A LIKE pattern, used with ESCAPE '\\', that matches `text` literally
    anywhere: '%' and '_' in the text are not wildcards."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def message_filters(
    *,
    from_role: str | None = None,
    to_role: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    work_status: str | None = None,
    pin_key: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """WHERE clauses over alias `m` (live messages only) and their named
    parameters, for the exact-match filters listings share."""
    clauses = ["m.deleted_at IS NULL"]
    params: dict[str, Any] = {}
    exact = {
        "from_role": from_role,
        "kind": kind,
        "status": status,
        "work_status": work_status,
        "pin_key": pin_key,
    }
    for column, value in exact.items():
        if value is not None:
            clauses.append(f"m.{column} = :{column}")
            params[column] = value
    if to_role is not None:
        clauses.append(addressed_to("to_role"))
        params["to_role"] = to_role
    return clauses, params


def recipients_of(conn: sqlite3.Connection, message_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT to_role FROM message_recipients WHERE message_id = ? ORDER BY to_role",
        (message_id,),
    ).fetchall()
    return [r["to_role"] for r in rows]


def expand_recipients(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the '*' placeholder with the real recipient list, and decode
    the addenda. Callers see `to` as a string for point-to-point and a list
    for a broadcast — the shape says which it is."""
    for row in rows:
        if row.get("to") == BROADCAST:
            row["to"] = recipients_of(conn, row["id"])
        if isinstance(row.get("addenda"), str):
            row["addenda"] = json.loads(row["addenda"])
    return rows


def is_addressed_to(msg: dict[str, Any], role: str) -> bool:
    """`to` is a string for point-to-point and a list for a broadcast."""
    return role in msg["to"] if isinstance(msg["to"], list) else msg["to"] == role


def require_party(msg: dict[str, Any], role: str, verb: str) -> None:
    """Refuse unless `role` sent or received the message."""
    if msg["from"] != role and not is_addressed_to(msg, role):
        raise PermissionError(
            f"message {msg['id']} is between '{msg['from']}' and '{msg['to']}', "
            f"cannot be {verb} by '{role}'"
        )


def insert_event(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    event: str,
    role: str,
    note: str | None,
    pin_keys: list[str] | None = None,
) -> None:
    """Append to a message's event log. `pin_keys` stores the pin keys the
    event was made under (a cleanup pass) as a JSON array."""
    conn.execute(
        "INSERT INTO message_events (message_id, event, role, note, pin_keys) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            message_id,
            event,
            role,
            note,
            None if pin_keys is None else json.dumps(sorted(pin_keys), ensure_ascii=False),
        ),
    )


def fetch_events(conn: sqlite3.Connection, *, message_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT message_id, event, role, note, created_at FROM message_events "
        "WHERE message_id = ? ORDER BY id ASC",
        (message_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@atomic
def insert_message(
    conn: sqlite3.Connection,
    *,
    from_role: str,
    to_role: str,
    topic: str,
    body: str,
    action_required: bool,
    reply_to: int | None,
    kind: str | None = None,
    work_status: str | None = None,
    pin_key: str | None = None,
    about_message_id: int | None = None,
    recipients: list[str] | None = None,
    addenda: dict[str, str] | None = None,
    decision_requested: bool = True,
    voters: list[str] | None = None,
) -> dict[str, Any]:
    cur = conn.execute(
        """
        INSERT INTO messages
            (from_role, to_role, topic, body, action_required, reply_to,
             status, kind, work_status, pin_key, about_message_id, addenda,
             decision_requested, voters)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id, created_at
        """,
        (
            from_role,
            to_role,
            topic,
            body,
            1 if action_required else 0,
            reply_to,
            "open" if action_required else None,
            kind,
            work_status,
            pin_key,
            about_message_id,
            json.dumps(addenda, ensure_ascii=False) if addenda else None,
            1 if decision_requested else 0,
            json.dumps(voters, ensure_ascii=False) if voters else None,
        ),
    )
    row = cur.fetchone()
    if recipients:
        conn.executemany(
            "INSERT INTO message_recipients (message_id, to_role) VALUES (?, ?)",
            [(row["id"], r) for r in recipients],
        )
    if work_status is not None:
        # A status set at send time is the sender's transition; record it so
        # the event log agrees with the COALESCE(..., from_role) fallbacks.
        insert_event(
            conn,
            message_id=row["id"],
            event=f"work_status:{work_status}",
            role=from_role,
            note="set at send time",
        )
    out = {"id": row["id"], "created_at": row["created_at"]}
    if recipients:
        out["recipients"] = list(recipients)
    if voters:
        out["voters"] = list(voters)
    return out


def fetch_inbox(
    conn: sqlite3.Connection,
    *,
    role: str,
    unread_only: bool,
    limit: int,
    mark_opened: bool = False,
) -> list[dict[str, Any]]:
    """The newest `limit` messages delivered to `role`, oldest first. Read
    state comes from `deliveries` (per recipient for a broadcast)."""
    inner = (
        "SELECT m.*, d.read_at AS d_read_at, d.opened_at AS d_opened_at "
        "FROM messages m JOIN deliveries d "
        "ON d.message_id = m.id AND d.to_role = ? "
        "WHERE m.deleted_at IS NULL"
    )
    params: list[Any] = [role]
    if unread_only:
        inner += " AND d.read_at IS NULL"
    inner += " ORDER BY m.id DESC LIMIT ?"
    params.append(limit)
    sql = f"SELECT * FROM ({inner}) ORDER BY id ASC"
    rows = []
    for raw in conn.execute(sql, params).fetchall():
        row = row_to_dict(raw)
        row["read_at"] = row.pop("d_read_at")
        row["opened_at"] = row.pop("d_opened_at")
        rows.append(row)
    expand_recipients(conn, rows)
    for row in rows:
        # each recipient sees the shared body plus their own tail, if any
        if isinstance(row.get("addenda"), dict):
            row["addendum"] = row["addenda"].get(role)
    if mark_opened:
        mark_delivered(conn, role=role, message_ids=[r["id"] for r in rows])
    return rows


@atomic
def mark_delivered(conn: sqlite3.Connection, *, role: str, message_ids: list[int]) -> None:
    """Record that these rows were handed to the role (opened_at).

    Distinct from mark_read, which is the role's own statement that it dealt
    with the message. Separate from fetch_inbox so a caller that reads
    limit+1 rows to detect truncation marks only the rows it shows.
    """
    if not message_ids:
        return
    marks = ",".join("?" * len(message_ids))
    conn.execute(
        f"UPDATE messages SET opened_at = {NOW_SQL} "
        f"WHERE id IN ({marks}) AND to_role = ? AND opened_at IS NULL",
        [*message_ids, role],
    )
    conn.execute(
        f"UPDATE message_recipients SET opened_at = {NOW_SQL} "
        f"WHERE message_id IN ({marks}) AND to_role = ? AND opened_at IS NULL",
        [*message_ids, role],
    )


def fetch_message(conn: sqlite3.Connection, message_id: int) -> dict[str, Any] | None:
    # Soft-deleted messages are invisible to every operation except
    # fetch_thread, which keeps them as tombstones so threads never break.
    row = conn.execute(
        "SELECT * FROM messages WHERE id = ? AND deleted_at IS NULL", (message_id,)
    ).fetchone()
    if row is None:
        return None
    return expand_recipients(conn, [row_to_dict(row)])[0]


def delivery_read_at(conn: sqlite3.Connection, *, message_id: int, role: str) -> str | None:
    row = conn.execute(
        "SELECT read_at FROM deliveries WHERE message_id = ? AND to_role = ?",
        (message_id, role),
    ).fetchone()
    return row["read_at"] if row else None


@atomic
def mark_read(conn: sqlite3.Connection, *, message_id: int, role: str) -> dict[str, Any]:
    msg = fetch_message(conn, message_id)
    if msg is None:
        raise ValueError(f"message {message_id} not found")
    if not is_addressed_to(msg, role):
        raise PermissionError(f"message {message_id} is addressed to '{msg['to']}', not '{role}'")
    already = delivery_read_at(conn, message_id=message_id, role=role)
    if already is not None:
        return {"id": message_id, "read_at": already, "already_read": True}
    # One of the two updates is a no-op depending on the storage shape; the
    # read_at read-back afterwards works for both.
    conn.execute(
        f"UPDATE messages SET read_at = {NOW_SQL} WHERE id = ? AND to_role = ? AND read_at IS NULL",
        (message_id, role),
    )
    conn.execute(
        f"UPDATE message_recipients SET read_at = {NOW_SQL} "
        f"WHERE message_id = ? AND to_role = ? AND read_at IS NULL",
        (message_id, role),
    )
    return {
        "id": message_id,
        "read_at": delivery_read_at(conn, message_id=message_id, role=role),
        "already_read": False,
    }


def normalize_timestamp(value: str, *, field: str) -> str:
    """An ISO-8601 instant in the stored format, so it compares as a string.

    Accepts 'T' or a space between date and time, 'Z' or a numeric offset,
    and a bare date. A value without an offset is taken as UTC.
    """
    try:
        when = datetime.fromisoformat((value or "").strip())
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        # An offset can push year 1 or year 9999 out of datetime's range.
        when = when.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError(
            f"'{field}' must be an ISO-8601 timestamp such as 2026-08-09T10:00:00Z "
            f"(years 1-9999 in UTC), got {value!r}"
        ) from None
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def list_messages(
    conn: sqlite3.Connection,
    *,
    topic: str | None,
    from_role: str | None,
    to_role: str | None,
    unread_only: bool,
    since: str | None,
    limit: int,
    status: str | None = None,
    kind: str | None = None,
    work_status: str | None = None,
    text: str | None = None,
    pin_key: str | None = None,
) -> list[dict[str, Any]]:
    """Unranked filter over live messages, newest first.

    `topic` and `text` are literal substrings, ASCII case-insensitive
    ('%' and '_' are not wildcards). `pin_key` matches the structural
    field only: a message that mentions the key in prose is not a proposal
    for it.
    """
    clauses, params = message_filters(
        from_role=from_role,
        to_role=to_role,
        kind=kind,
        status=status,
        work_status=work_status,
        pin_key=pin_key,
    )
    if topic is not None:
        clauses.append("m.topic LIKE :topic ESCAPE '\\'")
        params["topic"] = like_contains(topic)
    if text is not None:
        clauses.append("(m.topic LIKE :text ESCAPE '\\' OR m.body LIKE :text ESCAPE '\\')")
        params["text"] = like_contains(text)
    if unread_only:
        # Unread is per recipient; without a to_role there is no "whose
        # unread", so this keeps meaning the message row's own state.
        if to_role is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM deliveries d WHERE d.message_id = "
                "m.id AND d.to_role = :to_role AND d.read_at IS NULL)"
            )
        else:
            clauses.append("m.read_at IS NULL")
    if since is not None:
        clauses.append("m.created_at >= :since")
        params["since"] = normalize_timestamp(since, field="since")
    params["limit"] = limit
    sql = (
        f"SELECT m.* FROM messages m WHERE {' AND '.join(clauses)} ORDER BY m.id DESC LIMIT :limit"
    )
    return expand_recipients(conn, [row_to_dict(r) for r in conn.execute(sql, params).fetchall()])


def find_reply(
    conn: sqlite3.Connection,
    *,
    parent_id: int,
    role: str,
    after_id: int | None = None,
    include_read: bool = False,
) -> dict[str, Any] | None:
    """The next reply to `parent_id` this role has not seen yet.

    Unread by default, and `after_id` skips replies already handed over, so
    repeated waits move forward instead of returning the same message.
    Read state comes from `deliveries`, not from the message row: on a
    broadcast the row's own read_at is meaningless.
    """
    clauses = ["m.reply_to = ?", ADDRESSED_TO, "m.deleted_at IS NULL"]
    params: list[Any] = [parent_id, role, role]
    if after_id is not None:
        clauses.append("m.id > ?")
        params.append(after_id)
    if not include_read:
        clauses.append(
            "EXISTS (SELECT 1 FROM deliveries d WHERE d.message_id = m.id "
            "AND d.to_role = ? AND d.read_at IS NULL)"
        )
        params.append(role)
    row = conn.execute(
        f"SELECT * FROM messages m WHERE {' AND '.join(clauses)} ORDER BY m.id ASC LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    out = expand_recipients(conn, [row_to_dict(row)])[0]
    out["read_at"] = delivery_read_at(conn, message_id=out["id"], role=role)
    return out


def fetch_thread(conn: sqlite3.Connection, *, message_id: int) -> list[dict[str, Any]]:
    root = conn.execute(
        """
        WITH RECURSIVE up(id, reply_to) AS (
            SELECT id, reply_to FROM messages WHERE id = ?
            UNION ALL
            SELECT m.id, m.reply_to FROM messages m JOIN up ON m.id = up.reply_to
        )
        SELECT id FROM up WHERE reply_to IS NULL
        """,
        (message_id,),
    ).fetchone()
    if root is None:
        return []
    rows = conn.execute(
        """
        WITH RECURSIVE down(id) AS (
            SELECT ?
            UNION ALL
            SELECT m.id FROM messages m JOIN down ON m.reply_to = down.id
        )
        SELECT * FROM messages WHERE id IN (SELECT id FROM down) ORDER BY id ASC
        """,
        (root["id"],),
    ).fetchall()
    return expand_recipients(conn, [row_to_dict(r) for r in rows])


def observed_roles(conn: sqlite3.Connection) -> list[str]:
    """Roles that have sent or received something in this mailbox.

    The stdio-mode fallback when there is no channel registry; a member who
    has never spoken is not visible here.
    """
    rows = conn.execute(
        "SELECT DISTINCT from_role AS r FROM messages WHERE deleted_at IS NULL "
        "UNION SELECT DISTINCT to_role FROM messages WHERE deleted_at IS NULL "
        # A role that has only received broadcasts appears only here.
        "UNION SELECT DISTINCT mr.to_role FROM message_recipients mr "
        "  JOIN messages m ON m.id = mr.message_id WHERE m.deleted_at IS NULL "
        "ORDER BY r"
    ).fetchall()
    return [r["r"] for r in rows if r["r"] and r["r"] != BROADCAST]
