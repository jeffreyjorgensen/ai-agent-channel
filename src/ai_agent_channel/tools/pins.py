"""Pinned entries and consent: the pin approval rule, pin reads and history,
acknowledge, and the consent tally of a message."""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import db
from .common import (
    DECISIONS,
    FIELDS_DOC,
    NOTE_MAX,
    PIN_KEY_MAX,
    TITLE_MAX,
    VERSION_MAX,
    Fields,
    body_or_ref,
    channel_roles,
    check_enum,
    check_length,
    current_role,
    electorate_of,
    open_channel_db,
    registered_members,
)
from .registry import tool

# Every version of these keys, the first included, needs 'approved_by'. A
# floor, not a ceiling: a key ever written with approved_by is protected from
# then on (db.pin_ever_approved).
PROTECTED_PIN_KEYS = ("team-charter", "contract-version", "glossary")

# Appended to every tool that returns a pin: the digest is only useful if
# everyone hashes the same way, so the rule travels with the number.
DIGEST_DOC = (
    " Every pin response carries 'body_sha256' plus 'body_length_bytes' and "
    "'body_length_chars'. The hash is sha256 over the body's RAW UTF-8 BYTES "
    "exactly as stored — no normalisation of any kind (no trailing-whitespace "
    "trimming, no newline conversion, no Unicode NFC), so two parties who "
    "hash the same text always get the same number. Length is published "
    "under two explicitly named fields because 'length' alone is ambiguous "
    "for non-ASCII text, where one character can take several bytes. The "
    "server publishes these; it does NOT verify anything "
    "with them — comparing the pinned body against what was agreed is the "
    "team's check, and now it has an authoritative number to check against."
)

# get_acknowledgements is not a listing: 'fields' projects the answer object.
ACK_FIELDS = (
    "message_id",
    "acknowledgements",
    "agreed",
    "needed",
    "missing",
    "decisions",
    "from_non_recipients",
    "from_non_voters",
    "quenched_by_revision",
    "voters",
    "roles",
    "roles_source",
    "declared_dead_by",
    "body_sha256",
    "generated_at",
)
# The header set drops the notes but keeps what explains the count: votes from
# outside the electorate, votes quenched by a revision, and voids.
ACK_HEADERS = (
    "agreed",
    "needed",
    "missing",
    "decisions",
    "voters",
    "from_non_recipients",
    "from_non_voters",
    "quenched_by_revision",
    "declared_dead_by",
)


def _pin_fields(fields: Fields) -> tuple[str, ...] | None:
    return db.resolve_fields(fields, allowed=db.PIN_FIELDS, preset=db.PIN_HEADERS)


class _Blocked(Exception):
    """A consent rule that is not satisfied. Carries the diagnostic so the
    same check can either raise (pin_set) or be reported (dry_run)."""

    def __init__(self, problem: str, missing: list[str] | None = None) -> None:
        super().__init__(problem)
        self.problem = problem
        self.missing = missing or []


def _approval_names_key(
    conn: sqlite3.Connection, *, msg: dict[str, Any], key: str, approved_by: int
) -> None:
    """The approval must be a proposal about THIS pin.

    The structural link is authoritative. The text scan survives only for
    proposals written before pin_key existed, and only while the key has no
    open structural round: otherwise an unlinked message could settle a key
    past the one round the channel allows on it.
    """
    if msg.get("kind") != "proc":
        raise _Blocked(
            f"approved_by message {approved_by} is not a proposal "
            f"(kind={msg.get('kind')!r}) — only a kind='proc' message can "
            f"approve a pin; send one with pin_key='{key}'"
        )
    linked = msg.get("pin_key")
    if linked is not None:
        if linked != key:
            raise _Blocked(
                f"approved_by message {approved_by} is a proposal for pin '{linked}', not '{key}'"
            )
        return
    if key not in msg["topic"] and key not in msg["body"]:
        raise _Blocked(
            f"approved_by message {approved_by} does not name '{key}' — "
            f"send proposals with pin_key='{key}'"
        )
    open_rounds = db.open_rounds_for_pin(conn, key=key)
    if open_rounds:
        ids = ", ".join(f"#{r['id']}" for r in open_rounds)
        raise _Blocked(
            f"approved_by message {approved_by} only mentions '{key}' in its "
            f"text, and '{key}' has an open round ({ids}) — while a round is "
            f"open, only that round can approve the pin"
        )


def _needed_consent(msg: dict[str, Any], *, role: str) -> tuple[set[str] | None, bool]:
    """(the roles whose 'agree' this approval needs, whether the round declared
    them). None: the roster is unknown (stdio mode), so the party rule applies
    instead and any other role's fresh agree counts."""
    members = registered_members(role)
    if members is None:
        db.require_party(msg, role, "used as approval")
    # The electorate the round declared (visible to every role while it was
    # open), else every other role, as for rounds written before 'voters'.
    declared = [r for r in (msg.get("voters") or []) if r != msg["from"]]
    if declared:
        return set(declared), True
    if members is not None:
        return set(members) - {msg["from"]}, False
    return None, False


def _fresh_agrees(conn: sqlite3.Connection, *, approved_by: int) -> dict[str, str]:
    """{role: when} for every 'agree' that still counts. Votes quenched by a
    re-issue of the body approve nothing: they were given for text that no
    longer exists."""
    return {
        a["role"]: a["created_at"]
        for a in db.fetch_acknowledgements(conn, message_id=approved_by)
        if a["decision"] == "agree" and not a["stale"]
    }


def _check_quorum(
    *,
    key: str,
    approved_by: int,
    needed: set[str] | None,
    declared: bool,
    agree_at: dict[str, str],
    current: dict[str, Any] | None,
) -> None:
    """Every role the round needs has agreed, after the version being replaced."""
    if needed is None:
        if current is not None and max(agree_at.values()) < current["updated_at"]:
            raise _Blocked(
                f"'agree' on message {approved_by} predates the current "
                f"'{key}' version — consent is stale, propose again"
            )
        return
    # A pin is a channel-level contract: the proposal is the proposer's
    # consent, and every other role the round needs must have an 'agree'
    # on record, so two roles cannot rewrite it behind a third's back.
    missing = sorted(needed - set(agree_at))
    if missing:
        scope = f"the roles it named ({sorted(needed)})" if declared else "ALL roles"
        raise _Blocked(
            f"'{key}' is a channel-level contract and needs consent of "
            f"{scope}: proposal {approved_by} still lacks "
            f"'agree' from {missing}",
            missing=missing,
        )
    # Each counted consent must postdate the version being replaced,
    # otherwise it agreed to some OLDER state of the pin.
    if current is not None:
        stale = sorted(r for r in needed if agree_at[r] < current["updated_at"])
        if stale:
            raise _Blocked(
                f"'agree' from {stale} on message {approved_by} "
                f"predates the current '{key}' version — consent "
                f"is stale, propose again",
                missing=stale,
            )


def _check_pin_approval(
    conn: sqlite3.Connection,
    *,
    key: str,
    role: str,
    approved_by: int | None,
) -> None:
    """The whole consent rule for writing `key`, in one place.

    Extracted so pin_set and its dry_run answer from the SAME code: a
    preview that re-implements the check is a preview that will eventually
    disagree with the real thing.
    """
    current = db.pin_get(conn, key=key)
    protected = key in PROTECTED_PIN_KEYS or db.pin_ever_approved(conn, key=key)
    if protected and approved_by is None:
        stage = "updating" if current is not None else "creating"
        raise _Blocked(
            f"key '{key}' is protected (reserved, or previously updated "
            f"with approval): {stage} it requires 'approved_by' — the id "
            f"of a proposal message acknowledged with 'agree' by the other "
            f"side"
        )
    if approved_by is None:
        return
    msg = db.fetch_message(conn, approved_by)
    if msg is None:
        raise _Blocked(f"approved_by message {approved_by} not found")
    needed, declared = _needed_consent(msg, role=role)
    _approval_names_key(conn, msg=msg, key=key, approved_by=approved_by)
    voided = db.round_voided_by(conn, message_id=approved_by)
    if voided:
        raise _Blocked(
            f"proposal {approved_by} was declared void by {voided} — a dead "
            f"round cannot approve a pin; propose again"
        )
    agree_at = _fresh_agrees(conn, approved_by=approved_by)
    if not agree_at:
        raise _Blocked(
            f"message {approved_by} has no 'agree' acknowledgement — the change is not agreed yet",
            missing=sorted(needed) if needed else [],
        )
    # One agreed proposal authorises exactly one change.
    if db.approval_used(conn, message_id=approved_by):
        raise _Blocked(
            f"message {approved_by} already approved a pin update — "
            f"propose again for further changes"
        )
    _check_quorum(
        key=key,
        approved_by=approved_by,
        needed=needed,
        declared=declared,
        agree_at=agree_at,
        current=current,
    )


@tool(
    description=(
        "Create or update a channel-level pinned entry by stable key; every "
        "write appends a version (see pin_history). "
        "PROTECTED keys — 'team-charter', 'contract-version', 'glossary', and "
        "any key ever written with approved_by — need 'approved_by' on every "
        "version, the first included: the id of a kind='proc' proposal that "
        "(a) is for this key: its pin_key equals the key, or — only for a "
        "proposal sent without a pin_key, and only while the key has no open "
        "round — the key appears in its topic or body; (b) has a fresh "
        "'agree' (cast after the last revision of its body and after the "
        "current pin version) from every role of its declared 'voters' — or, "
        "if it declared none, from every other role of a hosted channel, or "
        "from any other role in stdio mode; (c) was not declared void by a "
        "voter (a member of its electorate; the author withdraws a proposal "
        "with delete_message instead); and (d) has not approved a pin version "
        "before — one agreed proposal, one change. In stdio mode you must "
        "also be the proposal's sender or a recipient. Other keys are written "
        "freely; passing approved_by protects them from then on. The body "
        "should be verbatim the agreed text — not enforced, the digests below "
        f"make it checkable. 'title' is at most {TITLE_MAX} characters, "
        f"'version' at most {VERSION_MAX}. dry_run=true runs every check and "
        "returns {ok, problem, missing_agrees} without writing. Returns the "
        "written version with 'superseded' (the proposals for this key it "
        "retired)." + DIGEST_DOC
    )
)
def pin_set(
    key: str,
    title: str,
    version: str,
    body: str = "",
    approved_by: int | None = None,
    dry_run: bool = False,
    body_ref: int | None = None,
) -> dict[str, Any]:
    role = current_role()
    body = body_or_ref(body, body_ref)
    key = (key or "").strip()
    title = (title or "").strip()
    version = (version or "").strip()
    if not key:
        raise ValueError("'key' is required")
    if len(key) > PIN_KEY_MAX:
        raise ValueError(f"'key' must be <= {PIN_KEY_MAX} characters")
    if not title:
        raise ValueError("'title' is required")
    check_length(title, "title", TITLE_MAX)
    if not body:
        raise ValueError("'body' is required")
    if not version:
        raise ValueError("'version' is required")
    check_length(version, "version", VERSION_MAX)
    # A preview writes nothing, so it does not queue for the write lock.
    with open_channel_db(write=not dry_run) as conn:
        try:
            _check_pin_approval(conn, key=key, role=role, approved_by=approved_by)
        except _Blocked as blocked:
            if not dry_run:
                raise ValueError(blocked.problem) from None
            return {
                "dry_run": True,
                "ok": False,
                "problem": blocked.problem,
                "missing_agrees": blocked.missing,
                "written": False,
            }
        # PermissionError is not a "not yet" — it says this role may never use
        # that message as approval, so it raises in dry_run too.
        if dry_run:
            return {
                "dry_run": True,
                "ok": True,
                "written": False,
                "would_write": {"key": key, "version": version, **db.body_digest(body)},
            }
        written = db.pin_set(
            conn,
            key=key,
            title=title,
            body=body,
            version=version,
            role=role,
            approved_by=approved_by,
        )
        # The question this round was asking is now settled, so the drafts
        # that asked it stop being outstanding. Reported, never silent.
        written["superseded"] = db.supersede_proposals_for_pin(
            conn, key=key, role=role, version=version
        )
        return written


@tool(
    description=(
        "Get the current version of a pinned entry by key, or null if the key "
        "has never been pinned." + DIGEST_DOC
    ),
    read_only=True,
)
def pin_get(key: str) -> dict[str, Any] | None:
    with open_channel_db() as conn:
        return db.pin_get(conn, key=key)


@tool(
    description=(
        "List all pinned entries (key, title, version, updated_by, updated_at, "
        "approved_by) WITHOUT bodies — cheap overview, and enough to verify "
        "your local copy of every document in one call. Use pin_get(key) to "
        "fetch a body." + DIGEST_DOC
    ),
    read_only=True,
)
def pin_list() -> dict[str, Any]:
    with open_channel_db() as conn:
        return {"result": db.pin_list(conn)}


@tool(
    description=(
        "Full version history of a pinned entry by key, newest first — audit of "
        "who changed it and when."
        + FIELDS_DOC.replace(
            "field names",
            "field names (key/title/version/updated_by/updated_at/approved_by/body/body_sha256/body_length_bytes)",
        )
    ),
    read_only=True,
)
def pin_history(key: str, fields: Fields = None) -> dict[str, Any]:
    projection = _pin_fields(fields)
    with open_channel_db() as conn:
        return {"result": db.project(db.pin_history(conn, key=key), projection)}


@tool(
    description=(
        "Record your role's decision on a message: agree, reject, "
        "needs_changes, or void, with an optional note "
        f"(at most {NOTE_MAX} characters). "
        "'void' means 'the subject of this decision no longer exists'. It "
        "clears the record from your awaiting_ack and never counts towards "
        "'agreed'. A void from a voter of the round (its declared 'voters', "
        "or its recipients when none were declared), cast after the last "
        "revision of the body, closes a pin round: the key is free for a new "
        "round and the proposal can no longer approve pin_set. A void from "
        "anyone else is recorded but closes nothing. "
        "'expect_body_sha256': pass the digest of the body you read and the "
        "vote is refused if the author has re-issued it since. "
        "Works on any message kind (only kind='proc' surfaces in "
        "awaiting_ack). One acknowledgement per (message, role) — repeating "
        "overwrites it. You cannot acknowledge your own message. A proposal "
        "is agreed when every role of its electorate — the declared "
        "'voters', or else its recipients — has a fresh 'agree' (see "
        "get_acknowledgements). "
        "Acking also retires your outstanding nudges about this message "
        "(sent with about_message_id=<this id> and addressed to you); they "
        "are returned as 'superseded'."
    )
)
def acknowledge(
    message_id: int,
    decision: str,
    note: str | None = None,
    expect_body_sha256: str | None = None,
) -> dict[str, Any]:
    role = current_role()
    check_enum(decision, DECISIONS, "decision")
    check_length(note, "note", NOTE_MAX)
    with open_channel_db(write=True) as conn:
        msg = db.fetch_message(conn, message_id)
        if msg is None:
            raise ValueError(f"message {message_id} not found")
        if msg["from"] == role:
            raise ValueError(
                f"cannot acknowledge your own message {message_id} (sender agreement is implicit)"
            )
        if expect_body_sha256 is not None:
            actual = db.body_digest(msg["body"])["body_sha256"]
            if actual != expect_body_sha256.strip().lower():
                raise ValueError(
                    f"the body of message {message_id} is {actual}, not "
                    f"{expect_body_sha256} — it was re-issued after you read "
                    f"it. Read it again before voting: consent is bound to "
                    f"the bytes, and this is the one place where acting on a "
                    f"stale copy cannot be taken back"
                )
        ack = db.upsert_acknowledgement(
            conn, message_id=message_id, role=role, decision=decision, note=note
        )
        if decision == "void":
            # A judgement that the round is dead is a lifecycle fact about
            # the message, not just a vote on it — the audit has to be able
            # to answer "who declared this dead, when, and why".
            db.insert_event(
                conn,
                message_id=message_id,
                event="declared_dead",
                role=role,
                note=note,
            )
        ack["superseded"] = db.supersede_nudges(conn, about_message_id=message_id, role=role)
        return ack


@tool(
    description=(
        "The consent state of a message: the acknowledgements on record AND "
        "'missing' — the roles whose vote is still absent, which is what you "
        "actually need to know and what the collected votes alone cannot "
        "tell you. "
        "'needed' counts the round's electorate: its declared 'voters' when "
        "it has them (listed under 'voters'), otherwise its recipients — "
        "never the channel roster, and never the author. Votes from roles "
        "outside the electorate are reported, not counted: under "
        "'from_non_voters' when voters were declared, 'from_non_recipients' "
        "otherwise. Votes cast before the body was re-issued appear under "
        "'quenched_by_revision'; voids under 'declared_dead_by'. "
        "'fields' projects this answer (not a listing): use "
        "fields='headers' for the tally without the notes." + FIELDS_DOC
    ),
    read_only=True,
)
def get_acknowledgements(message_id: int, fields: Fields = None) -> dict[str, Any]:
    projection = db.resolve_fields(fields, allowed=ACK_FIELDS, preset=ACK_HEADERS)
    with open_channel_db() as conn:
        msg = db.fetch_message(conn, message_id)
        if msg is None:
            raise ValueError(f"message {message_id} not found")
        roles, source = channel_roles(conn)
        acks = db.fetch_acknowledgements(conn, message_id=message_id)
        voters = electorate_of(msg)
        tally = db.ack_tally(
            {a["role"]: a["decision"] for a in acks if not a["stale"]},
            author=msg["from"],
            voters=voters,
            stale={a["role"]: a["decision"] for a in acks if a["stale"]},
            declared=bool(msg.get("voters")),
        )
        answer = {
            "message_id": message_id,
            "acknowledgements": acks,
            **tally,
            "roles": roles,
            "roles_source": source,
            # The digest of what is being voted on right now: pass it back to
            # acknowledge(expect_body_sha256=...) and a re-issue in between
            # cannot swallow your vote.
            "body_sha256": db.body_digest(msg["body"])["body_sha256"],
            "generated_at": db.now(conn),
        }
        return db.project([answer], projection)[0]
