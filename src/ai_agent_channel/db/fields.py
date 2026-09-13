"""Row shaping: sqlite rows to wire dicts, and opt-in field projection.

Projection keeps listings small enough for an MCP response once bodies grow;
it never changes the default shape — asking for everything returns everything."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# Wire names (post row_to_dict), not column names: 'from'/'to', never
# 'from_role'/'to_role'.
MESSAGE_FIELDS = (
    "id",
    "from",
    "to",
    "topic",
    "body",
    "created_at",
    "read_at",
    "opened_at",
    "action_required",
    "reply_to",
    "status",
    "resolved_by",
    "resolved_at",
    "resolution_note",
    "kind",
    "work_status",
    "blocked_by",
    "deleted_at",
    "pin_key",
    "about_message_id",
    "superseded_at",
    "decision_requested",
    "revised_at",
    "addenda",
    "addendum",
    "voters",
    # Computed by the tool rather than stored, but projectable like the rest
    # so an explicit field list gets exactly what it asked for and nothing
    # extra: acks (consent tally), age_days (debt lists), snippet/match
    # (search), the body digest (T-17), obligation (debt state of a proposal
    # that is also an action_required message).
    "acks",
    "age_days",
    "idle_days",
    "snippet",
    "match",
    "obligation",
    "body_sha256",
    "body_length_bytes",
    "body_length_chars",
)
# The wire renames from_role→from and to_role→to (row_to_dict), but the
# column names are what everyone types first, so a projection accepts both.
FIELD_ALIASES = {"from_role": "from", "to_role": "to"}
PIN_FIELDS = (
    "key",
    "title",
    "body",
    "version",
    "updated_by",
    "updated_at",
    "approved_by",
    "body_sha256",
    "body_length_bytes",
    "body_length_chars",
)
# What a listing is usually asked for — offered as a name so the common case
# is one word instead of a hand-typed list.
HEADER_PRESET = "headers"
MESSAGE_HEADERS = (
    "id",
    "from",
    "to",
    "topic",
    "created_at",
    "kind",
    "work_status",
    "status",
    "action_required",
    "pin_key",
    "acks",
    "age_days",
    "idle_days",
    "snippet",
    "match",
    "obligation",
    # The digest rides in the header set: a voter can check the text WITHOUT
    # pulling every body into context.
    "body_sha256",
    "body_length_bytes",
    "body_length_chars",
)
PIN_HEADERS = (
    "key",
    "title",
    "version",
    "updated_by",
    "updated_at",
    "approved_by",
    "body_sha256",
    "body_length_bytes",
)


def resolve_fields(
    fields: list[str] | str | None,
    *,
    allowed: tuple[str, ...],
    preset: tuple[str, ...],
) -> tuple[str, ...] | None:
    """Validate a projection request. None means 'everything' (the default);
    the literal 'headers' means the preset for that entity."""
    if fields is None:
        return None
    if isinstance(fields, str):
        fields = [fields]
    if list(fields) == [HEADER_PRESET]:
        return preset
    fields = [FIELD_ALIASES.get(f, f) for f in fields]
    unknown = [f for f in fields if f not in allowed and f != HEADER_PRESET]
    if unknown:
        raise ValueError(
            f"unknown field(s) {sorted(unknown)} — allowed: {sorted(allowed)}, "
            f"or the single value '{HEADER_PRESET}' for the usual listing set"
        )
    if not fields:
        raise ValueError("'fields' cannot be empty — omit it to get every field")
    return tuple(dict.fromkeys(fields))


def project(rows: list[dict[str, Any]], fields: tuple[str, ...] | None) -> list[dict[str, Any]]:
    if fields is None:
        return rows
    return [{k: r[k] for k in fields if k in r} for r in rows]


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["action_required"] = bool(d.get("action_required", 0))
    if isinstance(d.get("voters"), str):
        # Stored as a JSON array; every reader wants the list. A round that
        # declared nothing keeps voters=None, which every caller reads as
        # "the original rule applies".
        try:
            d["voters"] = json.loads(d["voters"])
        except json.JSONDecodeError:
            d["voters"] = None
    if "from_role" in d:
        d["from"] = d.pop("from_role")
    if "to_role" in d:
        d["to"] = d.pop("to_role")
    return d
