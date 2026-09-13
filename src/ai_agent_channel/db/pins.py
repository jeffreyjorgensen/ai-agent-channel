"""Pins and the consent around them: pin versions, acknowledgements (votes),
tallies, open rounds, the void rule, and what each role still has to decide."""

from __future__ import annotations

import sqlite3
from typing import Any

from .blobs import body_digest
from .connection import atomic
from .fields import row_to_dict
from .messages import addressed_to, expand_recipients, message_filters
from .schema import NOW_SQL

# "This proposal is still waiting for the decision of the role bound to
# :role". One definition for the listing, the stop-hook counter and the board.
# Not pending: a nudge (about_message_id set), a proposal opened for reading
# (decision_requested = 0), a superseded one, one the role has voted on since
# the last revision, and one whose declared electorate excludes the role.
AWAITING_ACK = (
    f"{addressed_to('role')} AND m.kind = 'proc' "
    "AND m.deleted_at IS NULL AND m.superseded_at IS NULL "
    "AND m.decision_requested = 1 AND m.about_message_id IS NULL "
    "AND NOT EXISTS ("
    "    SELECT 1 FROM acknowledgements a "
    "    WHERE a.message_id = m.id AND a.role = :role "
    "      AND (m.revised_at IS NULL OR a.created_at > m.revised_at)"
    ") "
    "AND (m.voters IS NULL OR EXISTS ("
    "    SELECT 1 FROM json_each(m.voters) j WHERE j.value = :role))"
)
# Positional form of AWAITING_ACK for `?` queries: bind the role four times.

# "The role of acknowledgement a belongs to the electorate of message m": the
# declared voters, or the addressees when none were declared.
_IN_ELECTORATE = (
    "((m.voters IS NOT NULL AND EXISTS "
    "     (SELECT 1 FROM json_each(m.voters) j WHERE j.value = a.role)) "
    " OR (m.voters IS NULL AND (m.to_role = a.role OR EXISTS "
    "     (SELECT 1 FROM message_recipients mr "
    "      WHERE mr.message_id = m.id AND mr.to_role = a.role))))"
)

# "Acknowledgement a on message m closes the round as dead": a 'void' cast by
# a member of the electorate after the last revision of the body. The author
# cannot acknowledge their own message (they withdraw by deleting it). One
# predicate for "is the key free" and "can this approve a pin".
_ROUND_VOID = (
    "a.message_id = m.id AND a.decision = 'void' "
    "AND (m.revised_at IS NULL OR a.created_at > m.revised_at) "
    f"AND {_IN_ELECTORATE}"
)


def fetch_awaiting_ack(
    conn: sqlite3.Connection,
    *,
    role: str,
    limit: int,
    from_role: str | None = None,
    pin_key: str | None = None,
) -> list[dict[str, Any]]:
    """Proposals `role` still owes a decision on.

    from_role= lets the SENDER measure what they have left outstanding on
    other people, which filtering by addressee alone cannot show.
    """
    clauses, params = message_filters(from_role=from_role, pin_key=pin_key)
    clauses.append(AWAITING_ACK)
    rows = conn.execute(
        f"SELECT * FROM messages m WHERE {' AND '.join(clauses)} ORDER BY m.id ASC LIMIT :limit",
        {**params, "role": role, "limit": limit},
    ).fetchall()
    return expand_recipients(conn, [row_to_dict(r) for r in rows])


def open_rounds_for_pin(conn: sqlite3.Connection, *, key: str) -> list[dict[str, Any]]:
    """Proposals for `key` that are still open — the round is neither
    settled, withdrawn, nor declared dead.

    By §7 the first pin_set makes a key protected forever, so two rounds on
    one key are a race whose loser silently proposes against an
    already-taken version; callers use this to refuse the second round.
    """
    rows = conn.execute(
        "SELECT id, from_role, topic, created_at FROM messages m "
        "WHERE m.kind = 'proc' AND m.pin_key = ? AND m.deleted_at IS NULL "
        "  AND m.superseded_at IS NULL "
        f"  AND NOT EXISTS (SELECT 1 FROM acknowledgements a WHERE {_ROUND_VOID}) "
        "ORDER BY m.id ASC",
        (key,),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def round_voided_by(conn: sqlite3.Connection, *, message_id: int) -> list[str]:
    """The roles whose 'void' closes this round (empty when it is live)."""
    rows = conn.execute(
        f"SELECT a.role FROM acknowledgements a JOIN messages m ON m.id = a.message_id "
        f"WHERE m.id = ? AND {_ROUND_VOID} ORDER BY a.role",
        (message_id,),
    ).fetchall()
    return [r["role"] for r in rows]


@atomic
def pin_set(
    conn: sqlite3.Connection,
    *,
    key: str,
    title: str,
    body: str,
    version: str,
    role: str,
    approved_by: int | None = None,
) -> dict[str, Any]:
    digest = body_digest(body)
    cur = conn.execute(
        """
        INSERT INTO pinned_entries (key, title, body, version, updated_by,
                                    approved_by, body_sha256,
                                    body_length_bytes, body_length_chars)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING updated_at
        """,
        (
            key,
            title,
            body,
            version,
            role,
            approved_by,
            digest["body_sha256"],
            digest["body_length_bytes"],
            digest["body_length_chars"],
        ),
    )
    row = cur.fetchone()
    return {
        "key": key,
        "version": version,
        "updated_by": role,
        "updated_at": row["updated_at"],
        "approved_by": approved_by,
        **digest,
    }


def pin_get(conn: sqlite3.Connection, *, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT key, title, body, version, updated_by, updated_at, approved_by, "
        "body_sha256, body_length_bytes, body_length_chars "
        "FROM pinned_entries WHERE key = ? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    return dict(row) if row else None


def pin_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT key, title, version, updated_by, updated_at, approved_by, "
        "body_sha256, body_length_bytes, body_length_chars FROM pinned_entries "
        "WHERE id IN (SELECT MAX(id) FROM pinned_entries GROUP BY key) "
        "ORDER BY key ASC",
    ).fetchall()
    return [dict(r) for r in rows]


def approval_used(conn: sqlite3.Connection, *, message_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pinned_entries WHERE approved_by = ? LIMIT 1",
        (message_id,),
    ).fetchone()
    return row is not None


def pin_ever_approved(conn: sqlite3.Connection, *, key: str) -> bool:
    """A key with at least one approved version is contractual: once the
    parties used the agree-loop for it, later updates must keep using it."""
    row = conn.execute(
        "SELECT 1 FROM pinned_entries WHERE key = ? AND approved_by IS NOT NULL LIMIT 1",
        (key,),
    ).fetchone()
    return row is not None


def pin_history(conn: sqlite3.Connection, *, key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT key, title, body, version, updated_by, updated_at, approved_by, "
        "body_sha256, body_length_bytes, body_length_chars "
        "FROM pinned_entries WHERE key = ? ORDER BY id DESC",
        (key,),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_acknowledgement(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    role: str,
    decision: str,
    note: str | None,
) -> dict[str, Any]:
    cur = conn.execute(
        f"""
        INSERT INTO acknowledgements (message_id, role, decision, note)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(message_id, role) DO UPDATE SET
            decision = excluded.decision,
            note = excluded.note,
            created_at = {NOW_SQL}
        RETURNING message_id, role, decision, note, created_at
        """,
        (message_id, role, decision, note),
    )
    return dict(cur.fetchone())


def acks_by_message(
    conn: sqlite3.Connection, *, message_ids: list[int], stale: bool = False
) -> dict[int, dict[str, str]]:
    """{message_id: {role: decision}} for a whole listing in one query —
    annotating N messages must not cost N round trips.

    Returns FRESH votes by default: a vote cast before the body was re-issued
    (revise_message) was cast on a different text, so it stops counting. Pass
    stale=True for the quenched ones — they are kept, and showing them is how
    a re-issue stays visible instead of looking like roles that never voted.
    """
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    freshness = (
        "a.created_at <= m.revised_at"
        if stale
        else ("(m.revised_at IS NULL OR a.created_at > m.revised_at)")
    )
    rows = conn.execute(
        f"SELECT a.message_id, a.role, a.decision FROM acknowledgements a "
        f"JOIN messages m ON m.id = a.message_id "
        f"WHERE a.message_id IN ({marks}) AND {freshness}",
        message_ids,
    ).fetchall()
    out: dict[int, dict[str, str]] = {}
    for r in rows:
        out.setdefault(r["message_id"], {})[r["role"]] = r["decision"]
    return out


def ack_tally(
    decisions: dict[str, str],
    *,
    author: str,
    voters: list[str],
    stale: dict[str, str] | None = None,
    declared: bool = False,
) -> dict[str, Any]:
    """What a proposal's consent looks like right now, from the server's
    records only.

    'voters' is who the round needs: the electorate the proposal DECLARED,
    and failing that its addressees — never the channel roster, which would
    count roles that never received the message. A pin proposal still goes
    to everyone, so the electorate is a subset of the readers.

    'declared' only changes what the leftover votes are CALLED: with an
    electorate the roles outside it are recipients whose vote does not
    block, not "non-recipients".

    'missing' names who has no recorded 'agree', so prose agreement in the
    thread cannot pass for consent.
    """
    eligible = [r for r in voters if r != author]
    agreed = sorted(r for r in eligible if decisions.get(r) == "agree")
    tally = {
        "agreed": len(agreed),
        "needed": len(eligible),
        "missing": sorted(r for r in eligible if decisions.get(r) != "agree"),
        "decisions": {r: d for r, d in sorted(decisions.items())},
        # Who the question was actually put to, so "agreed 1" next to more
        # recorded 'agree' decisions reads as votes from non-voters rather
        # than a broken counter.
        "voters": sorted(eligible),
    }
    # Only a void from the electorate closes the round (_ROUND_VOID); a
    # bystander's void is listed with the other non-voter decisions below.
    void = sorted(r for r in decisions if decisions[r] == "void" and r in voters)
    if void:
        # "the subject no longer exists": neither agreement nor refusal.
        tally["declared_dead_by"] = void
    # Votes from roles this round does not need are legitimate (acknowledge
    # works on anything) but they are not what closes it. Shown separately so
    # they neither inflate the count nor vanish.
    bystanders = {r: d for r, d in sorted(decisions.items()) if r not in eligible}
    if bystanders:
        tally["from_non_voters" if declared else "from_non_recipients"] = bystanders
    if stale:
        tally["quenched_by_revision"] = {r: d for r, d in sorted(stale.items())}
    return tally


def fetch_acknowledgements(conn: sqlite3.Connection, *, message_id: int) -> list[dict[str, Any]]:
    """Every vote on record, each flagged 'stale' when it predates the last
    re-issue of the body. Nothing is deleted by a revision — a vote is a
    fact about a text, and both the fact and which text it was about stay."""
    rows = conn.execute(
        "SELECT a.message_id, a.role, a.decision, a.note, a.created_at, "
        "  CASE WHEN m.revised_at IS NOT NULL AND a.created_at <= m.revised_at "
        "       THEN 1 ELSE 0 END AS stale "
        "FROM acknowledgements a JOIN messages m ON m.id = a.message_id "
        "WHERE a.message_id = ? ORDER BY a.role ASC",
        (message_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stale"] = bool(d["stale"])
        out.append(d)
    return out
