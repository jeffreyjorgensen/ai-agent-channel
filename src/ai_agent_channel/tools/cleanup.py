"""The one-off supersede backfill over rounds settled before the rule existed,
and its undo."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from .. import db
from .common import FIELDS_DOC, NOTE_MAX, Fields, check_length, current_role, open_channel_db
from .registry import tool

_PREVIEW_NOTE = (
    "post this list to the channel before applying; then call "
    "again with dry_run=false and the SAME arguments plus "
    "expect_count. This preview is filtered by exactly what "
    "you passed — key, ids, include_by_reference — so it is "
    "the set your apply would take, not everything that "
    "could ever be retired. 'excluded' counts what your "
    "arguments left out: rounds of other keys, and rows "
    "linked only by a '#N' mention in prose. "
    "'would_cascade' are nudges that go with them, "
    "'not_candidates' are ids matching nothing (a "
    "transcription error, not a no-op), 'multi_claimed' need "
    "every claiming key named in one pass"
)


def _preview(
    conn: sqlite3.Connection,
    *,
    everything: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    ids: list[int] | None,
    keys: list[str],
    include_by_reference: bool,
    projection: tuple[str, ...] | None,
    generated_at: str,
) -> dict[str, Any]:
    """The dry_run answer: what THIS call would retire, and what it left out."""
    selected = candidates
    not_candidates: list[int] = []
    if ids:
        wanted = set(ids)
        selected = [c for c in candidates if c["id"] in wanted]
        not_candidates = sorted(wanted - {c["id"] for c in candidates})
    # What the same call would also retire, so the size of the cascade is
    # known before the irreversible call.
    cascade = db.cascade_preview(conn, target_ids=[c["id"] for c in selected])
    return {
        "dry_run": True,
        "generated_at": generated_at,
        "would_retire": db.project(selected, projection),
        "count": len(selected),
        "would_cascade": cascade,
        "cascade_count": len(cascade),
        "not_candidates": not_candidates,
        "multi_claimed": [c["id"] for c in selected if c["claim_count"] > 1],
        "total_candidates": len(everything),
        "filtered_by_key": keys or None,
        "excluded": {
            "other_keys": sum(
                1 for c in everything if c["match_kind"] == "key" and c not in candidates
            ),
            "by_reference": 0
            if include_by_reference
            else sum(1 for c in everything if c["match_kind"] == "reference"),
        },
        "note": _PREVIEW_NOTE,
    }


@tool(
    description=(
        "One-off: replay the supersede rule over rounds settled BEFORE the "
        "rule existed. Candidates: proposals for a pin raised before its "
        "current version ('key' rows; claimed_by lists every matching key), "
        "nudges whose about_message_id target is deleted or superseded "
        "('target' rows), and — only with include_by_reference=true — "
        "messages linked to a candidate by a '#N' mention ('reference' rows). "
        "A revised proposal is live, not a dead target. "
        "dry_run=true (the default) returns exactly what this call would "
        "retire: 'key' filters key rows to that key ('target' rows claim no "
        "key and stay in), 'ids' narrows further; plus would_cascade (nudges "
        "retired along), not_candidates and multi_claimed. To apply, repeat "
        "with dry_run=false, the reviewed 'ids', 'expect_count' (= number of "
        "ids), and 'key' plus 'word_message_id' — one message per key, sent "
        "by that key's owner. Refused: ids that are not candidates of this "
        "call, approval records of pin versions, and ids claimed by keys the "
        "pass does not name. Retired messages stay readable with a "
        "'superseded_backfill' event; undo_backfill reverses a pass. 'fields' "
        "projects the preview rows." + FIELDS_DOC
    )
)
def backfill_superseded(
    dry_run: bool = True,
    ids: list[int] | None = None,
    fields: Fields = None,
    key: str | list[str] | None = None,
    word_message_id: int | list[int] | None = None,
    expect_count: int | None = None,
    snapshot_at: str | None = None,
    include_by_reference: bool = False,
) -> dict[str, Any]:
    role = current_role()
    projection = db.resolve_fields(fields, allowed=db.BACKFILL_FIELDS, preset=db.BACKFILL_HEADERS)
    keys = [key] if isinstance(key, str) else (key or [])
    with open_channel_db(write=not dry_run) as conn:
        everything = db.pending_supersede_backfill(conn)
        generated_at = db.now(conn)
        # What THIS call would apply, with THESE arguments — the preview and
        # the apply have to answer the same question, or the review that
        # precedes an irreversible call reviewed something else.
        candidates = db.filter_candidates(
            everything,
            keys=keys or None,
            include_by_reference=include_by_reference,
        )
        if dry_run:
            return _preview(
                conn,
                everything=everything,
                candidates=candidates,
                ids=ids,
                keys=keys,
                include_by_reference=include_by_reference,
                projection=projection,
                generated_at=generated_at,
            )
        if not ids:
            raise ValueError(
                "'ids' is required when dry_run=false — retiring everything "
                "the query returns without naming it is the thing this tool "
                "exists to prevent"
            )
        if expect_count is None:
            raise ValueError(
                "'expect_count' is required when dry_run=false: the number of "
                "records you expect to retire, taken from the word given in "
                "the channel rather than from the list you are passing. Two "
                "independent statements of the same intent — a slip changes "
                "one of them, never both"
            )
        words = [word_message_id] if isinstance(word_message_id, int) else (word_message_id or [])
        if not keys or len(keys) != len(words):
            raise ValueError(
                "pass 'key' and 'word_message_id' together, one word per key: "
                "the key this pass runs over, and the id of the message where "
                "that key's owner gave their word. Both go into the audit "
                "event, so a pass over someone else's key has to name that "
                "role's message instead of being an anonymous pass"
            )
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        result = db.apply_supersede_backfill(
            conn,
            ids=ids,
            role=role,
            words=dict(zip(keys, words, strict=True)),
            expect_count=expect_count,
            snapshot_at=snapshot_at,
            include_by_reference=include_by_reference,
            note=f"backfill of the supersede rule, applied {today}",
        )
        return {"dry_run": False, "applied_at": db.now(conn), **result}


@tool(
    description=(
        "Undo a cleanup: put back records that backfill_superseded retired. "
        "Only retirements made by a cleanup pass can be undone — a proposal "
        "retired by a vote or a new pin version cannot. Only the role that "
        "applied the pass, or the owner of a key the pass ran under, may "
        "undo it. Returns {restored, refused}; raises when nothing could be "
        "restored. Both the retirement and the undo stay in message_history. "
        f"'reason' is at most {NOTE_MAX} characters."
    )
)
def undo_backfill(ids: list[int], reason: str | None = None) -> dict[str, Any]:
    role = current_role()
    check_length(reason, "reason", NOTE_MAX)
    with open_channel_db(write=True) as conn:
        return db.undo_supersede_backfill(conn, ids=ids, role=role, reason=reason)
