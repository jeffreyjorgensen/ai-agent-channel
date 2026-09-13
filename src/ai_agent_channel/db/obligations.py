"""What happens to a message after it is sent: the two-party resolve / confirm
/ reopen loop, work status, re-issuing a proposal's body, and deletion."""

from __future__ import annotations

import sqlite3
from typing import Any

from .blobs import body_digest
from .connection import atomic
from .messages import fetch_message, insert_event, require_party
from .pins import approval_used, fetch_acknowledgements
from .schema import NOW_SQL
from .supersede import cascade_to_nudges


def _resolution_confirmed(conn: sqlite3.Connection, *, message_id: int) -> bool:
    """True when the latest resolve has a resolution_confirmed event after it
    (a reopen + re-resolve resets the clock and requires a fresh confirm)."""
    last_resolve = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS i FROM message_events "
        "WHERE message_id = ? AND event = 'resolve'",
        (message_id,),
    ).fetchone()["i"]
    row = conn.execute(
        "SELECT 1 FROM message_events WHERE message_id = ? "
        "AND event = 'resolution_confirmed' AND id > ? LIMIT 1",
        (message_id, last_resolve),
    ).fetchone()
    return row is not None


@atomic
def resolve_message(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    role: str,
    resolution_note: str | None,
) -> dict[str, Any]:
    msg = fetch_message(conn, message_id)
    if msg is None:
        raise ValueError(f"message {message_id} not found")
    require_party(msg, role, "resolved")
    if not msg["action_required"]:
        raise ValueError(f"message {message_id} has action_required=false, nothing to resolve")
    if msg["status"] == "resolved":
        return {
            "id": message_id,
            "status": "resolved",
            "resolved_by": msg["resolved_by"],
            "resolved_at": msg["resolved_at"],
            "already_resolved": True,
        }
    cur = conn.execute(
        "UPDATE messages SET status = 'resolved', resolved_by = ?, "
        f"resolved_at = {NOW_SQL}, resolution_note = ? "
        "WHERE id = ? RETURNING resolved_at",
        (role, resolution_note, message_id),
    )
    row = cur.fetchone()
    insert_event(conn, message_id=message_id, event="resolve", role=role, note=resolution_note)
    # Surface tasks that were waiting on this message as their blocker.
    unblocked = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM messages WHERE blocked_by = ? AND work_status = 'blocked' "
            "AND deleted_at IS NULL ORDER BY id ASC",
            (message_id,),
        ).fetchall()
    ]
    return {
        "id": message_id,
        "status": "resolved",
        "resolved_by": role,
        "resolved_at": row["resolved_at"],
        "already_resolved": False,
        "unblocked": unblocked,
    }


@atomic
def reopen_message(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    role: str,
    reason: str | None,
) -> dict[str, Any]:
    msg = fetch_message(conn, message_id)
    if msg is None:
        raise ValueError(f"message {message_id} not found")
    require_party(msg, role, "reopened")
    if not msg["action_required"]:
        raise ValueError(f"message {message_id} has action_required=false, nothing to reopen")
    if msg["status"] == "open":
        return {"id": message_id, "status": "open", "already_open": True}
    conn.execute(
        "UPDATE messages SET status = 'open', resolved_by = NULL, "
        "resolved_at = NULL, resolution_note = NULL WHERE id = ?",
        (message_id,),
    )
    insert_event(conn, message_id=message_id, event="reopen", role=role, note=reason)
    return {"id": message_id, "status": "open", "already_open": False}


@atomic
def confirm_resolution(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    role: str,
    note: str | None,
) -> dict[str, Any]:
    """The verification half of the resolve loop: the non-resolving party
    confirms the resolution. Until then (or until a reopen) the message keeps
    surfacing in that party's channel_status().resolved_for_you."""
    msg = fetch_message(conn, message_id)
    if msg is None:
        raise ValueError(f"message {message_id} not found")
    require_party(msg, role, "confirmed")
    if not msg["action_required"]:
        raise ValueError(f"message {message_id} has action_required=false, nothing to confirm")
    if msg["status"] != "resolved":
        raise ValueError(
            f"message {message_id} is not resolved — confirm_resolution follows resolve_message"
        )
    if msg["resolved_by"] == role:
        raise PermissionError(
            f"'{role}' resolved message {message_id} and cannot also confirm "
            f"their own resolution — the other participant verifies"
        )
    # Idempotent per resolution: a confirm newer than the latest resolve event
    # already covers this resolution (a reopen+re-resolve resets the clock).
    if _resolution_confirmed(conn, message_id=message_id):
        return {"id": message_id, "already_confirmed": True}
    insert_event(conn, message_id=message_id, event="resolution_confirmed", role=role, note=note)
    return {"id": message_id, "already_confirmed": False}


@atomic
def set_work_status(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    role: str,
    work_status: str,
    note: str | None,
    blocked_by: int | None,
) -> dict[str, Any]:
    msg = fetch_message(conn, message_id)
    if msg is None:
        raise ValueError(f"message {message_id} not found")
    require_party(msg, role, "updated")
    old = msg["work_status"]
    if work_status == "done":
        if old != "done_local":
            raise ValueError(
                "'done' confirms a declared done_local: current work_status "
                "must be 'done_local' (the executor declares done_local first)"
            )
        row = conn.execute(
            "SELECT role FROM message_events WHERE message_id = ? "
            "AND event = 'work_status:done_local' ORDER BY id DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        done_local_by = row["role"] if row else msg["from"]
        if done_local_by == role:
            raise PermissionError(
                "'done' must be confirmed by the other side — the role that "
                "declared done_local cannot also confirm it"
            )
    if blocked_by is not None:
        if work_status != "blocked":
            raise ValueError("'blocked_by' is only valid with work_status='blocked'")
        blocker = fetch_message(conn, blocked_by)
        if blocker is None:
            raise ValueError(f"blocked_by message {blocked_by} not found")
        if not blocker["action_required"]:
            raise ValueError(
                f"blocked_by message {blocked_by} is not action_required — "
                f"nothing can ever resolve it, so it would block forever; "
                f"for blockers without a resolvable message use 'blocked' "
                f"with a note and lift it manually"
            )
        if blocker["status"] == "resolved":
            raise ValueError(f"blocked_by message {blocked_by} is already resolved — not a blocker")
    if old == work_status and msg["blocked_by"] == blocked_by:
        # Same value re-set by the same side is an idempotent retry — UNLESS
        # the debt was reopened since: then it is an explicit re-take that
        # must hit the audit trail (and reset the stop-hook's "taken" state).
        # The OTHER side re-setting it is always a real transition: for
        # needs_you/done_local the last setter determines whose ball it is.
        last = conn.execute(
            "SELECT id, role FROM message_events WHERE message_id = ? AND event = ? "
            "ORDER BY id DESC LIMIT 1",
            (message_id, f"work_status:{work_status}"),
        ).fetchone()
        last_reopen = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS i FROM message_events "
            "WHERE message_id = ? AND event = 'reopen'",
            (message_id,),
        ).fetchone()["i"]
        last_setter = last["role"] if last else msg["from"]
        last_id = last["id"] if last else 0
        if last_setter == role and last_id >= last_reopen:
            return {"id": message_id, "work_status": work_status, "already_set": True}
    conn.execute(
        "UPDATE messages SET work_status = ?, blocked_by = ? WHERE id = ?",
        (work_status, blocked_by if work_status == "blocked" else None, message_id),
    )
    insert_event(
        conn,
        message_id=message_id,
        event=f"work_status:{work_status}",
        role=role,
        note=note,
    )
    return {
        "id": message_id,
        "work_status": work_status,
        "previous": old,
        "already_set": False,
    }


@atomic
def revise_message(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    role: str,
    body: str,
    topic: str | None,
    note: str | None,
) -> dict[str, Any]:
    """Re-issue the body of a proposal on the SAME id, so an iterating round
    stays one record instead of a new message plus nudges per edit.

    Votes cast on the previous text are QUENCHED, not deleted: they stay on
    record (flagged stale) and stop counting toward 'agreed', because they
    agreed to something else. Consent stays bound to the exact bytes it was
    given for.
    """
    msg = fetch_message(conn, message_id)
    if msg is None:
        raise ValueError(f"message {message_id} not found")
    if msg["from"] != role:
        raise PermissionError(
            f"message {message_id} was sent by '{msg['from']}' — only the "
            f"author re-issues its body; reply or propose your own instead"
        )
    if approval_used(conn, message_id=message_id):
        raise ValueError(
            f"message {message_id} is the approval record for a pin version — "
            f"re-issuing its body would rewrite the text a pin says it was "
            f"approved against; propose the next change as a new message"
        )
    if not body:
        raise ValueError("'body' is required")
    topic = topic if topic is not None else msg["topic"]
    if body == msg["body"] and topic == msg["topic"]:
        return {
            "id": message_id,
            "unchanged": True,
            "revised_at": msg["revised_at"],
            **body_digest(body),
        }
    before = body_digest(msg["body"])
    after = body_digest(body)
    quenched = sorted(
        a["role"] for a in fetch_acknowledgements(conn, message_id=message_id) if not a["stale"]
    )
    row = conn.execute(
        "UPDATE messages SET body = ?, topic = ?, "
        f"revised_at = {NOW_SQL} "
        "WHERE id = ? RETURNING revised_at",
        (body, topic, message_id),
    ).fetchone()
    insert_event(
        conn,
        message_id=message_id,
        event="revision",
        role=role,
        note=(
            f"body {before['body_sha256']} ({before['body_length_bytes']} B) "
            f"→ {after['body_sha256']} ({after['body_length_bytes']} B)"
            + (f"; {note}" if note else "")
        ),
    )
    return {
        "id": message_id,
        "unchanged": False,
        "revised_at": row["revised_at"],
        "previous_body_sha256": before["body_sha256"],
        "quenched_votes": quenched,
        **after,
    }


@atomic
def delete_message(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    role: str,
) -> dict[str, Any]:
    msg = fetch_message(conn, message_id)
    if msg is None:
        raise ValueError(f"message {message_id} not found")
    require_party(msg, role, "deleted")
    # A deletion is a tombstone for everyone. A proposal is a shared decision,
    # so only its author may withdraw it; a recipient answers with a vote.
    if msg["kind"] == "proc" and msg["from"] != role:
        raise PermissionError(
            f"message {message_id} is a proposal by '{msg['from']}'; only its author "
            f"can delete it. Vote on it instead (acknowledge, 'void' if its "
            f"subject is gone)"
        )
    if approval_used(conn, message_id=message_id):
        raise ValueError(
            f"message {message_id} is the approval record for a pin version and cannot be deleted"
        )
    if msg["action_required"] and msg["status"] == "open":
        raise ValueError(
            f"message {message_id} is an open obligation — resolve_message it "
            f"first; deletion must not silently close debts"
        )
    # The verification window is protected too: a resolved-but-unconfirmed
    # debt still sits in the other side's resolved_for_you — deleting it now
    # would silently clear that pending verification.
    if (
        msg["action_required"]
        and msg["status"] == "resolved"
        and not _resolution_confirmed(conn, message_id=message_id)
    ):
        raise ValueError(
            f"message {message_id} is resolved but not yet confirmed — the "
            f"other side still sees it in resolved_for_you; confirm_resolution "
            f"(or reopen_message) first, deletion must not silently clear "
            f"pending verification"
        )
    # Soft delete: the row becomes a tombstone — invisible to inbox/search/
    # filters, but still present in fetch_thread so reply chains stay intact.
    # Acks and events are kept as history.
    conn.execute(
        f"UPDATE messages SET deleted_at = {NOW_SQL} WHERE id = ?",
        (message_id,),
    )
    superseded = cascade_to_nudges(
        conn,
        target_ids=[message_id],
        role=role,
        note=f"target message {message_id} was deleted",
    )
    return {"id": message_id, "deleted": True, "superseded": superseded}


def obligation_state(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate proposals that are ALSO action_required with their debt state.

    The vote and the debt are independent on purpose — a resolve must not
    cancel the other side's obligation to say yes or no — but a listing of
    one must still show the state of the other.
    """
    for row in rows:
        if not row.get("action_required"):
            row["obligation"] = None
            continue
        row["obligation"] = {
            "status": row.get("status"),
            "resolved_by": row.get("resolved_by"),
            "resolved_at": row.get("resolved_at"),
            "confirmed": (
                row.get("status") == "resolved"
                and _resolution_confirmed(conn, message_id=row["id"])
            ),
        }
    return rows
