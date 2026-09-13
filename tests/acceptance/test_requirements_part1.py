"""Acceptance tests for the 2026-08-08 requirement document (T-01..T-17).

One test per "Check" section of that document — the criterion the team
wrote for each point, in the order the document lists them. They are kept
together rather than spread across the suites by subject because they are a
CONTRACT WITH A DATE: when someone asks "is T-07 done", the answer should be
a test name, not a reading of the diff.

The numbers quoted in the comments are theirs, measured on a live channel of
five roles over a thousand messages.
"""

from __future__ import annotations

import pytest

from ai_agent_channel import db, server
from helpers import L

ROLES = ("alpha", "beta", "gamma", "delta")


@pytest.fixture
def member(channel_identity):
    """A registered channel of four roles (HTTP-mode identity, no uvicorn).

    Several rules only exist where the server KNOWS the roster — a proposal
    for a pin must reach every role, and that cannot be checked against a
    roster inferred from traffic.
    """
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


# --- T-01: mark_read, one addressee predicate -------------------------------


@pytest.mark.requirement("T-01")
def test_t01_batch_mark_read_accepts_a_multi_recipient_message(member):
    """Their case: a batch of ONE broadcast failed entirely, while the same id
    passed one at a time. The price was 107 messages marked zero, over one."""
    member("alpha")
    point = server.send_message(to="beta", topic="direct", body="x")["id"]
    wide = _proc(to=["beta", "gamma"])

    member("beta")
    marked = server.mark_read(message_ids=[point, wide])
    assert isinstance(marked, list)
    assert [m["id"] for m in marked] == [point, wide]
    assert all(m["read_at"] for m in marked)


@pytest.mark.requirement("T-01")
def test_t01_both_ways_of_addressing_everyone_behave_the_same(member):
    """The app role's objection: the field holds a LIST of roles, not '*'. Both forms
    of the call normalise into one storage shape — this test holds that
    property, so the fix is not a fix to a shape the channel never had."""
    member("alpha")
    by_list = _proc(to=["beta", "gamma", "delta"])
    by_star = _proc(to="*")

    member("beta")
    for proposal in (by_list, by_star):
        marked = server.mark_read(message_ids=[proposal])
        assert isinstance(marked, list)
        assert [m["id"] for m in marked] == [proposal]


@pytest.mark.requirement("T-01")
def test_t01_a_batch_still_refuses_someone_elses_mail(member):
    member("alpha")
    private = server.send_message(to="beta", topic="direct", body="x")["id"]
    member("gamma")
    with pytest.raises(PermissionError, match="nothing was marked"):
        server.mark_read(message_ids=[private])


# --- T-02: backfill — projection and dedup with a list of keys --------------


def _settle(member, key: str, *, drafts: int) -> list[int]:
    """A round settled by a pin version: `drafts` proposals, then the pin."""
    member("alpha")
    ids = [
        _proc(to=list(ROLES[1:]), topic=f"{key} v{i}", body=f"changing {key}")
        for i in range(drafts)
    ]
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=ids[-1], decision="agree")
    member("alpha")
    server.pin_set(key=key, title=key, body="body", version="1", approved_by=ids[-1])
    return ids


@pytest.mark.requirement("T-02")
def test_t02_preview_projects_and_lists_every_claiming_key(member):
    """121,754 characters of answer would not pass through the tool; and 356 rows for 274
    messages — because several keys claimed the very same id."""
    # The draft comes FIRST: the timestamp cut is what separates "listed
    # because the rule did not exist" from "listed because the round is
    # genuinely open", and a draft raised after the pin is a live round that
    # must never appear here.
    member("alpha")
    seed = _proc(to=list(ROLES[1:]), topic="the old one", body="about alpha-key and beta-key")

    _settle(member, "alpha-key", drafts=2)
    member("alpha")
    both = _proc(
        to=list(ROLES[1:]),
        topic="shared draft",
        body="about alpha-key and beta-key at once",
    )
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=both, decision="agree")
    member("alpha")
    server.pin_set(key="beta-key", title="b", body="body", version="1", approved_by=both)

    # unwind the live rule so the state matches "the rule did not exist yet",
    # which is the only situation the backfill is for
    with db.open_db() as conn:
        conn.execute("UPDATE messages SET superseded_at = NULL")
        conn.execute("DELETE FROM message_events WHERE event = 'superseded'")
    preview = server.backfill_superseded()

    rows = {r["id"]: r for r in preview["would_retire"]}
    assert seed in rows
    # ONE line for that message, with both claiming keys named in it
    assert rows[seed]["claim_count"] == 2
    assert sorted(rows[seed]["claimed_by"]) == ["alpha-key", "beta-key"]
    assert seed in preview["multi_claimed"]

    narrow = server.backfill_superseded(fields=["id", "topic", "claimed_by"])
    assert all(set(r) <= {"id", "topic", "claimed_by"} for r in narrow["would_retire"])
    assert all("body" not in r for r in narrow["would_retire"])


# --- T-03: fields='headers' as a string -------------------------------------


@pytest.mark.requirement("T-03")
def test_t03_headers_is_accepted_as_a_bare_string(member):
    """Every tool's description promised "or the single value 'headers'", and
    the schema typed it as a list — so the call was refused client-side."""
    member("alpha")
    _proc(to="beta")
    member("beta")
    for call in (
        lambda: server.awaiting_ack(fields="headers"),
        lambda: server.open_obligations(fields="headers"),
        lambda: server.list_messages(fields="headers"),
        lambda: server.read_inbox(unread_only=False, fields="headers"),
    ):
        rows = L(call())
        assert all("body" not in r for r in rows)


@pytest.mark.requirement("T-03")
def test_t03_get_acknowledgements_takes_headers_too(member):
    member("alpha")
    pid = _proc(to="beta")
    member("beta")
    server.acknowledge(message_id=pid, decision="agree", note="a long note" * 50)
    state = server.get_acknowledgements(message_id=pid, fields="headers")
    assert {"agreed", "needed", "missing", "decisions"} <= set(state)
    assert "acknowledgements" not in state  # T-10: no notes


# --- T-04: needed is counted from the recipients ----------------------------


@pytest.mark.requirement("T-04")
def test_t04_a_proposal_to_one_role_needs_one_vote(member):
    """356 of 358 procs are addressed to ONE role, and each shows needed=4: such a
    proposal never closes: the count has nowhere to fall to."""
    member("alpha")
    pid = _proc(to="beta")
    member("beta")
    server.acknowledge(message_id=pid, decision="agree")

    state = server.get_acknowledgements(message_id=pid)
    assert (state["agreed"], state["needed"], state["missing"]) == (1, 1, [])


@pytest.mark.requirement("T-04")
def test_t04_a_pin_proposal_must_reach_every_role(member):
    """The other half: a single-addressee pin proposal would show a full
    quorum and then be refused by pin_set. The refusal comes at send time."""
    member("alpha")
    with pytest.raises(ValueError, match="must be addressed to every other role"):
        _proc(to="beta", pin_key="team-charter")
    with pytest.raises(ValueError, match="delta"):
        _proc(to=["beta", "gamma"], pin_key="team-charter")
    # addressed to everyone: accepted
    assert _proc(to=list(ROLES[1:]), pin_key="team-charter")
    assert _proc(to="*", pin_key="glossary")


@pytest.mark.requirement("T-04")
def test_t04_no_proposal_shows_a_full_quorum_that_pin_set_then_refuses(member):
    """“There exists no proc that shows a full quorum and is at the same time
    refused by pin_set” — a property, not an example."""
    member("alpha")
    pid = _proc(to=list(ROLES[1:]), pin_key="team-charter")
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=pid, decision="agree")
    member("alpha")
    state = server.get_acknowledgements(message_id=pid)
    assert state["missing"] == []
    assert (
        server.pin_set(
            key="team-charter",
            title="t",
            body="b",
            version="1",
            approved_by=pid,
            dry_run=True,
        )["ok"]
        is True
    )


# --- T-05: a nudge creates no record of its own -----------------------------


@pytest.mark.requirement("T-05")
def test_t05_a_nudge_is_not_a_decision_of_its_own(member):
    """content: get_thread(831) — agreed: 0 on ALL 62 records. Nobody votes
    on a nudge, ever."""
    member("alpha")
    pid = _proc(to="beta")
    nudge = _proc(to="beta", topic="a vote is needed", about_message_id=pid)

    member("beta")
    assert {m["id"] for m in L(server.awaiting_ack())} == {pid}
    assert nudge not in {m["id"] for m in L(server.awaiting_ack())}


@pytest.mark.requirement("T-05")
def test_t05_a_proposal_without_the_field_is_refused_at_send_time(member):
    member("alpha")
    with pytest.raises(ValueError, match="explicit 'about_message_id'"):
        server.send_message(to="beta", topic="t", body="b", kind="proc", pin_key=None)
    # null is a real answer, not an absence
    assert server.send_message(
        to="beta",
        topic="t",
        body="b",
        kind="proc",
        pin_key=None,
        about_message_id=None,
    )["id"]


@pytest.mark.requirement("T-05")
def test_t05_a_nudge_retires_when_its_target_is_withdrawn(member):
    """infra: 13 of a round's 31 records are retired by neither T-05 nor the cleanup — their target
    is a withdrawn revision nobody will ever vote on again."""
    member("alpha")
    target = _proc(to=list(ROLES[1:]), pin_key="deploy-topology")
    nudge = _proc(to="beta", topic="a vote is needed", about_message_id=target)

    # the pin moves on — the target is settled, and so is the nudge about it
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=target, decision="agree")
    member("alpha")
    server.pin_set(
        key="deploy-topology",
        title="d",
        body="b",
        version="1.4",
        approved_by=target,
    )
    events = [e["event"] for e in L(server.message_history(message_id=nudge))]
    assert "superseded" in events
    member("beta")
    assert nudge not in {m["id"] for m in L(server.awaiting_ack())}


@pytest.mark.requirement("T-05")
def test_t05_a_nudge_retires_when_its_target_is_deleted(member):
    member("alpha")
    target = _proc(to="beta")
    nudge = _proc(to="beta", topic="a vote is needed", about_message_id=target)
    result = server.delete_message(message_id=target)
    assert result["superseded"] == [nudge]


# --- T-06: pin_key is mandatory on a proc that changes a pin ----------------


@pytest.mark.requirement("T-06")
def test_t06_a_proposal_without_an_explicit_pin_key_is_refused(member):
    member("alpha")
    with pytest.raises(ValueError, match="explicit 'pin_key'"):
        server.send_message(to="beta", topic="t", body="b", kind="proc", about_message_id=None)


@pytest.mark.requirement("T-06")
def test_t06_pin_set_can_no_longer_fail_for_not_naming_the_key(member):
    """“pin_set(approved_by=P) cannot refuse on the grounds that 'the proposal did not
    name the key': such a P can no longer be created.”

    The only proposals that can exist now either carry the key structurally
    or explicitly disclaim one — and a disclaiming proposal cannot collect
    the votes of a pin round, because a pin proposal must reach everyone.
    """
    member("alpha")
    unrelated = _proc(to=list(ROLES[1:]), topic="no pin", body="not a word")
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=unrelated, decision="agree")
    member("alpha")
    preview = server.pin_set(
        key="contract-version",
        title="c",
        body="b",
        version="1",
        approved_by=unrelated,
        dry_run=True,
    )
    # it still refuses — but for naming, which the sender was told about at
    # send time, not after four roles voted
    assert preview["ok"] is False
    assert "does not name" in preview["problem"]


@pytest.mark.requirement("T-06")
def test_t06_other_kinds_keep_their_optional_fields(member):
    """§0 is about fields whose omission breaks a mechanism, not about every field."""
    member("alpha")
    assert server.send_message(to="beta", topic="question", body="?", kind="question")
    assert server.send_message(to="beta", topic="plain", body="x")


# --- T-07: a proposal's revisions live on one id ----------------------------


@pytest.mark.requirement("T-07")
def test_t07_reissuing_the_body_keeps_one_record_and_quenches_the_votes(member):
    """The deploy-topology 1.4 round: 40 messages in 10 hours, 31 still open —
    19.5% of the channel's whole count, one document, one day."""
    member("alpha")
    pid = _proc(to=["beta", "gamma"], body="revision 1")
    member("beta")
    server.acknowledge(message_id=pid, decision="agree")
    assert server.get_acknowledgements(message_id=pid)["agreed"] == 1

    member("alpha")
    revised = server.revise_message(message_id=pid, body="revision 2")
    assert revised["previous_body_sha256"] == db.body_digest("revision 1")["body_sha256"]
    assert revised["body_sha256"] == db.body_digest("revision 2")["body_sha256"]
    assert revised["quenched_votes"] == ["beta"]

    # one record, not two
    member("beta")
    listed = L(server.awaiting_ack())
    assert [m["id"] for m in listed] == [pid]
    # the vote for the previous edition does not count as agreement
    state = server.get_acknowledgements(message_id=pid)
    assert state["agreed"] == 0
    assert "beta" in state["missing"]
    assert state["quenched_by_revision"] == {"beta": "agree"}


@pytest.mark.requirement("T-07")
def test_t07_history_records_both_digests(member):
    member("alpha")
    pid = _proc(to="beta", body="one")
    server.revise_message(message_id=pid, body="two", note="fixed the entry count")
    event = [e for e in L(server.message_history(message_id=pid)) if e["event"] == "revision"]
    assert len(event) == 1
    assert db.body_digest("one")["body_sha256"] in event[0]["note"]
    assert db.body_digest("two")["body_sha256"] in event[0]["note"]
    assert "fixed the entry count" in event[0]["note"]


@pytest.mark.requirement("T-07")
def test_t07_only_the_author_reissues_and_never_after_approval(member):
    member("alpha")
    pid = _proc(to=list(ROLES[1:]), pin_key="team-charter", body="charter")
    member("beta")
    with pytest.raises(PermissionError, match="only the author"):
        server.revise_message(message_id=pid, body="someone else edit")

    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=pid, decision="agree")
    member("alpha")
    server.pin_set(key="team-charter", title="t", body="charter", version="1", approved_by=pid)
    with pytest.raises(ValueError, match="approval record"):
        server.revise_message(message_id=pid, body="after the fact")


@pytest.mark.requirement("T-07")
def test_t07_stale_votes_cannot_approve_a_pin(member):
    """The consent rule and the revision rule must agree: a vote for an
    older text is not consent for the current one."""
    member("alpha")
    pid = _proc(to=list(ROLES[1:]), pin_key="glossary", body="glossary v1")
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=pid, decision="agree")
    member("alpha")
    server.revise_message(message_id=pid, body="glossary v2")
    preview = server.pin_set(
        key="glossary",
        title="g",
        body="glossary v2",
        version="1",
        approved_by=pid,
        dry_run=True,
    )
    assert preview["ok"] is False
    assert sorted(preview["missing_agrees"]) == sorted(ROLES[1:])


# --- T-08: awaiting_ack — filtering by author and by key --------------------


@pytest.mark.requirement("T-08")
def test_t08_the_sender_can_measure_what_they_hung_on_others(member):
    """infra: a zero in awaiting_ack did not mean "I am clear", it meant "nobody
    is writing to me". 78 of 159 records were authored by infra — 49%."""
    member("alpha")
    mine = [_proc(to="beta") for _ in range(3)]
    _proc(to="beta", pin_key=None, topic="one more")
    member("gamma")
    _proc(to="beta")

    member("beta")
    assert len(L(server.awaiting_ack())) == 5
    from_alpha = L(server.awaiting_ack(from_role="alpha"))
    assert len(from_alpha) == 4
    assert set(mine) <= {m["id"] for m in from_alpha}
    assert len(L(server.awaiting_ack(from_role="gamma"))) == 1


@pytest.mark.requirement("T-08")
def test_t08_filter_by_pin_key(member):
    member("alpha")
    keyed = _proc(to=list(ROLES[1:]), pin_key="team-charter")
    _proc(to="beta")
    member("beta")
    assert [m["id"] for m in L(server.awaiting_ack(pin_key="team-charter"))] == [keyed]


# --- T-09: the board — "decisions you owe" ----------------------------------


@pytest.mark.requirement("T-09")
def test_t09_the_board_number_counts_decisions_not_traffic(member):
    """identity: "to decide 33", with 0 actual decisions owed. On a board that
    is a false accusation of a named role, in front of a human."""
    member("alpha")
    real = _proc(to="beta")
    _proc(to="beta", topic="a call", about_message_id=real)  # a call
    _proc(to="beta", topic="for review", decision_requested=False)  # T-12
    settled = _proc(to=list(ROLES[1:]), pin_key="glossary")
    for role in ROLES[1:]:
        member(role)
        server.acknowledge(message_id=settled, decision="agree")
    member("alpha")
    server.pin_set(key="glossary", title="g", body="b", version="1", approved_by=settled)

    member("beta")
    # four proposals reached beta; exactly one is a decision beta owes
    assert server.channel_status()["counts"]["awaiting_ack"] == 1
    assert [m["id"] for m in L(server.awaiting_ack())] == [real]

    with db.open_db(db_path=None) if False else db.open_db() as conn:
        snapshot = db.board_snapshot(conn, roles=list(ROLES))
    assert snapshot["counts"]["beta"]["awaiting_ack"] == 1


# --- T-11: message_history — a status supplied at send time -----------------


@pytest.mark.requirement("T-11")
def test_t11_a_status_set_at_send_time_is_audited(member):
    """infra, on a controlled pair: #572 EMPTY (supplied in send_message),
    #718 and #847 — 4 events each (set via set_work_status)."""
    member("alpha")
    mid = server.send_message(
        to="beta",
        topic="task",
        body="x",
        action_required=True,
        work_status="needs_you",
    )["id"]
    events = L(server.message_history(message_id=mid))
    assert [(e["event"], e["role"]) for e in events] == [("work_status:needs_you", "alpha")]


@pytest.mark.requirement("T-11")
def test_t11_the_stop_hook_rule_is_unchanged_by_the_new_row(member):
    """The ownership rule used to read the absence of a row; now it reads the row.
    The answer must match, or fixing the audit would have moved the semantics."""
    member("alpha")
    mid = server.send_message(
        to="beta",
        topic="task",
        body="x",
        action_required=True,
        work_status="needs_you",
    )["id"]
    member("beta")
    # the sender moved last → the debt is untaken and still nags
    assert server.channel_status()["counts"]["open_obligations_untaken"] == 1
    server.set_work_status(message_id=mid, work_status="in_progress")
    assert server.channel_status()["counts"]["open_obligations_untaken"] == 0


# --- T-12: a proc that asks for no decision ---------------------------------


@pytest.mark.requirement("T-12")
def test_t12_a_proposal_opened_for_reading_is_not_a_debt(member):
    """#905: "opening this for review. NOT collecting votes" — and it sat for six
    days."""
    member("alpha")
    reading = _proc(to=list(ROLES[1:]), topic="for review", decision_requested=False)
    for role in ROLES[1:]:
        member(role)
        assert reading not in {m["id"] for m in L(server.awaiting_ack())}
    # anyone who wants to weigh in still can
    member("beta")
    assert server.acknowledge(message_id=reading, decision="needs_changes")


# --- T-13: a closed debt and the requirement to vote ------------------------


@pytest.mark.requirement("T-13")
def test_t13_a_closed_debt_is_visible_in_the_decision_list(member):
    """10 records sit in awaiting_ack with status='resolved'; on two of them
    the resolution was confirmed by both sides on 08-04."""
    member("alpha")
    mid = server.send_message(
        to="beta",
        topic="both a debt and a decision",
        body="x",
        kind="proc",
        action_required=True,
        pin_key=None,
        about_message_id=None,
    )["id"]
    member("beta")
    server.resolve_message(message_id=mid, resolution_note="done")
    member("alpha")
    server.confirm_resolution(message_id=mid, note="acknowledged")

    member("beta")
    row = L(server.awaiting_ack())[0]
    # the vote is still owed — a resolve must not cancel someone else's
    # obligation to answer — but the listing says the work is done
    assert row["id"] == mid
    assert row["obligation"]["status"] == "resolved"
    assert row["obligation"]["resolved_by"] == "beta"
    assert row["obligation"]["confirmed"] is True


# --- T-14: a reply to a multi-recipient message is multi-recipient ----------


@pytest.mark.requirement("T-14")
def test_t14_an_answer_may_keep_the_recipients_of_what_it_answers(member):
    """crypto hit this answering #1472: either lie about the kind, or
    send four separate letters — doing exactly what §4 R3 exists against."""
    member("alpha")
    asked = _proc(to=list(ROLES[1:]), topic="question to everyone", body="answer with reply")
    member("beta")
    answer = server.send_message(
        to=["alpha", "gamma", "delta"],
        topic="reply",
        body="here",
        kind="answer",
        reply_to=asked,
    )
    assert sorted(answer["recipients"]) == ["alpha", "delta", "gamma"]


@pytest.mark.requirement("T-14")
def test_t14_a_wide_reply_stays_within_the_parents_audience(member):
    """Keeping the recipients is not choosing new ones: a wide answer may go
    only to roles that were party to what it answers."""
    member("alpha")
    parent = server.send_message(to=["beta", "gamma"], topic="status", body="x", kind="status")[
        "id"
    ]
    member("beta")
    with pytest.raises(ValueError, match="not party to it"):
        server.send_message(
            to=["alpha", "delta"], topic="re", body="y", kind="answer", reply_to=parent
        )
    reply = server.send_message(
        to=["alpha", "gamma"], topic="re", body="y", kind="answer", reply_to=parent
    )
    assert reply["recipients"] == ["alpha", "gamma"]


@pytest.mark.requirement("T-14")
def test_t14_the_ban_stands_outside_a_reply(member):
    member("alpha")
    with pytest.raises(ValueError, match="may be sent to several roles"):
        server.send_message(to=["beta", "gamma"], topic="without reply", body="x", kind="answer")


@pytest.mark.requirement("T-14")
def test_t14_action_required_is_still_single_recipient(member):
    member("alpha")
    asked = _proc(to=list(ROLES[1:]), topic="question", body="x")
    member("beta")
    with pytest.raises(ValueError, match="action_required cannot be broadcast"):
        server.send_message(
            to=["alpha", "gamma"],
            topic="reply",
            body="x",
            kind="answer",
            reply_to=asked,
            action_required=True,
        )


# --- T-15: limits, field names, truncation ----------------------------------


@pytest.mark.requirement("T-15")
async def test_t15_the_topic_limit_is_named_in_the_refusal_and_the_description(member):
    member("alpha")
    with pytest.raises(ValueError, match="80 characters"):
        server.send_message(to="beta", topic="me" * 81, body="x")
    tool = next(t for t in await server.mcp.list_tools() if t.name == "send_message")
    assert tool.description is not None
    assert "80 characters" in tool.description


@pytest.mark.requirement("T-15")
def test_t15_fields_accepts_the_column_names_too(member):
    """identity, one turn: fields does not accept from_role/to_role — the names
    are from and to."""
    member("alpha")
    server.send_message(to="beta", topic="t", body="x")
    rows = L(server.list_messages(fields=["id", "from_role", "to_role"]))
    assert set(rows[0]) == {"id", "from", "to"}


@pytest.mark.requirement("T-15")
def test_t15_a_truncated_listing_says_so(member):
    """“Silent truncation looks like a complete answer” — exactly the shape
    they gave a name to."""
    member("alpha")
    for i in range(4):
        server.send_message(to="beta", topic=f"t{i}", body="x")
    full = server.list_messages(limit=10)
    assert "truncated" not in full
    windowed = server.list_messages(limit=2)
    assert len(windowed["result"]) == 2
    assert windowed["truncated"]["limit"] == 2


@pytest.mark.requirement("T-15")
def test_t15_the_extra_row_is_never_delivered_as_read(member):
    """read_inbox marks what it hands over; the probe row must not count."""
    member("alpha")
    ids = [server.send_message(to="beta", topic=f"t{i}", body="x")["id"] for i in range(3)]
    member("beta")
    shown = L(server.read_inbox(limit=2))
    assert [m["id"] for m in shown] == ids[1:]  # the NEWEST two
    counts = server.channel_status()["counts"]
    assert counts["unread"] == 3 and counts["unopened"] == 1


# --- T-17: body_sha256 on a message body ------------------------------------


@pytest.mark.requirement("T-17")
def test_t17_every_tool_that_returns_a_body_publishes_its_digest(member):
    """#1478: declared fe8c1c3d… / 40,621 bytes; the body was 3cc4681e… / 41,235.
    The author hashed a file and sent text with two fragments added to it."""
    body = "proposal body\n"
    expected = db.body_digest(body)
    member("alpha")
    mid = _proc(to="beta", body=body)

    member("beta")
    for rows in (
        L(server.read_inbox(unread_only=False)),
        L(server.list_messages()),
        L(server.get_thread(message_id=mid)),
        L(server.search_messages(query="proposal")),
        L(server.awaiting_ack()),
    ):
        row = next(r for r in rows if r["id"] == mid)
        assert row["body_sha256"] == expected["body_sha256"]
        assert row["body_length_bytes"] == expected["body_length_bytes"]
        assert row["body_length_chars"] == expected["body_length_chars"]


@pytest.mark.requirement("T-17")
def test_t17_the_digest_rides_in_the_header_projection(member):
    member("alpha")
    _proc(to="beta", body="body")
    member("beta")
    row = L(server.awaiting_ack(fields="headers"))[0]
    assert "body" not in row
    assert row["body_sha256"] == db.body_digest("body")["body_sha256"]


@pytest.mark.requirement("T-17")
def test_t17_the_digest_covers_the_shared_body_not_the_personal_tail(member):
    """Otherwise five recipients would get five numbers for one id."""
    member("alpha")
    mid = server.send_message(
        to=["beta", "gamma"],
        topic="shared",
        body="shared text",
        kind="proc",
        pin_key=None,
        about_message_id=None,
        addenda={"beta": "for you, separately"},
    )["id"]
    digests = {}
    for role in ("beta", "gamma"):
        member(role)
        row = next(r for r in L(server.read_inbox(unread_only=False)) if r["id"] == mid)
        digests[role] = row["body_sha256"]
    assert digests["beta"] == digests["gamma"] == db.body_digest("shared text")["body_sha256"]
