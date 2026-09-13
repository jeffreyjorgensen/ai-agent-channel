"""Debts and their lifecycle: resolve/confirm/reopen, work_status transitions,
and the lists of what a role owes, can start, or still has to decide."""

from __future__ import annotations

from typing import Any

from .. import db
from .common import (
    FIELDS_DOC,
    NOTE_MAX,
    WORK_STATUSES,
    Fields,
    check_enum,
    check_length,
    check_limit,
    current_role,
    listing,
    message_fields,
    message_listing,
    open_channel_db,
    strip_or_none,
    with_age,
    with_movement,
)
from .registry import tool

_NOTE_LIMIT_DOC = f" Notes and reasons are at most {NOTE_MAX} characters."


@tool(
    description=(
        "Mark an action_required message as resolved. Records who closed it, "
        "when, and an optional resolution note. The response lists 'unblocked' "
        "— blocked tasks that were waiting on this message; tell their owner "
        "or resume them. Resolving an already-resolved "
        "message is a no-op. Either party may resolve (always attributed via "
        "resolved_by), but the etiquette is explicit: for an action_required "
        "message the executor is the ADDRESSEE (to) — the addressee resolves "
        "with a note naming what was done; the author (from) verifies and "
        "uses reopen_message if unsatisfied, or confirm_resolution if "
        "satisfied. The surfacing is SYMMETRIC: whoever resolves, the message "
        "keeps surfacing in the OTHER participant's channel_status()."
        "resolved_for_you until they confirm or reopen — a resolve is never "
        "silent in either direction. So cancelling your own request is "
        "legitimate: resolve it yourself with a note like 'cancelled, not "
        "needed' and the addressee will see it and confirm ('understood, "
        "dropping it'). What is NOT legitimate is resolving a debt the other "
        "side owes you as if the work were done." + _NOTE_LIMIT_DOC
    )
)
def resolve_message(
    message_id: int,
    resolution_note: str | None = None,
) -> dict[str, Any]:
    role = current_role()
    check_length(resolution_note, "resolution_note", NOTE_MAX)
    with open_channel_db(write=True) as conn:
        return db.resolve_message(
            conn, message_id=message_id, role=role, resolution_note=resolution_note
        )


@tool(
    description=(
        "Confirm a resolution you verified — the closing half of the debt "
        "loop. A resolved obligation keeps surfacing in channel_status()."
        "resolved_for_you of the participant who did NOT resolve it, until "
        "that participant either confirms (this tool) or reopens. The "
        "resolver cannot confirm their own resolution. Idempotent per "
        "resolution (a reopen + re-resolve requires a fresh confirmation); "
        "logged to message_history as 'resolution_confirmed'." + _NOTE_LIMIT_DOC
    )
)
def confirm_resolution(
    message_id: int,
    note: str | None = None,
) -> dict[str, Any]:
    role = current_role()
    check_length(note, "note", NOTE_MAX)
    with open_channel_db(write=True) as conn:
        return db.confirm_resolution(conn, message_id=message_id, role=role, note=note)


@tool(
    description=(
        "Reopen a resolved action_required message. Either party may reopen "
        "(not just the author) — e.g. the executor who discovers their own "
        "fix was incomplete. Records who reopened it and why in the message "
        "history. Reopening an open message is a no-op." + _NOTE_LIMIT_DOC
    )
)
def reopen_message(
    message_id: int,
    reason: str | None = None,
) -> dict[str, Any]:
    role = current_role()
    check_length(reason, "reason", NOTE_MAX)
    with open_channel_db(write=True) as conn:
        return db.reopen_message(conn, message_id=message_id, role=role, reason=reason)


@tool(
    description=(
        "List open obligations: action_required messages with status='open' "
        "addressed to a role (defaults to your own role). These are the debts "
        "that still need resolve_message. Each carries 'age_days' (since "
        "it was raised) and 'idle_days' (since it last MOVED — a status "
        "transition, a resolve, a reopen). Idle is the number that finds "
        "forgotten work: an old debt worked on yesterday is healthy, a "
        "young one nobody has touched is not, and age alone cannot tell "
        "them apart. Nothing is ever auto-closed on either number." + FIELDS_DOC
    ),
    read_only=True,
)
def open_obligations(
    to_role: str | None = None,
    limit: int = 100,
    fields: Fields = None,
) -> dict[str, Any]:
    check_limit(limit)
    to_role = strip_or_none(to_role) or current_role()
    projection = message_fields(fields)
    with open_channel_db() as conn:
        rows = db.list_messages(
            conn,
            topic=None,
            from_role=None,
            to_role=to_role,
            unread_only=False,
            since=None,
            limit=limit + 1,
            status="open",
        )
        return listing(
            db.with_body_digest(with_movement(conn, with_age(rows))),
            limit=limit,
            fields=projection,
            generated_at=db.now(conn),
        )


@tool(
    description=(
        "Change the work_status of an EXISTING message as the work moves "
        "through its lifecycle — do not send a new message just to change "
        "status. Either party (sender or recipient) may call this; third "
        "roles are rejected. Transitions: any value can be set from any "
        "state, with ONE exception — 'done' requires the current status to be "
        "'done_local' and must be set by the OTHER role than whoever declared "
        "done_local. Semantics: 'done_local' = the executor finished on their "
        "side; 'done' = completed AND confirmed by the other role (peer "
        "confirmation — the channel cannot verify merges or production). "
        "'done' is not a dead end: if an issue resurfaces, move the status "
        "back (audited) or reopen the obligation. 'needs_you' is relative to "
        "the SETTER: it always means the ball is at the other participant "
        "than whoever set it (it surfaces in THEIR channel_status). "
        "Re-setting the current value by the other role is a real, audited "
        "transition (it moves the ball back); by the same role it is a "
        "no-op. For 'blocked' on another "
        "message, pass blocked_by=<id>; the blocker MUST be an unresolved "
        "action_required message (otherwise nothing could ever resolve it and "
        "your task would block forever — rejected). Blocked on something "
        "without a resolvable message (human decision, external run) — use "
        "'blocked' with a note and lift it manually. Every transition is "
        f"logged to message_history. Notes are at most {NOTE_MAX} characters."
    )
)
def set_work_status(
    message_id: int,
    work_status: str,
    note: str | None = None,
    blocked_by: int | None = None,
) -> dict[str, Any]:
    role = current_role()
    check_enum(work_status, WORK_STATUSES, "work_status")
    check_length(note, "note", NOTE_MAX)
    with open_channel_db(write=True) as conn:
        return db.set_work_status(
            conn,
            message_id=message_id,
            role=role,
            work_status=work_status,
            note=note,
            blocked_by=blocked_by,
        )


@tool(
    description=(
        "What you can actually START right now: open obligations addressed to "
        "you that are NOT sitting behind a live blocker, oldest first. "
        "open_obligations answers 'how much do you owe' — a number that "
        "includes work you cannot move — while this answers 'what do you pick "
        "up', which is the question at the start of a session. A task marked "
        "'blocked' whose blocker has since been resolved or deleted DOES "
        "appear here: unblocking is surfaced, never automatic, so it is your "
        "move to resume it with set_work_status. Each item carries age_days "
        "and idle_days (days since it last moved)."
    ),
    read_only=True,
)
def ready_work(
    limit: int = 50,
    fields: Fields = None,
) -> dict[str, Any]:
    role = current_role()
    check_limit(limit)
    projection = message_fields(fields)
    with open_channel_db() as conn:
        rows = db.ready_work(conn, role=role, limit=limit + 1)
        return message_listing(
            conn,
            with_movement(conn, with_age(rows)),
            limit=limit,
            fields=projection,
        )


@tool(
    description=(
        "List proposals (kind='proc') addressed to a role (defaults to yours) "
        "that still need that role's agree/reject/needs_changes. These are "
        "debts just like open_obligations; check both. "
        "Not listed: nudges (sent with about_message_id), proposals opened "
        "for reading (decision_requested=false), superseded ones, rounds "
        "whose declared 'voters' exclude the role, and ones the role has "
        "already voted on — unless the body was re-issued since. "
        "'from_role' filters by author (what you are waiting on from "
        "others); 'pin_key' narrows to one pin's round. "
        "Entries that are also action_required carry 'obligation' "
        "({status, resolved_by, resolved_at, confirmed}) — the work and the "
        "decision are closed independently. "
        "If an entry's subject no longer exists, answer it with "
        "acknowledge(decision='void')." + FIELDS_DOC
    ),
    read_only=True,
)
def awaiting_ack(
    to_role: str | None = None,
    limit: int = 100,
    fields: Fields = None,
    from_role: str | None = None,
    pin_key: str | None = None,
) -> dict[str, Any]:
    check_limit(limit)
    to_role = strip_or_none(to_role) or current_role()
    projection = message_fields(fields)
    with open_channel_db() as conn:
        rows = with_age(
            db.fetch_awaiting_ack(
                conn,
                role=to_role,
                limit=limit + 1,
                from_role=strip_or_none(from_role),
                pin_key=strip_or_none(pin_key),
            )
        )
        return message_listing(
            conn,
            db.obligation_state(conn, rows),
            limit=limit,
            fields=projection,
            generated_at=db.now(conn),
        )
