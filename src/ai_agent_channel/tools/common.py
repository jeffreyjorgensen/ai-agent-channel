"""Shared by every tool module: caller identity and database, limits and
vocabularies, listing envelopes, and the consent/age annotations listings carry."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from .. import auth, db

ROLE_ENV = "AI_AGENT_CHANNEL_ROLE"
TOPIC_MAX = 80
PIN_KEY_MAX = 80
LIMIT_MAX = 1000

# Free-text fields other than message and pin bodies (bodies are bounded by
# the upload limit instead). Generous for what each field is, small enough
# that no single call can bloat every listing that carries the field.
TITLE_MAX = 200
VERSION_MAX = 80
NOTE_MAX = 4000  # note, reason, resolution_note, addenda values
LABEL_MAX = 80
FILTER_MAX = 200  # list_messages topic/text substring filters

KINDS = ("bug", "feat", "proc", "status", "question", "answer")
WORK_STATUSES = ("proposed", "in_progress", "done_local", "needs_you", "done", "blocked")
STATUSES = ("open", "resolved")
# 'void': what the decision was about no longer exists (e.g. the edition a
# proposal points at was replaced), so neither agreeing nor refusing is true.
DECISIONS = ("agree", "reject", "needs_changes", "void")

# Appended to the description of every tool that takes 'fields'.
FIELDS_DOC = (
    " Optional 'fields' projects the response: a list of field names, or the "
    "single value 'headers' for the usual listing set (everything except the "
    "bodies). Omit it and the full record comes back. Use "
    "it when a listing over a long history would otherwise be too large to "
    "return — bodies dominate the size, and a 'which messages' question "
    "rarely needs them; fetch the ones you want individually afterwards."
)


def current_identity() -> auth.Identity | None:
    """HTTP-mode identity (set by the auth middleware), None in stdio mode."""
    return auth.CURRENT_IDENTITY.get()


def current_role() -> str:
    ident = current_identity()
    if ident is not None:
        if ident.role is not None:
            return ident.role
        raise PermissionError(
            "the admin token has no mailbox role — connect with a channel "
            "role token to use mailbox tools"
        )
    role = os.environ.get(ROLE_ENV, "").strip()
    if not role:
        raise ValueError(
            f"{ROLE_ENV} is not set. Configure the env var in your MCP server "
            f"config (e.g. 'frontend' or 'backend') so the channel knows who you are."
        )
    return role


@contextmanager
def open_channel_db(*, write: bool = False) -> Iterator[sqlite3.Connection]:
    """The current caller's mailbox: the token's channel DB over HTTP, the
    env/default DB over stdio.

    write=True runs the whole block in one transaction holding the write
    lock, so a tool's checks and the writes they justify cannot interleave
    with another session's, and a refusal half-way leaves nothing behind.
    """
    ident = current_identity()
    path = ident.db_path if ident is not None else None
    with db.open_db(path) as conn:
        if write:
            with db.transaction(conn):
                yield conn
        else:
            yield conn


def check_limit(limit: int) -> None:
    if limit < 1 or limit > LIMIT_MAX:
        raise ValueError(f"'limit' must be between 1 and {LIMIT_MAX}")


def check_length(value: str | None, field: str, maximum: int) -> None:
    """Refuse a free-text argument longer than `maximum` characters."""
    if value is not None and len(value) > maximum:
        raise ValueError(f"'{field}' must be <= {maximum} characters (got {len(value)})")


def strip_or_none(value: str | None) -> str | None:
    """A trimmed string, or None for a missing or blank one."""
    return (value or "").strip() or None


def require_admin() -> None:
    ident = current_identity()
    if ident is None or not ident.is_admin:
        raise PermissionError(
            "management tools require the admin token over the HTTP transport "
            "(stdio mode has a single implicit channel — nothing to manage)"
        )


def check_enum(value: str | None, allowed: tuple[str, ...], field: str) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"'{field}' must be one of {list(allowed)}, got '{value}'")


# A list of field names or the bare string 'headers'. The annotation must admit
# both: clients validate arguments against the schema generated from it.
Fields = list[str] | str | None


def message_fields(fields: Fields) -> tuple[str, ...] | None:
    return db.resolve_fields(fields, allowed=db.MESSAGE_FIELDS, preset=db.MESSAGE_HEADERS)


def listing(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    fields: tuple[str, ...] | None = None,
    keep: str = "head",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Wrap rows in the {"result": [...]} envelope, marking a truncated answer.

    Pass rows fetched with limit+1: the surplus row only proves truncation and
    is dropped. The marker sits beside the rows, never inside them. keep='tail'
    for a window anchored at the newest rows and returned oldest-first
    (read_inbox), so the row dropped is the oldest, never the freshest.
    """
    truncated = len(rows) > limit
    if truncated:
        rows = rows[-limit:] if keep == "tail" else rows[:limit]
    out: dict[str, Any] = {"result": db.project(rows, fields)}
    # a snapshot names the moment it is true for, so listings compare by cut-off
    if generated_at is not None:
        out["generated_at"] = generated_at
    if truncated:
        out["truncated"] = {
            "limit": limit,
            "returned": len(rows),
            "note": (
                "more rows match than were returned — raise 'limit' or narrow "
                "the filters; this answer is a window, not the whole set"
            ),
        }
    return out


def message_listing(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    limit: int,
    fields: tuple[str, ...] | None,
    keep: str = "head",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """A message listing with vote tallies and body digests attached."""
    return listing(
        db.with_body_digest(annotate_acks(conn, rows)),
        limit=limit,
        fields=fields,
        keep=keep,
        generated_at=generated_at,
    )


def registered_members(role: str | None = None) -> list[str] | None:
    """The channel's full roster from the token registry, or None where there
    is no registry to ask (stdio mode, or a token without a roster). `role`
    defaults to the caller's own."""
    ident = current_identity()
    if ident is None or not ident.peers:
        return None
    me = role if role is not None else ident.role
    if me is None:
        return None
    return sorted({me, *ident.peers})


def channel_roles(conn: sqlite3.Connection) -> tuple[list[str], str]:
    """(roles, where they came from). The token registry knows the real
    membership; stdio mode has no registry, so it falls back to whoever has
    appeared in the mailbox — which can only under-report, never invent."""
    members = registered_members()
    if members is not None:
        return members, "channel-registry"
    return db.observed_roles(conn), "observed-in-messages"


def body_or_ref(body: str, body_ref: int | None) -> str:
    """A body given inline, or the text of a sealed upload.

    Two ways in, one value out — so every rule that reads a body (digests,
    revision quenching, pin approval) sees the same thing whichever route
    the text took.
    """
    if body_ref is None:
        return body
    if body:
        raise ValueError(
            "pass either 'body' or 'body_ref', not both — two sources for one "
            "text is exactly the drift this channel refuses everywhere else"
        )
    with open_channel_db() as conn:
        return db.blob_body(conn, upload_id=body_ref)


def voters_of(row: dict[str, Any]) -> list[str]:
    """A message's addressees — who a decision is actually being asked of.

    `to` is a string for point-to-point and a list for a multi-recipient
    message; both shapes answer the same question.
    """
    to = row.get("to")
    return list(to) if isinstance(to, list) else ([to] if to else [])


def electorate_of(row: dict[str, Any]) -> list[str]:
    """Who this round NEEDS — the electorate it declared, else its addressees.

    Kept apart from `voters_of` on purpose: that one answers "who was this
    written to", which is what decides whether a reply may keep a wide
    audience. Collapsing the two would let a proposal that narrowed its
    electorate also narrow who is allowed to answer it.
    """
    declared = row.get("voters")
    return list(declared) if declared else voters_of(row)


def annotate_acks(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the consent tally to every proposal in a listing.

    Every view of a proposal carries it, so an 'agree' written in prose is
    always read next to the votes actually on record.
    """
    proposals = [r for r in rows if r.get("kind") == "proc" and "id" in r]
    if not proposals:
        return rows
    ids = [r["id"] for r in proposals]
    acks = db.acks_by_message(conn, message_ids=ids)
    stale = db.acks_by_message(conn, message_ids=ids, stale=True)
    for row in proposals:
        row["acks"] = db.ack_tally(
            acks.get(row["id"], {}),
            author=row.get("from", ""),
            voters=electorate_of(row),
            stale=stale.get(row["id"]),
            declared=bool(row.get("voters")),
        )
    return rows


def _days_since(stamp: str | None) -> int:
    now = datetime.now(UTC)
    if stamp is None:
        return 0
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return 0
    return max(0, (now - when).days)


def with_age(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate debt lists with age_days so stale items stand out."""
    for m in messages:
        m["age_days"] = _days_since(m.get("created_at"))
    return messages


def with_movement(conn: sqlite3.Connection, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add idle_days: days since the debt last moved (a transition, resolve or
    reopen), not since it was created. Idle, not age, finds forgotten work."""
    moved = db.last_movement(conn, message_ids=[m["id"] for m in messages if "id" in m])
    for m in messages:
        mid: int | None = m.get("id")
        stamp = moved.get(mid) if mid is not None else None
        m["idle_days"] = _days_since(stamp or m.get("created_at"))
    return messages
