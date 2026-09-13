"""Acceptance tests for the 2026-08-09 requirement document 2.0 (T-01..T-33)
(the new points, T-18…T-33).

Eleven of these could not be seen in correspondence: they appeared only when
an IRREVERSIBLE operation went through the channel for the first time. That
is the reason they are worth a suite of their own — each one is a shape of
failure that a week of review did not find and one day of use did.
"""

from __future__ import annotations

import pytest

from ai_agent_channel import db, server
from helpers import L

ROLES = ("alpha", "beta", "gamma", "delta")


@pytest.fixture
def member(channel_identity):
    return channel_identity(ROLES)


def _proc(**kw) -> int:
    kw.setdefault("pin_key", None)
    kw.setdefault("about_message_id", None)
    if kw.get("pin_key"):
        # These cases were all written when "everyone" was implied. '*' is
        # that same rule, now said out loud — the assertions below are about
        # the all-roles quorum and must keep testing exactly that.
        kw.setdefault("voters", "*")
    kw.setdefault("topic", "proposal")
    kw.setdefault("body", "text")
    return server.send_message(kind="proc", **kw)["id"]


def _legacy(
    *,
    key: str | None = None,
    body: str = "draft",
    frm: str = "alpha",
    to: str = "beta",
    about: int | None = None,
) -> int:
    """History as it exists: written before the rules that now shape it."""
    with db.open_db() as conn:
        return db.insert_message(
            conn,
            from_role=frm,
            to_role=to,
            topic="old proposal",
            body=body,
            action_required=False,
            reply_to=None,
            kind="proc",
            pin_key=key,
            about_message_id=about,
        )["id"]


def _settled(member, key: str, *, drafts: int) -> dict:
    """A round closed before the retiring rule existed."""
    ids = [_legacy(key=key, body=f"draft {n} about {key}") for n in range(drafts)]
    approved = _legacy(key=key, body=f"outcome for {key}")
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=approved, decision="agree")
    member("alpha")
    server.pin_set(key=key, title=key, body="body", version="1", approved_by=approved)
    with db.open_db() as conn:
        conn.execute("UPDATE messages SET superseded_at = NULL")
        conn.execute(
            "DELETE FROM message_events WHERE event IN ('superseded', ?)", (db.BACKFILL_EVENT,)
        )
    return {"drafts": ids, "approved": approved}


def _word(member, key: str) -> int:
    """The letter where the key's owner gives their word for a pass."""
    member("alpha")
    return server.send_message(
        to="beta", topic=f"cleanup {key}"[:80], body="withdrawing what is listed"
    )["id"]


# --- T-18: dry_run must reflect ids -----------------------------------------


@pytest.mark.requirement("T-18")
def test_t18_preview_with_ids_shows_exactly_what_the_apply_would_take(member):
    """`ids` changed nothing in dry_run: 274 candidates with it and without it.
    The preview answered a different question than the one asked before an
    irreversible call."""
    settled = _settled(member, "alpha-key", drafts=3)
    one = settled["drafts"][0]

    whole = server.backfill_superseded()
    assert whole["count"] == 3

    narrowed = server.backfill_superseded(ids=[one])
    assert [r["id"] for r in narrowed["would_retire"]] == [one]
    assert narrowed["count"] == 1
    assert narrowed["total_candidates"] == 3


@pytest.mark.requirement("T-18")
def test_t18_an_id_that_matches_nothing_is_named_not_dropped(member):
    """An id dropped in silence is a transcription error that passed review."""
    settled = _settled(member, "alpha-key", drafts=1)
    member("alpha")
    stranger = server.send_message(to="beta", topic="unrelated", body="x")["id"]

    preview = server.backfill_superseded(ids=[settled["drafts"][0], stranger])
    assert preview["not_candidates"] == [stranger]
    assert [r["id"] for r in preview["would_retire"]] == [settled["drafts"][0]]


@pytest.mark.requirement("T-18")
def test_t18_the_cascade_is_visible_before_the_irreversible_call(member):
    """Applying retires more than the ids name — by design; but no call told
    you how much more."""
    settled = _settled(member, "alpha-key", drafts=1)
    target = settled["drafts"][0]
    nudge = _legacy(body="cast your vote", about=target)

    preview = server.backfill_superseded(ids=[target])
    assert [r["id"] for r in preview["would_cascade"]] == [nudge]
    assert preview["cascade_count"] == 1

    applied = server.backfill_superseded(
        dry_run=False,
        ids=[target],
        expect_count=1,
        key="alpha-key",
        word_message_id=_word(member, "alpha-key"),
    )
    assert applied["retired"] == [target]
    assert applied["cascaded"] == [nudge]


@pytest.mark.requirement("T-18")
def test_t18_the_preview_is_filtered_by_the_key_the_pass_names(member):
    """Found by a role against live data: three different keys returned one and
    the same list of 162 records, byte for byte. The preview was answering
    "what could be retired at all", and that is not the question asked before
    an irreversible call."""
    alpha = _settled(member, "alpha-key", drafts=2)
    beta = _settled(member, "beta-key", drafts=3)

    only_alpha = server.backfill_superseded(key="alpha-key")
    only_beta = server.backfill_superseded(key="beta-key")
    assert {r["id"] for r in only_alpha["would_retire"]} == set(alpha["drafts"])
    assert {r["id"] for r in only_beta["would_retire"]} == set(beta["drafts"])
    assert only_alpha["filtered_by_key"] == ["alpha-key"]
    # and what was left out is stated as a number rather than passed over
    assert only_alpha["excluded"]["other_keys"] == 3


@pytest.mark.requirement("T-18")
def test_t18_applying_refuses_an_id_that_belongs_to_another_key(member):
    """A pass runs under one key, backed by that key's owner's word. Retiring
    someone else's round under it is the same attribution problem, not a
    narrowing."""
    _settled(member, "alpha-key", drafts=1)
    beta = _settled(member, "beta-key", drafts=1)
    with pytest.raises(ValueError, match="not claimed by the key"):
        server.backfill_superseded(
            dry_run=False,
            ids=beta["drafts"],
            expect_count=1,
            key="alpha-key",
            word_message_id=_word(member, "alpha-key"),
        )


@pytest.mark.requirement("T-18")
def test_t18_a_prose_reference_needs_asking_for_by_name(member):
    """The transitive version of this rule shipped and was withdrawn the same
    day: against live traffic it swept up 59 ordinary letters whose only
    crime was mentioning someone else's id."""
    settled = _settled(member, "alpha-key", drafts=1)
    abandoned = settled["drafts"][0]
    citing = _legacy(body=f"by the way, #{abandoned} was about the same thing")

    default = server.backfill_superseded()
    assert citing not in {r["id"] for r in default["would_retire"]}
    assert default["excluded"]["by_reference"] >= 1

    with pytest.raises(ValueError, match="include_by_reference"):
        server.backfill_superseded(
            dry_run=False,
            ids=[citing],
            expect_count=1,
            key="alpha-key",
            word_message_id=_word(member, "alpha-key"),
        )


# --- T-19: a pin's approved_by can never be a candidate ---------------------


@pytest.mark.requirement("T-19")
def test_t19_no_approval_record_is_ever_a_candidate(member):
    """The intersection of {pin_list[*].approved_by} with would_retire was six
    out of six. There is one check: it must be empty."""
    settled = _settled(member, "alpha-key", drafts=2)
    preview = server.backfill_superseded()
    approvals = {p["approved_by"] for p in L(server.pin_list()) if p["approved_by"]}
    assert approvals
    assert approvals & {r["id"] for r in preview["would_retire"]} == set()
    assert settled["approved"] in approvals


@pytest.mark.requirement("T-19")
def test_t19_the_refusal_names_it_rather_than_silently_skipping(member):
    """A one-digit typo lands on a valid neighbouring message id — neither the
    preview nor posting the list to the channel catches it. Only the server
    does."""
    settled = _settled(member, "alpha-key", drafts=1)
    with pytest.raises(ValueError, match="approval record of pin"):
        server.backfill_superseded(
            dry_run=False,
            ids=[settled["approved"]],
            expect_count=1,
            key="alpha-key",
            word_message_id=_word(member, "alpha-key"),
        )


# --- T-20: the signal is "the target is retired", not a key name match ------


@pytest.mark.requirement("T-20")
def test_t20_a_nudge_whose_target_died_before_any_pin_is_a_candidate(member):
    """identity opened all nine of theirs: on seven, the target was killed by
    the next revision of the same round and died BEFORE the pin. The textual
    signal finds none of them."""
    member("alpha")
    target = _proc(to="beta", body="draft 1")
    nudge = _legacy(body="your vote is needed", about=target)
    _withdraw_in_history(target)

    preview = server.backfill_superseded()
    row = [r for r in preview["would_retire"] if r["id"] == nudge]
    assert row, "a nudge whose target is gone must be a candidate"
    assert row[0]["structural"] is True
    assert row[0]["settled_by"][0] == {
        "matched_by": "target",
        "target_id": target,
        "reason": "deleted",
    }


def _withdraw_in_history(message_id: int) -> None:
    """A target deleted before deletion cascaded to its nudges — the shape
    history holds; through the tools the nudge would be retired with it."""
    with db.open_db() as conn:
        conn.execute(
            "UPDATE messages SET deleted_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (message_id,),
        )


@pytest.mark.requirement("T-20")
def test_t20_a_nudge_about_a_revised_proposal_is_not_a_candidate(member):
    """A revision keeps the round live on the same id (PROTOCOL section 12):
    the nudge still asks for a vote someone can give."""
    member("alpha")
    target = _proc(to="beta", body="draft 1")
    nudge = _legacy(body="your vote is needed", about=target)
    member("alpha")
    server.revise_message(message_id=target, body="draft 2")

    preview = server.backfill_superseded(include_by_reference=True)
    assert nudge not in {r["id"] for r in preview["would_retire"]}


@pytest.mark.requirement("T-20")
def test_t20_a_structural_row_needs_no_key_and_cannot_misattribute(member):
    """The textual signal named the wrong owner as decisive: the round is
    closed by one key while claimed_by holds another. The structural one
    claims no key at all."""
    member("alpha")
    target = _proc(to="beta", body="draft 1")
    nudge = _legacy(body="vote needed", about=target)
    _withdraw_in_history(target)

    row = next(r for r in server.backfill_superseded()["would_retire"] if r["id"] == nudge)
    assert row["claimed_by"] == [] and row["claim_count"] == 0


@pytest.mark.requirement("T-20")
def test_t20_a_nudge_dies_with_a_target_that_is_itself_being_retired(member):
    """Seven of identity's nine cases: the target was killed by the round's
    next revision and died BEFORE the pin. In storage such a target carries
    no mark — it is simply abandoned, and is found only as a candidate under
    its own key."""
    settled = _settled(member, "alpha-key", drafts=1)
    abandoned = settled["drafts"][0]
    nudge = _legacy(body=f"your vote is needed on #{abandoned}")

    # by default a prose reference is NOT a candidate: it is a guess about
    # what a message is about, and the transitive version of this rule swept
    # 59 ordinary letters on live traffic within hours of shipping
    default = {r["id"] for r in server.backfill_superseded()["would_retire"]}
    assert abandoned in default
    assert nudge not in default

    preview = server.backfill_superseded(include_by_reference=True)
    rows = {r["id"]: r for r in preview["would_retire"]}
    assert nudge in rows, "a call about an abandoned target must be findable by query"
    assert rows[nudge]["match_kind"] == "reference"
    assert rows[nudge]["settled_by"][0]["target_id"] == abandoned


@pytest.mark.requirement("T-20")
def test_t20_a_live_letter_that_merely_cites_an_id_is_not_swept(member):
    """The red test against the previous one: a round happening right now
    quotes the id of a superseded draft — and must not become a cleanup
    candidate. Otherwise the cleanup list poisons itself with live
    traffic."""
    settled = _settled(member, "alpha-key", drafts=1)
    abandoned = settled["drafts"][0]
    member("alpha")
    substantive = _proc(
        to=list(ROLES[1:]),
        topic="spec 3.0, the clause about cleanup",
        body=(
            f"Working through case #{abandoned} as an example of the class.\n"
            + "A detailed walk-through running to many paragraphs. " * 80
        ),
    )
    ids = {r["id"] for r in server.backfill_superseded()["would_retire"]}
    assert abandoned in ids
    assert substantive not in ids


@pytest.mark.requirement("T-20")
def test_t20_every_row_says_how_it_was_matched(member):
    settled = _settled(member, "alpha-key", drafts=1)
    row = next(
        r for r in server.backfill_superseded()["would_retire"] if r["id"] == settled["drafts"][0]
    )
    assert {c["matched_by"] for c in row["settled_by"]} <= {"pin_key", "text", "target"}
    assert row["settled_by"][0]["matched_by"] == "pin_key"


# --- T-21: a fourth decision — "the proposal is dead" -----------------------


@pytest.mark.requirement("T-21")
def test_t21_void_closes_the_record_without_becoming_agreement(member):
    """Four roles independently chose "leave it forever": the mechanism had no
    third move."""
    member("alpha")
    pid = _proc(to=["beta", "gamma"], pin_key=None)
    member("beta")
    server.acknowledge(message_id=pid, decision="void", note="the subject is gone")

    assert pid not in {m["id"] for m in L(server.awaiting_ack())}
    state = server.get_acknowledgements(message_id=pid)
    assert state["agreed"] == 0
    assert state["declared_dead_by"] == ["beta"]
    assert state["decisions"]["beta"] == "void"
    events = [e for e in L(server.message_history(message_id=pid)) if e["event"] == "declared_dead"]
    assert events and events[0]["role"] == "beta" and events[0]["note"] == "the subject is gone"


@pytest.mark.requirement("T-21")
def test_t21_a_dead_proposal_can_never_approve_a_pin(db_path, as_role):
    """The red test from the specification: dead does not become agreed.

    The round must be one that WOULD approve but for the void — otherwise
    the refusal proves nothing about it. In stdio mode an undeclared
    electorate needs only one recipient's fresh agree, so infra's agree is a
    full quorum and backend's void is the only thing that can stop it."""
    for role in ("backend", "infra"):
        as_role(role)
        server.send_message(to="frontend", topic="hello", body="present")
    as_role("frontend")
    pid = server.send_message(
        to="*",
        topic="notes v1",
        body="text",
        kind="proc",
        pin_key="notes",
        about_message_id=None,
    )["id"]
    as_role("backend")
    server.acknowledge(message_id=pid, decision="void", note="subject is gone")
    as_role("infra")
    server.acknowledge(message_id=pid, decision="agree")

    as_role("frontend")
    with db.open_db() as conn:
        assert db.open_rounds_for_pin(conn, key="notes") == []
    preview = server.pin_set(
        key="notes",
        title="n",
        version="1",
        body="text",
        approved_by=pid,
        dry_run=True,
    )
    assert preview["ok"] is False
    assert "declared void by ['backend']" in preview["problem"]
    with pytest.raises(ValueError, match="void"):
        server.pin_set(key="notes", title="n", version="1", body="text", approved_by=pid)
    assert server.pin_get(key="notes") is None


@pytest.mark.requirement("T-21")
def test_t21_on_a_roster_the_refusal_names_the_void(member):
    """With a known roster the void also leaves a vote missing; the refusal
    must still say the round is dead, not ask for an agree that can never
    make it live again."""
    member("alpha")
    pid = _proc(to=list(ROLES[1:]), pin_key="glossary")
    for role in ("gamma", "delta"):
        member(role)
        server.acknowledge(message_id=pid, decision="agree")
    member("beta")
    server.acknowledge(message_id=pid, decision="void")
    member("alpha")
    preview = server.pin_set(
        key="glossary",
        title="g",
        body="b",
        version="1",
        approved_by=pid,
        dry_run=True,
    )
    assert preview["ok"] is False
    assert "declared void by ['beta']" in preview["problem"]


# --- T-22: expect_count is mandatory when applying --------------------------


@pytest.mark.requirement("T-22")
def test_t22_applying_without_a_count_is_refused(member):
    settled = _settled(member, "alpha-key", drafts=2)
    with pytest.raises(ValueError, match="'expect_count' is required"):
        server.backfill_superseded(
            dry_run=False,
            ids=settled["drafts"],
            key="alpha-key",
            word_message_id=_word(member, "alpha-key"),
        )


@pytest.mark.requirement("T-22")
def test_t22_a_wrong_count_retires_nothing(member):
    """A second independent channel for one statement: the ids from the
    preview, the number from the letter where the owner gave their word. They
    cannot agree by accident."""
    settled = _settled(member, "alpha-key", drafts=2)
    with pytest.raises(ValueError, match=r"expect_count.*is 3 but"):
        server.backfill_superseded(
            dry_run=False,
            ids=settled["drafts"],
            expect_count=3,
            key="alpha-key",
            word_message_id=_word(member, "alpha-key"),
        )
    member("beta")
    assert len(L(server.awaiting_ack())) == 2  # nothing was withdrawn


# --- T-23: the retirement event names the key and the owner's word ----------


@pytest.mark.requirement("T-23")
def test_t23_the_pass_names_its_key_and_the_owners_word(member):
    """31 records were retired under a generic note, the word covering them
    sits in another letter, and nothing links the two."""
    settled = _settled(member, "alpha-key", drafts=1)
    word = _word(member, "alpha-key")
    server.backfill_superseded(
        dry_run=False,
        ids=settled["drafts"],
        expect_count=1,
        key="alpha-key",
        word_message_id=word,
    )
    event = next(
        e
        for e in L(server.message_history(message_id=settled["drafts"][0]))
        if e["event"] == db.BACKFILL_EVENT
    )
    assert "alpha-key" in event["note"]
    assert f"word: #{word}" in event["note"]


@pytest.mark.requirement("T-23")
def test_t23_a_pass_without_key_or_word_is_refused(member):
    settled = _settled(member, "alpha-key", drafts=1)
    with pytest.raises(ValueError, match="pass 'key' and 'word_message_id'"):
        server.backfill_superseded(dry_run=False, ids=settled["drafts"], expect_count=1)


@pytest.mark.requirement("T-23")
def test_t23_the_word_must_come_from_the_keys_owner(member):
    """Red: passing a word_message_id for a message sent by someone who is NOT
    the key's owner must be refused, or the field gets filled with
    anything."""
    settled = _settled(member, "alpha-key", drafts=1)  # pinned by alpha
    member("beta")
    someone_elses = server.send_message(to="alpha", topic="I am not the owner", body="x")["id"]
    with pytest.raises(ValueError, match="owned by 'alpha'"):
        server.backfill_superseded(
            dry_run=False,
            ids=settled["drafts"],
            expect_count=1,
            key="alpha-key",
            word_message_id=someone_elses,
        )


# --- T-24: a retirement is reversible ---------------------------------------


@pytest.mark.requirement("T-24")
def test_t24_a_cleanup_retirement_can_be_undone(member):
    """Every other item makes the mistake less likely. Reversibility makes it
    fixable — the only protection against a mistake nobody foresaw."""
    settled = _settled(member, "alpha-key", drafts=1)
    gone = settled["drafts"][0]
    server.backfill_superseded(
        dry_run=False,
        ids=[gone],
        expect_count=1,
        key="alpha-key",
        word_message_id=_word(member, "alpha-key"),
    )
    member("beta")
    assert gone not in {m["id"] for m in L(server.awaiting_ack())}

    member("alpha")
    result = server.undo_backfill(ids=[gone], reason="wrong round")
    assert result["restored"] == [gone]

    member("beta")
    assert gone in {m["id"] for m in L(server.awaiting_ack())}
    events = [e["event"] for e in L(server.message_history(message_id=gone))]
    assert db.BACKFILL_EVENT in events and "superseded_undone" in events


@pytest.mark.requirement("T-24")
def test_t24_ordinary_quenching_is_not_undoable(member):
    """Red: an undo of an id retired by a vote or by a new pin version must
    refuse, or ordinary quenching becomes undoable."""
    member("alpha")
    target = _proc(to=["beta", "gamma"], pin_key=None)
    nudge = _proc(to="beta", topic="a vote is needed", about_message_id=target)
    member("beta")
    server.acknowledge(message_id=target, decision="agree")

    member("alpha")
    with pytest.raises(ValueError, match="retired by the live rule"):
        server.undo_backfill(ids=[nudge])


# --- T-27: being multi-claimed blocks rather than informs -------------------


@pytest.mark.requirement("T-27")
def test_t27_a_multi_claimed_id_needs_the_word_of_every_claiming_key(member):
    """Everyone accepted the rule and nothing held it: one role's list still
    contained four multi-claimed records that nobody had named."""
    shared = _legacy(body="about alpha-key and beta-key at once")
    _settled(member, "alpha-key", drafts=1)
    _settled(member, "beta-key", drafts=1)

    preview = server.backfill_superseded(ids=[shared])
    assert preview["multi_claimed"] == [shared]
    assert sorted(preview["would_retire"][0]["claimed_by"]) == ["alpha-key", "beta-key"]

    with pytest.raises(ValueError, match=r"claimed by.*Missing"):
        server.backfill_superseded(
            dry_run=False,
            ids=[shared],
            expect_count=1,
            key="alpha-key",
            word_message_id=_word(member, "alpha-key"),
        )
    # green: a pass that names both keys and both words goes through
    both = server.backfill_superseded(
        dry_run=False,
        ids=[shared],
        expect_count=1,
        key=["alpha-key", "beta-key"],
        word_message_id=[_word(member, "alpha-key"), _word(member, "beta-key")],
    )
    assert both["retired"] == [shared]


# --- T-28: a round on an occupied key does not open silently ----------------


@pytest.mark.requirement("T-28")
def test_t28_a_second_round_on_an_occupied_key_is_refused(member):
    """Three procs with one pin_key within six minutes; the server accepted
    all three."""
    member("alpha")
    first = _proc(to=list(ROLES[1:]), pin_key="team-charter", topic="version 2.0")
    member("beta")
    with pytest.raises(ValueError, match=f"#{first}"):
        _proc(to=["alpha", "gamma", "delta"], pin_key="team-charter", topic="version 1.1")


@pytest.mark.requirement("T-28")
def test_t28_the_refusal_has_a_way_out(member):
    """With no way out, the item swaps one trap for another: a single round
    nobody can close. They are not abandoned out of malice — the session
    ended."""
    member("alpha")
    first = _proc(to=list(ROLES[1:]), pin_key="team-charter")
    member("alpha")
    server.delete_message(message_id=first)  # the author withdrew their own round
    member("beta")
    assert _proc(to=["alpha", "gamma", "delta"], pin_key="team-charter")


@pytest.mark.requirement("T-28")
@pytest.mark.parametrize("voters", ["*", ["beta"]], ids=["everyone", "declared"])
def test_t28_void_also_frees_the_key(member, voters):
    """A void from a voter of the round — every role, or the electorate the
    round declared — closes it and frees the key."""
    member("alpha")
    first = _proc(to=list(ROLES[1:]), pin_key="glossary", voters=voters)
    member("beta")
    server.acknowledge(message_id=first, decision="void", note="the subject is gone")
    with db.open_db() as conn:
        assert db.open_rounds_for_pin(conn, key="glossary") == []
        assert db.round_voided_by(conn, message_id=first) == ["beta"]
    assert _proc(to=["alpha", "gamma", "delta"], pin_key="glossary")


# --- T-29: every snapshot carries the moment it is true for ----------------


@pytest.mark.requirement("T-29")
def test_t29_snapshots_carry_the_moment_they_are_true_for(member):
    member("alpha")
    _proc(to="beta")
    member("beta")
    for call in (
        server.awaiting_ack,
        server.open_obligations,
        server.backfill_superseded,
    ):
        assert call()["generated_at"].endswith("Z")


@pytest.mark.requirement("T-29")
def test_t29_the_refusal_separates_a_typo_from_a_race(member):
    """A role refused on a correct list will learn to fit the number to the
    refusal — if the refusal cannot tell a typo from a race."""
    settled = _settled(member, "alpha-key", drafts=2)
    first, second = settled["drafts"]
    snapshot = server.backfill_superseded()["generated_at"]

    member("gamma")  # somebody else got there first
    server.backfill_superseded(
        dry_run=False,
        ids=[second],
        expect_count=1,
        key="alpha-key",
        word_message_id=_word(member, "alpha-key"),
    )
    member("alpha")
    with pytest.raises(ValueError, match="retired after your snapshot"):
        server.backfill_superseded(
            dry_run=False,
            ids=[first, second],
            expect_count=2,
            key="alpha-key",
            word_message_id=_word(member, "alpha-key"),
            snapshot_at=snapshot,
        )


@pytest.mark.requirement("T-29")
def test_t29_a_vote_cannot_land_on_a_body_you_never_read(member):
    """A vote is the one place where the snapshot and the action diverge
    irreversibly for the round."""
    member("alpha")
    pid = _proc(to="beta", body="revision 1")
    member("beta")
    read = server.get_acknowledgements(message_id=pid)
    member("alpha")
    server.revise_message(message_id=pid, body="revision 2")

    member("beta")
    with pytest.raises(ValueError, match="re-issued after you read it"):
        server.acknowledge(
            message_id=pid,
            decision="agree",
            expect_body_sha256=read["body_sha256"],
        )
    fresh = server.get_acknowledgements(message_id=pid)
    assert server.acknowledge(
        message_id=pid, decision="agree", expect_body_sha256=fresh["body_sha256"]
    )


# --- T-30: the header projection keeps what explains the number ------------


@pytest.mark.requirement("T-30")
def test_t30_headers_explain_why_agreed_is_one_next_to_four_agrees(member):
    """The projection showed a one next to four agrees, and the field that
    explains the difference was not in it. That reads as a broken
    counter."""
    member("alpha")
    pid = _proc(to="beta")
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=pid, decision="agree")

    headers = server.get_acknowledgements(message_id=pid, fields="headers")
    assert headers["agreed"] == 1 and len(headers["decisions"]) == 3
    assert headers["voters"] == ["beta"]
    assert set(headers["from_non_recipients"]) == {"gamma", "delta"}


@pytest.mark.requirement("T-30")
def test_t30_headers_distinguish_no_votes_from_quenched_votes(member):
    """The projection read as "nobody looked", while the truth was "three read
    it, asked for changes, the changes were made, the votes were
    quenched"."""
    member("alpha")
    pid = _proc(to=list(ROLES[1:]), body="revision 1")
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=pid, decision="needs_changes")
    member("alpha")
    server.revise_message(message_id=pid, body="revision 2")

    headers = server.get_acknowledgements(message_id=pid, fields="headers")
    assert headers["agreed"] == 0
    assert set(headers["quenched_by_revision"]) == set(ROLES[1:])


# --- T-31: a document has an object you can submit and verify --------------


@pytest.mark.requirement("T-31")
def test_t31_a_body_too_large_to_type_goes_in_through_an_upload(member):
    """68,054 characters do not fit into one call — not "risky", they do not
    fit."""
    member("alpha")
    part1, part2 = "first half\n" * 200, "second half\n" * 200
    up = server.upload_content(text=part1, label="spec 3.0")
    server.upload_content(text=part2, upload_id=up["upload_id"])
    sealed = server.seal_content(upload_id=up["upload_id"])

    whole = part1 + part2
    assert sealed["sha256"] == db.body_digest(whole)["body_sha256"]
    assert sealed["length_bytes"] == len(whole.encode())

    sent = server.send_message(
        to="beta",
        topic="spec 3.0",
        body_ref=up["upload_id"],
        kind="proc",
        pin_key=None,
        about_message_id=None,
    )
    member("beta")
    row = next(m for m in L(server.read_inbox()) if m["id"] == sent["id"])
    assert row["body"] == whole
    assert row["body_sha256"] == sealed["sha256"]


@pytest.mark.requirement("T-31")
def test_t31_the_document_has_its_own_number_apart_from_the_letter(member):
    """The team's file and the letter's body differed by 5,796 bytes of
    preamble, and the difference was CORRECT: the check did not exist, it had
    not "broken"."""
    member("alpha")
    document = "=== BODY START ===\ndocument contents\n"
    up = server.upload_content(text=document)
    sealed = server.seal_content(upload_id=up["upload_id"])

    server.send_message(
        to="beta",
        topic="document",
        kind="proc",
        pin_key=None,
        about_message_id=None,
        body="voting preamble\n\n" + document,
    )
    # the letter's digest is not the document's — and now both exist
    member("beta")
    letter = next(m for m in L(server.read_inbox()) if m["topic"] == "document")
    assert letter["body_sha256"] != sealed["sha256"]
    assert (
        server.get_content(upload_id=up["upload_id"])["sha256"]
        == (db.body_digest(document)["body_sha256"])
    )


@pytest.mark.requirement("T-31")
def test_t31_a_sealed_upload_cannot_change_under_its_number(member):
    member("alpha")
    up = server.upload_content(text="one")
    server.seal_content(upload_id=up["upload_id"])
    with pytest.raises(ValueError, match="sealed"):
        server.upload_content(text="two", upload_id=up["upload_id"])


@pytest.mark.requirement("T-31")
def test_t31_an_unsealed_upload_cannot_be_used_as_a_body(member):
    member("alpha")
    up = server.upload_content(text="still writing")
    with pytest.raises(ValueError, match="not sealed"):
        server.send_message(
            to="beta",
            topic="too early",
            body_ref=up["upload_id"],
        )


@pytest.mark.requirement("T-31")
def test_t31_a_pin_body_can_be_an_upload_too(member):
    """A pin is the channel's principal document; if the body cannot be
    submitted, it cannot be pinned either."""
    member("alpha")
    text = "charter\n" * 100
    up = server.upload_content(text=text)
    server.seal_content(upload_id=up["upload_id"])
    pid = _proc(to=list(ROLES[1:]), pin_key="team-charter", body=text)
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=pid, decision="agree")
    member("alpha")
    written = server.pin_set(
        key="team-charter",
        title="Charter",
        version="1",
        body_ref=up["upload_id"],
        approved_by=pid,
    )
    assert written["body_sha256"] == db.body_digest(text)["body_sha256"]


# --- T-33: waiting is useless while the role has a backlog -----------------


@pytest.mark.requirement("T-33")
async def test_t33_wait_for_reply_does_not_hand_back_what_you_have_read(member, fast_wait):
    """Three calls in a row returned the same message, marked read half an
    hour earlier."""
    member("alpha")
    asked = server.send_message(to="beta", topic="question", body="?")["id"]
    member("beta")
    answer = server.send_message(
        to="alpha", topic="reply", body="here", kind="answer", reply_to=asked
    )["id"]

    member("alpha")
    first = await server.wait_for_reply(message_id=asked, timeout_s=1)
    assert first["id"] == answer
    server.mark_read(message_id=answer)

    again = await server.wait_for_reply(message_id=asked, timeout_s=1, poll_interval_s=0.1)
    assert again["timed_out"] is True


@pytest.mark.requirement("T-33")
async def test_t33_the_returned_reply_carries_a_true_read_at(member):
    """The returned record carried read_at: null on a message that had been
    marked read."""
    member("alpha")
    asked = _proc(to=["beta", "gamma"], topic="question to everyone")
    member("beta")
    answer = server.send_message(
        to=["alpha", "gamma"],
        topic="reply",
        body="here",
        kind="answer",
        reply_to=asked,
    )["id"]
    member("gamma")
    reply = await server.wait_for_reply(message_id=asked, timeout_s=1)
    assert reply["id"] == answer and reply["read_at"] is None
    server.mark_read(message_id=answer)
    reply = await server.wait_for_reply(message_id=asked, timeout_s=1, include_read=True)
    assert reply["read_at"] is not None


@pytest.mark.requirement("T-33")
async def test_t33_after_id_skips_what_you_already_handled(member):
    member("alpha")
    asked = server.send_message(to="beta", topic="question", body="?")["id"]
    member("beta")
    one = server.send_message(to="alpha", topic="reply 1", body="a", kind="answer", reply_to=asked)[
        "id"
    ]
    two = server.send_message(to="alpha", topic="reply 2", body="b", kind="answer", reply_to=asked)[
        "id"
    ]
    member("alpha")
    assert (await server.wait_for_reply(message_id=asked, timeout_s=1))["id"] == one
    later = await server.wait_for_reply(message_id=asked, timeout_s=1, after_id=one)
    assert later["id"] == two


@pytest.mark.requirement("T-33")
async def test_t33_a_standing_round_does_not_wake_a_sleeper(member, fast_wait):
    """infra's red test: the role has exactly one record in awaiting_ack, a
    live round whose decision the role is deliberately postponing."""
    member("alpha")
    _proc(to="beta")
    member("beta")
    result = await server.wait_for_mail(timeout_s=1, poll_interval_s=0.1)
    assert result["timed_out"] is True
    assert result["pending_at_entry"] == {"unread": 1, "awaiting_ack": 1}


@pytest.mark.requirement("T-33")
async def test_t33_but_something_new_still_wakes_it(member, fast_wait):
    """The fix must not turn into "never wakes anyone"."""
    import asyncio

    member("alpha")
    _proc(to="beta")
    member("beta")
    waiter = asyncio.create_task(server.wait_for_mail(timeout_s=30, poll_interval_s=0.1))
    await fast_wait.until_polled()  # the backlog is recorded; the waiter sleeps
    member("alpha")
    server.send_message(to="beta", topic="new", body="x")
    member("beta")
    result = await asyncio.wait_for(waiter, timeout=5)
    assert result["timed_out"] is False
    assert result["pending"]["unread"] == 2
    assert result["pending_at_entry"]["awaiting_ack"] == 1


# --- T-25: what shipped is visible through a call ---------------------------


@pytest.mark.requirement("T-25")
def test_t25_a_role_is_told_what_changed_without_reading_our_documents(member):
    """The items are called T-18, T-27 — numbers from a document the roles
    never saw. A number cannot tell you the rules moved; you have to say what
    to do differently now."""
    member("alpha")
    news = server.channel_status()["server"]["whats_new"]
    assert news, "the first call on a new build must explain what changed"
    for item in news:
        assert item["what"] and item["do"]
        # no T-numbers: that is the language of our correspondence, not their work
        assert "T-" not in item["what"] and "T-" not in item["do"]
    joined = " ".join(i["do"] for i in news)
    for named in (
        "pin_key",
        "about_message_id",
        "void",
        "revise_message",
        "upload_content",
        "body_ref",
        "wait_for_mail",
        "expect_count",
        "undo_backfill",
        "generated_at",
    ):
        assert named in joined, f"{named} is named nowhere"


@pytest.mark.requirement("T-25")
def test_t25_the_notice_is_shown_once_and_then_stops(member):
    """A notice that repeats forever is the thing everyone learns to scroll
    past. And everyone still has to learn that the rules changed."""
    member("alpha")
    assert "whats_new" in server.channel_status()["server"]
    assert "whats_new" not in server.channel_status()["server"]
    # …but it has not been shown to the other role yet
    member("beta")
    assert "whats_new" in server.channel_status()["server"]
    # …and stays available on demand rather than being lost for good
    assert server.server_build()["whats_new"]


@pytest.mark.requirement("T-25")
def test_t25_the_channel_points_at_its_own_documentation(member):
    """From inside the channel it must be clear where to read the rest."""
    member("alpha")
    block = server.channel_status()["server"]
    assert "get_protocol" in block["contract"]
    assert "server_build" in block["changes"]


@pytest.mark.requirement("T-25")
async def test_t25_the_tools_name_the_way_out_of_the_walls_they_put_up(member):
    """A refusal and a description are the only documentation that definitely
    gets read: it is read at the moment someone hits it."""
    tools = {t.name: t.description or "" for t in await server.mcp.list_tools()}
    # the body does not fit in a call → where that is documented
    assert "upload_content" in tools["send_message"]
    # a dead round → what closes it
    assert "void" in tools["awaiting_ack"]
    # the protocol is long → where to start
    assert "server_build" in tools["get_protocol"]

    member("alpha")
    first = _proc(to=list(ROLES[1:]), pin_key="team-charter")
    member("beta")
    with pytest.raises(ValueError) as refusal:
        _proc(to=["alpha", "gamma", "delta"], pin_key="team-charter")
    # the refusal names both what blocks you and every way out
    text = str(refusal.value)
    assert f"#{first}" in text
    assert "pin_set" in text and "delete_message" in text and "void" in text


@pytest.mark.requirement("T-25")
def test_t25_the_running_server_says_what_it_implements(member):
    """A deployment used to be discovered by calls that should have failed not
    failing."""
    build = server.server_build()
    shipped = {item["id"] for item in build["shipped"]}
    assert {
        "T-01",
        "T-02",
        "T-03",
        "T-04",
        "T-10",
        "T-11",
        "T-13",
        "T-14",
        "T-15",
        "T-17",
    } <= shipped
    assert all(item["shipped_at"] for item in build["shipped"])
    assert build["version"]
    # and what is deliberately absent says so, instead of looking missing
    assert {item["id"] for item in build["not_shipped"]} >= {"T-16", "T-26"}
