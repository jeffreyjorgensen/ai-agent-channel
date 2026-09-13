"""The consent round: linking a proposal to its pin, previewing the write,
and letting the outstanding list forget — causally, never by age.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import db, server
from helpers import L

CHARTER = "team-charter"


def _propose(as_role, *, key: str | None, body: str = "new revision", to="backend"):
    as_role("frontend")
    return server.send_message(
        to=to, topic="proposal", body=body, kind="proc", pin_key=key, about_message_id=None
    )["id"]


def _legacy_proposal(*, key: str, body: str, frm: str = "frontend", to: str = "backend") -> int:
    """A proposal written straight to storage, the way history holds them.

    A second open round on one key is now refused at send time (T-28) and a
    changed text is re-issued on the same id (T-07) — so several live
    drafts under one key can no longer be CREATED through the tools. They
    exist only in history written before those rules, which is exactly what
    the backfill is for; the fixture has to produce that history the same
    way history got it, by writing the row.
    """
    with db.open_db() as conn:
        return db.insert_message(
            conn,
            from_role=frm,
            to_role=to,
            topic="proposal",
            body=body,
            action_required=False,
            reply_to=None,
            kind="proc",
            pin_key=key,
        )["id"]


def _agree(as_role, pid: int, role: str = "backend"):
    as_role(role)
    server.acknowledge(message_id=pid, decision="agree")
    as_role("frontend")


# --- item 3: the structural link between a proposal and a pin -------------


def test_pin_key_links_the_proposal_structurally(db_path: Path, as_role):
    """Body never names the key — the field alone must be enough, which is
    the whole point: the round no longer depends on the wording."""
    pid = _propose(as_role, key=CHARTER, body="text with no key name")
    _agree(as_role, pid)
    result = server.pin_set(key=CHARTER, title="Charter", body="v1", version="v1", approved_by=pid)
    assert result["version"] == "v1"


def test_proposal_that_names_nothing_is_still_rejected(db_path: Path, as_role):
    pid = _propose(as_role, key=None, body="text with no key name")
    _agree(as_role, pid)
    with pytest.raises(ValueError, match="does not name"):
        server.pin_set(key=CHARTER, title="Charter", body="v1", version="v1", approved_by=pid)


def test_legacy_proposals_naming_the_key_in_prose_still_work(db_path: Path, as_role):
    """Seven weeks of history predate the field; those proposals must not
    stop being usable."""
    pid = _propose(as_role, key=None, body=f"changing {CHARTER} to v1")
    _agree(as_role, pid)
    assert (
        server.pin_set(key=CHARTER, title="Charter", body="v1", version="v1", approved_by=pid)[
            "version"
        ]
        == "v1"
    )


# --- item 3: dry_run -------------------------------------------------------


def test_dry_run_reports_the_same_problem_before_anyone_votes(db_path: Path, as_role):
    pid = _propose(as_role, key=None, body="text with no key name")
    preview = server.pin_set(
        key=CHARTER,
        title="Charter",
        body="v1",
        version="v1",
        approved_by=pid,
        dry_run=True,
    )
    assert preview["ok"] is False
    assert preview["written"] is False
    assert "does not name" in preview["problem"]
    # …and the real call, once votes exist, fails with exactly that problem
    _agree(as_role, pid)
    with pytest.raises(ValueError, match="does not name"):
        server.pin_set(key=CHARTER, title="Charter", body="v1", version="v1", approved_by=pid)


def test_dry_run_names_who_has_not_voted(db_path: Path, as_role):
    pid = _propose(as_role, key=CHARTER)
    preview = server.pin_set(
        key=CHARTER,
        title="Charter",
        body="v1",
        version="v1",
        approved_by=pid,
        dry_run=True,
    )
    assert preview["ok"] is False
    assert "no 'agree'" in preview["problem"]


def test_dry_run_writes_nothing(db_path: Path, as_role):
    pid = _propose(as_role, key=CHARTER)
    _agree(as_role, pid)
    ok = server.pin_set(
        key=CHARTER,
        title="Charter",
        body="v1",
        version="v1",
        approved_by=pid,
        dry_run=True,
    )
    assert ok["ok"] is True and ok["written"] is False
    assert len(ok["would_write"]["body_sha256"]) == 64
    assert server.pin_get(key=CHARTER) is None  # nothing was created
    assert L(server.pin_history(key=CHARTER)) == []
    # and the approval was not spent by the preview
    assert (
        server.pin_set(key=CHARTER, title="Charter", body="v1", version="v1", approved_by=pid)[
            "version"
        ]
        == "v1"
    )


# --- item 4: a successful pin_set supersedes the round ---------------------


def test_pin_set_retires_the_proposals_for_that_key(db_path: Path, as_role):
    # legacy shape: three live drafts under one key, which T-28 no longer
    # lets anyone create — the retiring rule still has to handle them
    drafts = [_legacy_proposal(key=CHARTER, body=f"revision {n}") for n in range(3)]
    as_role("backend")
    assert len(L(server.awaiting_ack())) == 3
    _agree(as_role, drafts[-1])

    result = server.pin_set(
        key=CHARTER, title="Charter", body="v1", version="v1", approved_by=drafts[-1]
    )
    assert sorted(result["superseded"]) == sorted(drafts)
    as_role("backend")
    assert L(server.awaiting_ack()) == []
    assert server.channel_status()["counts"]["awaiting_ack"] == 0


def test_a_proposal_for_another_key_is_not_retired(db_path: Path, as_role):
    """The red test: quenching by time instead of by key would silently eat
    live work."""
    other = _propose(as_role, key="glossary", body="changing the glossary")
    mine = _propose(as_role, key=CHARTER)
    _agree(as_role, mine)
    result = server.pin_set(key=CHARTER, title="Charter", body="v1", version="v1", approved_by=mine)
    assert result["superseded"] == [mine]
    as_role("backend")
    assert [m["id"] for m in L(server.awaiting_ack())] == [other]


def test_explicit_pin_key_wins_over_prose(db_path: Path, as_role):
    """A glossary proposal that merely MENTIONS the charter must survive a
    charter update — the structural field is authoritative."""
    other = _propose(as_role, key="glossary", body=f"unlike {CHARTER}, this one differs")
    mine = _propose(as_role, key=CHARTER)
    _agree(as_role, mine)
    assert (
        other
        not in server.pin_set(
            key=CHARTER, title="Charter", body="v1", version="v1", approved_by=mine
        )["superseded"]
    )


def test_superseded_stays_readable_and_says_why(db_path: Path, as_role):
    """Retired is not deleted: the round must remain auditable."""
    draft = _legacy_proposal(key=CHARTER, body="revision 1")
    keeper = _legacy_proposal(key=CHARTER, body="revision 2")
    _agree(as_role, keeper)
    server.pin_set(key=CHARTER, title="Charter", body="v1", version="v7", approved_by=keeper)
    still_there = [m["id"] for m in L(server.list_messages(pin_key=CHARTER))]
    assert draft in still_there
    event = [e for e in L(server.message_history(draft)) if e["event"] == "superseded"]
    assert event and "v7" in event[0]["note"]


# --- item 5: acknowledge supersedes the nudges -----------------------------


def test_acking_a_proposal_retires_the_nudges_about_it(db_path: Path, as_role):
    pid = _propose(as_role, key=CHARTER)
    nudge = server.send_message(
        to="backend",
        topic="your vote is needed",
        body=f"waiting for a vote on {pid}",
        kind="proc",
        about_message_id=pid,
        pin_key=None,
    )["id"]
    as_role("backend")
    # The nudge asks for a decision about ANOTHER message, so it is not a
    # decision of its own: only the proposal is listed. Nobody has ever
    # voted on a nudge — one role's whole history had 62 of them at zero
    # votes, closed by nothing.
    assert {m["id"] for m in L(server.awaiting_ack())} == {pid}

    result = server.acknowledge(message_id=pid, decision="agree")
    assert result["superseded"] == [nudge]
    assert L(server.awaiting_ack()) == []


def _superseded(mid: int) -> bool:
    return any(e["event"] == "superseded" for e in L(server.message_history(message_id=mid)))


def test_a_nudge_to_someone_else_is_not_retired(db_path: Path, as_role):
    as_role("frontend")
    pid = server.send_message(
        to="backend",
        topic="proposal",
        body="x",
        kind="proc",
        pin_key=CHARTER,
        about_message_id=None,
    )["id"]
    nudges = {
        role: server.send_message(
            to=role,
            topic="a vote is needed",
            body="x",
            kind="proc",
            about_message_id=pid,
            pin_key=None,
        )["id"]
        for role in ("backend", "infra")
    }
    as_role("backend")
    server.acknowledge(message_id=pid, decision="agree")
    # backend's own reminder is answered by backend's vote; infra's is not —
    # a vote retires the nudges addressed to the voter, not everyone's.
    assert _superseded(nudges["backend"])
    assert not _superseded(nudges["infra"])


def test_about_message_id_must_exist(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="about_message_id 999 not found"):
        server.send_message(to="backend", topic="t", body="b", about_message_id=999)


# --- item 8: filtering by key ----------------------------------------------


def test_list_by_pin_key_ignores_mere_mentions(db_path: Path, as_role):
    real = _propose(as_role, key=CHARTER, body=f"revision of {CHARTER}")
    as_role("frontend")
    server.send_message(to="backend", topic="chatter", body=f"about {CHARTER} by the way — later")
    assert [m["id"] for m in L(server.list_messages(pin_key=CHARTER))] == [real]
    # text= answers the other question and still returns both — the two
    # filters are not competing, they mean different things
    assert len(L(server.list_messages(text=CHARTER))) == 2


def test_retirement_is_never_by_age(db_path: Path, as_role):
    """Nothing in the channel retires a proposal for being old — only the
    two causal events do."""
    old = _propose(as_role, key="glossary", body="old revision")
    with db.open_db() as conn:
        conn.execute(
            "UPDATE messages SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (old,),
        )
    as_role("backend")
    assert [m["id"] for m in L(server.awaiting_ack())] == [old]


# --- replaying the rule over history, once ---------------------------------


def _settled_round(as_role, key: str, *, extra_drafts: int = 1) -> dict:
    """A round that closed BEFORE superseding existed: drafts, a vote, a pin
    — and then the drafts left listed because nothing retired them."""
    drafts = [_legacy_proposal(key=key, body=f"draft {n}") for n in range(extra_drafts)]
    approved = _legacy_proposal(key=key, body="final revision")
    as_role("frontend")
    _agree(as_role, approved)
    server.pin_set(key=key, title="Document", body="body", version="v1", approved_by=approved)
    # unwind the live rule so the state matches "the rule did not exist yet"
    with db.open_db() as conn:
        conn.execute("UPDATE messages SET superseded_at = NULL")
        conn.execute("DELETE FROM message_events WHERE event = 'superseded'")
    return {"drafts": drafts, "approved": approved}


def _word(as_role, key: str = "release-notes") -> int:
    """The message where the key's owner gave their word for this pass."""
    as_role("frontend")  # _settled_round pins as frontend, so it owns it
    return server.send_message(
        to="backend",
        topic=f"cleanup {key}",
        body="withdrawing the listed ones",
    )["id"]


def _apply(as_role, preview, *, key: str):
    ids = [m["id"] for m in preview["would_retire"]]
    return server.backfill_superseded(
        dry_run=False,
        ids=ids,
        expect_count=len(ids),
        key=key,
        word_message_id=_word(as_role, key),
    )


def test_backfill_previews_by_default_and_names_everything(db_path: Path, as_role):
    settled = _settled_round(as_role, "release-notes", extra_drafts=2)
    preview = server.backfill_superseded()

    assert preview["dry_run"] is True
    # the approved draft is NOT a candidate: it is the approval record of a
    # pin version, and retiring one of those is never right — the guard also
    # catches the one-digit slip that lands on a valid neighbouring id
    assert preview["count"] == 2
    named = {m["id"] for m in preview["would_retire"]}
    assert named == set(settled["drafts"])
    assert settled["approved"] not in named
    assert preview["generated_at"]
    # a count cannot be reviewed — each item says what settled it, and one
    # line per message lists EVERY key that claims it
    first = preview["would_retire"][0]
    assert first["claim_count"] == 1
    assert first["settled_by"][0]["version"] == "v1"
    assert first["topic"] and first["from"] and first["to"]

    # the preview changed nothing — compare against the state, not a
    # remembered number (the approved draft already carries backend's ack,
    # so it is not awaiting one)
    as_role("backend")
    before = {m["id"] for m in L(server.awaiting_ack())}
    server.backfill_superseded()
    assert {m["id"] for m in L(server.awaiting_ack())} == before
    assert before == set(settled["drafts"])


def test_a_round_opened_after_the_pin_is_never_touched(db_path: Path, as_role):
    """The cut between 'the rule did not exist' and 'this round is genuinely
    open' is the timestamp — and it is the whole safety of the operation."""
    _settled_round(as_role, "release-notes")
    live = _legacy_proposal(key="release-notes", body="new round, still open")

    preview = server.backfill_superseded()
    assert live not in {m["id"] for m in preview["would_retire"]}

    _apply(as_role, preview, key="release-notes")
    as_role("backend")
    assert [m["id"] for m in L(server.awaiting_ack())] == [live]


def test_applying_needs_the_reviewed_ids(db_path: Path, as_role):
    _settled_round(as_role, "release-notes")
    with pytest.raises(ValueError, match="'ids' is required"):
        server.backfill_superseded(dry_run=False)


def test_it_refuses_ids_that_were_not_shown(db_path: Path, as_role):
    """What gets retired must be what the channel reviewed, not whatever the
    query happens to return at apply time."""
    _settled_round(as_role, "release-notes")
    live = _legacy_proposal(key="release-notes", body="new round")
    with pytest.raises(ValueError, match="never were candidates"):
        server.backfill_superseded(
            dry_run=False,
            ids=[live],
            expect_count=1,
            key="release-notes",
            word_message_id=_word(as_role),
        )


def test_retired_rows_record_that_a_cleanup_happened(db_path: Path, as_role):
    settled = _settled_round(as_role, "release-notes")
    preview = server.backfill_superseded()
    _apply(as_role, preview, key="release-notes")
    events = [
        e
        for e in L(server.message_history(settled["drafts"][0]))
        if e["event"] == db.BACKFILL_EVENT
    ]
    assert events, "the retirement must be in the history, not just implied"
    assert "backfill" in events[0]["note"]
    # the pass names its key and the letter where the owner gave their word
    assert "release-notes" in events[0]["note"] and "word: #" in events[0]["note"]
    # readable, not deleted
    assert any(m["id"] == settled["drafts"][0] for m in L(server.list_messages()))


def test_backfill_leaves_other_keys_alone(db_path: Path, as_role):
    _settled_round(as_role, "release-notes")
    other = _propose(as_role, key="glossary", body="about the glossary")
    preview = server.backfill_superseded()
    assert other not in {m["id"] for m in preview["would_retire"]}
