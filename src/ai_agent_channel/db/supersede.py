"""Retiring messages that are answered, and replaying that rule over history.

Retirement is causal, never by age. The two live causes are a new version of
the pin a proposal targeted, and an ack on the proposal a nudge was about.
Superseded messages stay readable with their acks and history; they only stop
being listed as outstanding.

The backfill replays the pin rule for rounds settled before it existed. Only a
proposal raised before the pin's current version was settled by it; one raised
after is a live round and is never touched."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .connection import atomic
from .fields import row_to_dict
from .messages import ADDRESSED_TO, fetch_message, insert_event
from .pins import pin_get, pin_list
from .schema import NOW_SQL

# What a backfill candidate can be projected down to.
BACKFILL_FIELDS = (
    "id",
    "from",
    "to",
    "topic",
    "created_at",
    "pin_key",
    "settled_by",
    "claimed_by",
    "claim_count",
)
BACKFILL_HEADERS = ("id", "from", "to", "topic", "created_at", "claimed_by", "claim_count")

# The event name of retirements made by a cleanup pass. Only these can be
# undone; retirements by the live rule cannot.
BACKFILL_EVENT = "superseded_backfill"

# "#1403": how older messages refer to another message in prose.
_MESSAGE_REF_RE = re.compile(r"#(\d{1,7})\b")

# One "<key> (word: #N)" entry of the note stamp written before pin_keys was
# stored; a key containing ',', ';', '(', ')' or '#' cannot be read back.
_LEGACY_STAMP_ENTRY_RE = re.compile(r"[^,;()#]+ \(word: #\d+\)")

# "Proposal m is for the pin key bound to :key": the structural field when
# set, otherwise the key's literal, case-sensitive occurrence in the text.
_CLAIMED_BY_KEY = (
    "(pin_key = :key OR (pin_key IS NULL AND (instr(topic, :key) > 0 OR instr(body, :key) > 0)))"
)

# Upper bound on ids bound into one IN (...) list.
_ID_CHUNK = 500


@atomic
def _mark_superseded(
    conn: sqlite3.Connection,
    *,
    ids: list[int],
    role: str,
    note: str,
    event: str = "superseded",
    pin_keys: list[str] | None = None,
) -> list[int]:
    for message_id in ids:
        insert_event(
            conn, message_id=message_id, event=event, role=role, note=note, pin_keys=pin_keys
        )
    conn.executemany(
        f"UPDATE messages SET superseded_at = {NOW_SQL} WHERE id = ?",
        [(i,) for i in ids],
    )
    return ids


@atomic
def cascade_to_nudges(
    conn: sqlite3.Connection,
    *,
    target_ids: list[int],
    role: str,
    note: str,
    event: str = "superseded",
    pin_keys: list[str] | None = None,
) -> list[int]:
    """Retire the live messages whose about_message_id points at these ids.

    A nudge asks for a decision about another message; once that message is
    settled (voted, superseded, deleted, retired by a cleanup) nothing else
    could retire the nudge. Follows about_message_id transitively (a nudge
    about a nudge); see cascade_preview.
    """
    rows = cascade_preview(conn, target_ids=target_ids)
    return _mark_superseded(
        conn, ids=[r["id"] for r in rows], role=role, note=note, event=event, pin_keys=pin_keys
    )


def cascade_preview(conn: sqlite3.Connection, *, target_ids: list[int]) -> list[dict[str, Any]]:
    """What cascade_to_nudges would retire: live messages about these ids,
    following about_message_id transitively, in walk order. Stops when a
    level adds nothing new, so a cycle terminates."""
    out: list[dict[str, Any]] = []
    frontier = [i for i in target_ids if i]
    seen = set(frontier)
    while frontier:
        fresh: list[sqlite3.Row] = []
        for start in range(0, len(frontier), _ID_CHUNK):
            chunk = frontier[start : start + _ID_CHUNK]
            rows = conn.execute(
                f"SELECT id, from_role, to_role, topic, about_message_id FROM messages "
                f"WHERE about_message_id IN ({','.join('?' * len(chunk))}) "
                f"AND deleted_at IS NULL AND superseded_at IS NULL",
                chunk,
            ).fetchall()
            fresh.extend(r for r in rows if r["id"] not in seen)
        if not fresh:
            break
        fresh.sort(key=lambda r: r["id"])
        seen.update(r["id"] for r in fresh)
        out.extend(row_to_dict(r) for r in fresh)
        frontier = [r["id"] for r in fresh]
    return out


@atomic
def supersede_proposals_for_pin(
    conn: sqlite3.Connection, *, key: str, role: str, version: str
) -> list[int]:
    """Retire outstanding proposals for `key` once a new version is pinned.

    The explicit `pin_key` field wins outright; the text match (literal and
    case-sensitive, like pin_set's approval check) applies only to proposals
    without one, so a proposal for another key that mentions this one is
    not retired.
    """
    rows = conn.execute(
        "SELECT id FROM messages WHERE kind = 'proc' AND deleted_at IS NULL "
        f"AND superseded_at IS NULL AND {_CLAIMED_BY_KEY} ORDER BY id ASC",
        {"key": key},
    ).fetchall()
    note = f"'{key}' moved to version {version}"
    ids = _mark_superseded(conn, ids=[r["id"] for r in rows], role=role, note=note)
    return ids + cascade_to_nudges(conn, target_ids=ids, role=role, note=f"target settled: {note}")


@atomic
def supersede_nudges(conn: sqlite3.Connection, *, about_message_id: int, role: str) -> list[int]:
    """Retire this role's nudges about a message they just acknowledged."""
    rows = conn.execute(
        f"SELECT m.id FROM messages m WHERE m.about_message_id = ? "
        f"AND {ADDRESSED_TO} "
        f"AND m.deleted_at IS NULL AND m.superseded_at IS NULL ORDER BY m.id ASC",
        (about_message_id, role, role),
    ).fetchall()
    return _mark_superseded(
        conn,
        ids=[r["id"] for r in rows],
        role=role,
        note=f"acknowledged message {about_message_id}",
    )


def pin_approval_ids(conn: sqlite3.Connection) -> dict[int, str]:
    """{message_id: key} for every message any pin version cites as its
    approval. These are never cleanup candidates: a mistyped id in a
    reviewed list can land on a valid neighbouring approval record."""
    rows = conn.execute(
        "SELECT DISTINCT approved_by, key FROM pinned_entries WHERE approved_by IS NOT NULL"
    ).fetchall()
    return {r["approved_by"]: r["key"] for r in rows}


def _dead_targets(conn: sqlite3.Connection) -> dict[int, str]:
    """Messages that can no longer be voted on: deleted or superseded. A
    revised proposal is not dead — a revision keeps the round live on the
    same id (PROTOCOL §12)."""
    rows = conn.execute(
        "SELECT id, deleted_at FROM messages "
        "WHERE deleted_at IS NOT NULL OR superseded_at IS NOT NULL"
    ).fetchall()
    return {r["id"]: "deleted" if r["deleted_at"] else "superseded" for r in rows}


def key_owner(conn: sqlite3.Connection, *, key: str) -> str | None:
    """Who owns a pin key: whoever set its current version."""
    pin = pin_get(conn, key=key)
    return pin["updated_by"] if pin else None


def _candidate(by_id: dict[int, dict[str, Any]], row: sqlite3.Row) -> dict[str, Any]:
    item = by_id.setdefault(row["id"], {**row_to_dict(row), "settled_by": []})
    item.pop("body", None)
    return item


def _message_refs(row: sqlite3.Row) -> set[int]:
    return {int(n) for n in _MESSAGE_REF_RE.findall(f"{row['topic']}\n{row['body']}")}


def _claim_by_keys(conn: sqlite3.Connection, by_id: dict[int, dict[str, Any]]) -> None:
    """Proposals raised before the current version of a pin that claims them.
    A message claimed by several keys lists every claim."""
    for pin in pin_list(conn):
        key, settled_at = pin["key"], pin["updated_at"]
        rows = conn.execute(
            "SELECT id, from_role, to_role, topic, created_at, pin_key "
            "FROM messages WHERE kind = 'proc' AND deleted_at IS NULL "
            f"AND superseded_at IS NULL AND created_at < :settled_at AND {_CLAIMED_BY_KEY} "
            "ORDER BY id ASC",
            {"settled_at": settled_at, "key": key},
        ).fetchall()
        for row in rows:
            _candidate(by_id, row)["settled_by"].append(
                {
                    "key": key,
                    "version": pin["version"],
                    "updated_at": settled_at,
                    "updated_by": pin["updated_by"],
                    "matched_by": "pin_key" if row["pin_key"] == key else "text",
                }
            )


def _claim_by_dead_target_text(
    conn: sqlite3.Connection, by_id: dict[int, dict[str, Any]], dead: dict[int, str]
) -> None:
    """Proposals without about_message_id that cite a dead message as "#N"."""
    if not dead:
        return
    rows = conn.execute(
        "SELECT id, from_role, to_role, topic, body, created_at, pin_key "
        "FROM messages WHERE kind = 'proc' AND about_message_id IS NULL "
        "AND deleted_at IS NULL AND superseded_at IS NULL"
    ).fetchall()
    for row in rows:
        for target in sorted(r for r in _message_refs(row) if r in dead and r != row["id"]):
            _candidate(by_id, row)["settled_by"].append(
                {"matched_by": "target_text", "target_id": target, "reason": dead[target]}
            )


def _claim_by_dead_target(
    conn: sqlite3.Connection, by_id: dict[int, dict[str, Any]], dead: dict[int, str]
) -> None:
    """Live messages whose about_message_id points at a dead message. A join,
    not an IN list: the dead set can exceed SQLite's variable limit."""
    rows = conn.execute(
        "SELECT m.id, m.from_role, m.to_role, m.topic, m.created_at, m.pin_key, "
        "m.about_message_id FROM messages m JOIN messages t ON t.id = m.about_message_id "
        "WHERE (t.deleted_at IS NOT NULL OR t.superseded_at IS NOT NULL) "
        "AND m.deleted_at IS NULL AND m.superseded_at IS NULL ORDER BY m.id ASC"
    ).fetchall()
    for row in rows:
        _candidate(by_id, row)["settled_by"].append(
            {
                "matched_by": "target",
                "target_id": row["about_message_id"],
                "reason": dead[row["about_message_id"]],
            }
        )


def _claim_by_reference(conn: sqlite3.Connection, by_id: dict[int, dict[str, Any]]) -> None:
    """Proposals that cite, as "#N", a message already found to be retiring.

    One level only, never transitive over "#N" citations: a transitive walk
    would reach ordinary messages whose only link is citing an id. These rows
    are classified 'reference' and are applied only when asked for.
    """
    seed = set(by_id)
    rows = conn.execute(
        "SELECT id, from_role, to_role, topic, body, created_at, pin_key, "
        "about_message_id FROM messages WHERE kind = 'proc' "
        "AND deleted_at IS NULL AND superseded_at IS NULL"
    ).fetchall()
    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["id"] in by_id:
            continue
        for target in sorted(r for r in _message_refs(row) if r in seed and r != row["id"]):
            _candidate(found, row)["settled_by"].append(
                {
                    "matched_by": "target_text",
                    "target_id": target,
                    "reason": "target is itself being retired",
                }
            )
    by_id.update(found)


def _classify(item: dict[str, Any]) -> None:
    """'key': a pin claims it, so a pass over that key owns it. 'target': its
    about_message_id points at a dead message (exact, no key). 'reference':
    linked only by a "#N" citation in prose."""
    item["claimed_by"] = sorted({c["key"] for c in item["settled_by"] if "key" in c})
    item["claim_count"] = len(item["claimed_by"])
    kinds = {c["matched_by"] for c in item["settled_by"]}
    if item["claimed_by"]:
        item["match_kind"] = "key"
    elif "target" in kinds:
        item["match_kind"] = "target"
    else:
        item["match_kind"] = "reference"
    item["structural"] = item["match_kind"] in ("target", "reference")


def pending_supersede_backfill(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """What the rule would have retired, one row per message (never a count),
    so the owner of a key can recognise a round they know is still open.

    * Structural claims: the message's about_message_id target is dead
      (superseded or deleted). No key is involved.
    * Textual claims: the key's name occurs in the message, or the message
      cites a dead or retiring message as "#N". These can miss, over-claim or
      name the wrong owner, so they are applied only when named in `ids`.

    Approval records of pin versions are never candidates.
    """
    by_id: dict[int, dict[str, Any]] = {}
    dead = _dead_targets(conn)
    _claim_by_keys(conn, by_id)
    _claim_by_dead_target_text(conn, by_id, dead)
    _claim_by_dead_target(conn, by_id, dead)
    _claim_by_reference(conn, by_id)
    for message_id in pin_approval_ids(conn):
        by_id.pop(message_id, None)
    out = [by_id[i] for i in sorted(by_id)]
    for item in out:
        _classify(item)
    return out


def filter_candidates(
    candidates: list[dict[str, Any]],
    *,
    keys: list[str] | None = None,
    include_by_reference: bool = False,
) -> list[dict[str, Any]]:
    """Narrow candidates to what a given pass would actually retire, so the
    preview and the apply answer the same question.

    The key filter narrows only rows a key claims: 'target' and 'reference'
    rows claim no key, so naming a key does not select them out.
    """
    out = []
    for row in candidates:
        if row["match_kind"] == "reference" and not include_by_reference:
            continue
        if (
            keys is not None
            and row["match_kind"] == "key"
            and not set(row["claimed_by"]) & set(keys)
        ):
            continue
        out.append(row)
    return out


def _check_expect_count(ids: list[int], expect_count: int) -> None:
    if not ids:
        raise ValueError("'ids' is required — see the preview")
    if expect_count != len(ids):
        raise ValueError(
            f"'expect_count' is {expect_count} but 'ids' names {len(ids)} "
            f"distinct messages. The two are collected separately on purpose "
            f"— the ids from the preview, the count from the word given in "
            f"the channel — so a mismatch means one of them slipped. Nothing "
            f"was retired"
        )


def _check_outside_pass(
    ids: list[int],
    everything: dict[int, dict[str, Any]],
    candidates: dict[int, dict[str, Any]],
    words: dict[str, int],
) -> None:
    """Refuse ids that are candidates, but not of this pass: claimed by
    another key, or linked only by a "#N" reference not asked for."""
    outside = [i for i in ids if i in everything and i not in candidates]
    wrong_key = [
        f"{i} (claimed by {everything[i]['claimed_by']}, not {sorted(words)})"
        for i in outside
        if everything[i]["match_kind"] == "key"
    ]
    if wrong_key:
        raise ValueError(
            f"these are not claimed by the key(s) this pass names: "
            f"{wrong_key}. A pass runs over one key with that key owner's "
            f"word behind it; retiring another key's round under it is the "
            f"attribution problem, not a shortcut. Nothing was retired"
        )
    by_reference = [i for i in outside if everything[i]["match_kind"] == "reference"]
    if by_reference:
        raise ValueError(
            f"{by_reference} are linked to a dead round only by a '#N' "
            f"mention in their text — that is a guess about what a message "
            f"is about, and it matches ordinary messages that merely cite "
            f"an id. Review them in "
            f"backfill_superseded(dry_run=true, include_by_reference=true) "
            f"and pass include_by_reference=true if you still mean it. "
            f"Nothing was retired"
        )


def _check_known(
    conn: sqlite3.Connection,
    ids: list[int],
    candidates: dict[int, dict[str, Any]],
    snapshot_at: str | None,
) -> None:
    """Refuse ids that are not candidates, split by the fix each needs: a
    transcription error, a race with another retirement, or an approval
    record."""
    unknown = [i for i in ids if i not in candidates]
    if not unknown:
        return
    approvals = pin_approval_ids(conn)
    retired_since, never, protected = [], [], []
    for i in unknown:
        row = conn.execute("SELECT superseded_at FROM messages WHERE id = ?", (i,)).fetchone()
        if i in approvals:
            protected.append(f"{i} (approval record of pin '{approvals[i]}')")
        elif (
            row
            and row["superseded_at"]
            and (snapshot_at is None or row["superseded_at"] > snapshot_at)
        ):
            who = conn.execute(
                "SELECT role, created_at FROM message_events WHERE message_id = ? "
                "AND event IN ('superseded', ?) ORDER BY id DESC LIMIT 1",
                (i, BACKFILL_EVENT),
            ).fetchone()
            retired_since.append(
                f"{i} (retired by {who['role']} at {who['created_at']})"
                if who
                else f"{i} (already retired)"
            )
        else:
            never.append(i)
    parts = []
    if never:
        parts.append(f"never were candidates: {never}")
    if retired_since:
        parts.append(f"retired after your snapshot: {retired_since}")
    if protected:
        parts.append(f"cannot be retired: {protected}")
    raise ValueError(
        "; ".join(parts) + ". Nothing was retired — re-run the preview and review it again"
    )


def _check_multi_claims(
    ids: list[int], candidates: dict[int, dict[str, Any]], words: dict[str, int]
) -> None:
    """A message claimed by several keys needs every claiming key named."""
    named = set(words)
    for i in ids:
        claims = set(candidates[i]["claimed_by"])
        missing = sorted(claims - named) if len(claims) > 1 else []
        if missing:
            raise ValueError(
                f"message {i} is claimed by {sorted(claims)} — a pass "
                f"naming only {sorted(named)} cannot retire it. At most "
                f"one of those claims can be right, so it goes in a pass "
                f"that carries the word of every claiming key's owner. "
                f"Missing: {missing}. Nothing was retired"
            )


def _check_words(conn: sqlite3.Connection, words: dict[str, int]) -> None:
    """Each key's word must be a message sent by that key's owner."""
    for key, word_id in sorted(words.items()):
        word = fetch_message(conn, word_id)
        if word is None:
            raise ValueError(f"word_message_id {word_id} not found")
        owner = key_owner(conn, key=key)
        if owner is not None and word["from"] != owner:
            raise ValueError(
                f"message {word_id} was sent by '{word['from']}', but pin "
                f"'{key}' is owned by '{owner}' (they set its current "
                f"version) — the word authorising a pass over a key has to "
                f"come from that key's owner. Nothing was retired"
            )


@atomic
def apply_supersede_backfill(
    conn: sqlite3.Connection,
    *,
    ids: list[int],
    role: str,
    words: dict[str, int],
    expect_count: int,
    note: str,
    snapshot_at: str | None = None,
    include_by_reference: bool = False,
) -> dict[str, Any]:
    """Retire exactly the ids that were reviewed — not a re-run of the query.

    * `expect_count` is a second, independent record of the same claim: the
      ids come from the preview, the number from the message where the key's
      owner gave their word.
    * `words` binds each key to the message where its owner authorised the
      pass; the keys are stored on every retirement event (pin_keys) and the
      words in its note.
    * a message claimed by several keys needs all of them named.
    * unknown ids are reported as never candidates, retired after the
      snapshot, or protected approval records.
    """
    ids = sorted(set(ids))
    _check_expect_count(ids, expect_count)
    everything = {item["id"]: item for item in pending_supersede_backfill(conn)}
    candidates = {
        item["id"]: item
        for item in filter_candidates(
            list(everything.values()),
            keys=sorted(words) or None,
            include_by_reference=include_by_reference,
        )
    }
    _check_outside_pass(ids, everything, candidates, words)
    _check_known(conn, ids, candidates, snapshot_at)
    _check_multi_claims(ids, candidates, words)
    _check_words(conn, words)

    keys = sorted(words)
    stamp = ", ".join(f"{k} (word: #{v})" for k, v in sorted(words.items()))
    retired = _mark_superseded(
        conn,
        ids=ids,
        role=role,
        note=f"{note}; key {stamp}",
        event=BACKFILL_EVENT,
        pin_keys=keys,
    )
    cascaded = cascade_to_nudges(
        conn,
        target_ids=retired,
        role=role,
        note=f"target retired by cleanup; key {stamp}",
        event=BACKFILL_EVENT,
        pin_keys=keys,
    )
    return {
        "retired": retired,
        "cascaded": cascaded,
        "count": len(retired),
        "cascaded_count": len(cascaded),
        "keys": keys,
    }


def _legacy_note_keys(note: str | None) -> list[str] | None:
    """The keys of a cleanup event written before pin_keys was stored, read
    from its note "<note>; key k1 (word: #N), k2 (word: #M)". None when the
    note cannot be read unambiguously."""
    if note is None or note.count("; key ") != 1:
        return None
    entries = note.split("; key ", 1)[1].split(", ")
    if not all(_LEGACY_STAMP_ENTRY_RE.fullmatch(e) for e in entries):
        return None
    return [e.rsplit(" (word: #", 1)[0] for e in entries]


def _event_keys(event: sqlite3.Row) -> list[str]:
    """The pin keys a cleanup event was made under; empty when unknown, which
    leaves undo to the role that applied the pass."""
    if event["pin_keys"] is not None:
        return list(json.loads(event["pin_keys"]))
    return _legacy_note_keys(event["note"]) or []


@atomic
def undo_supersede_backfill(
    conn: sqlite3.Connection, *, ids: list[int], role: str, reason: str | None
) -> dict[str, Any]:
    """Put back what a cleanup pass retired.

    Only retirements made by a cleanup pass can be undone; a proposal retired
    by a vote or a new pin version stays retired. Only the role that applied
    the pass, or the owner of a key the pass named, may undo it.
    """
    ids = sorted(set(ids))
    if not ids:
        raise ValueError("'ids' is required")
    restored: list[int] = []
    refused: list[str] = []
    forbidden: list[str] = []
    for i in ids:
        row = conn.execute("SELECT superseded_at FROM messages WHERE id = ?", (i,)).fetchone()
        if row is None:
            refused.append(f"{i} (not found)")
            continue
        if row["superseded_at"] is None:
            refused.append(f"{i} (not retired)")
            continue
        last = conn.execute(
            "SELECT event, role, note, pin_keys FROM message_events WHERE message_id = ? "
            "AND event IN ('superseded', ?) ORDER BY id DESC LIMIT 1",
            (i, BACKFILL_EVENT),
        ).fetchone()
        if last is None or last["event"] != BACKFILL_EVENT:
            refused.append(f"{i} (retired by the live rule, not by a cleanup)")
            continue
        keys = _event_keys(last)
        owners = {key_owner(conn, key=k) for k in keys}
        if role != last["role"] and role not in owners:
            forbidden.append(
                f"{i} (retired by '{last['role']}' under key(s) {sorted(keys)} — "
                f"only that role or the key's owner may undo it)"
            )
            continue
        conn.execute("UPDATE messages SET superseded_at = NULL WHERE id = ?", (i,))
        insert_event(
            conn,
            message_id=i,
            event="superseded_undone",
            role=role,
            note=reason,
        )
        restored.append(i)
    if forbidden and not restored:
        raise PermissionError("nothing was restored — " + "; ".join(forbidden + refused))
    if refused and not restored:
        raise ValueError("nothing was restored — " + "; ".join(refused))
    return {"restored": restored, "refused": refused + forbidden}
