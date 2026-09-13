"""Making the state of a consent round readable, and splitting the counter
that was measuring discipline instead of state.
"""

from __future__ import annotations

from pathlib import Path

from ai_agent_channel import server
from helpers import L


def _proposal(as_role, to="backend"):
    as_role("frontend")
    return server.send_message(
        to=to, topic="proposal", body="text", kind="proc", pin_key=None, about_message_id=None
    )["id"]


# --- item 6: the channel's roster -------------------------------------------


def test_list_roles_in_stdio_mode_reports_who_has_spoken(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="t", body="b")
    server.send_message(to="infra", topic="t", body="b")
    roles = server.list_roles()
    assert roles["roles"] == ["backend", "frontend", "infra"]
    assert roles["you"] == "frontend"
    # honest about where the answer came from: stdio has no registry, so a
    # silent member is simply not visible
    assert roles["source"] == "observed-in-messages"


# --- item 6: who we are waiting for -----------------------------------------


def test_get_acknowledgements_names_who_is_missing(db_path: Path, as_role):
    pid = _proposal(as_role)
    as_role("infra")
    server.send_message(to="frontend", topic="hi", body="x")  # infra joins
    as_role("backend")
    server.acknowledge(message_id=pid, decision="agree")

    state = server.get_acknowledgements(message_id=pid)
    assert state["agreed"] == 1
    # 'needed' counts the RECIPIENTS, not the roster: this proposal went to
    # backend alone, so one vote closes it. Counting the roster meant infra
    # was demanded a vote on a letter it never received — and the round could
    # then never close, because the number had nowhere to fall.
    assert state["needed"] == 1
    assert state["missing"] == []
    assert state["decisions"] == {"backend": "agree"}


def test_needed_follows_the_recipients_not_the_roster(db_path: Path, as_role):
    as_role("frontend")
    for role in ("backend", "infra"):
        server.send_message(to=role, topic="hi", body="x")  # roster of three
    wide = server.send_message(
        to=["backend", "infra"],
        topic="shared",
        body="x",
        kind="proc",
        pin_key=None,
        about_message_id=None,
    )["id"]
    narrow = server.send_message(
        to="backend", topic="narrow", body="x", kind="proc", pin_key=None, about_message_id=None
    )["id"]

    as_role("backend")
    server.acknowledge(message_id=wide, decision="agree")
    server.acknowledge(message_id=narrow, decision="agree")

    assert server.get_acknowledgements(message_id=narrow)["needed"] == 1
    assert server.get_acknowledgements(message_id=narrow)["missing"] == []
    wide_state = server.get_acknowledgements(message_id=wide)
    assert wide_state["needed"] == 2 and wide_state["missing"] == ["infra"]


def test_a_vote_from_a_non_recipient_is_shown_but_not_counted(db_path: Path, as_role):
    """acknowledge works on messages addressed to others — that is
    deliberate. Such a vote must neither inflate the count nor disappear."""
    as_role("frontend")
    for role in ("backend", "infra"):
        server.send_message(to=role, topic="hi", body="x")
    pid = server.send_message(
        to="backend", topic="narrow", body="x", kind="proc", pin_key=None, about_message_id=None
    )["id"]
    as_role("infra")
    server.acknowledge(message_id=pid, decision="agree")

    state = server.get_acknowledgements(message_id=pid)
    assert state["needed"] == 1 and state["agreed"] == 0
    assert state["missing"] == ["backend"]
    assert state["from_non_recipients"] == {"infra": "agree"}


def test_author_is_never_counted_as_a_missing_voter(db_path: Path, as_role):
    pid = _proposal(as_role)
    as_role("backend")
    server.acknowledge(message_id=pid, decision="agree")
    state = server.get_acknowledgements(message_id=pid)
    assert "frontend" not in state["missing"]
    assert state["missing"] == []


def test_prose_agreement_shows_up_as_missing(db_path: Path, as_role):
    """The failure this exists for: a role answers 'agree' in a reply and
    never calls acknowledge. The thread reads as consent; the record says
    otherwise, and the tally is what makes the difference visible."""
    pid = _proposal(as_role)
    as_role("backend")
    server.send_message(
        to="frontend",
        topic="re: proposal",
        body="agree, let us go",
        reply_to=pid,
        kind="answer",
    )
    thread = L(server.get_thread(message_id=pid))
    proposal = next(m for m in thread if m["id"] == pid)
    assert proposal["acks"]["agreed"] == 0
    assert proposal["acks"]["missing"] == ["backend"]


def test_the_tally_travels_with_every_view(db_path: Path, as_role):
    pid = _proposal(as_role)
    as_role("backend")
    for rows in (
        L(server.list_messages()),
        L(server.read_inbox()),
        L(server.awaiting_ack()),
        L(server.get_thread(message_id=pid)),
        L(server.search_messages(query="proposal")),
    ):
        row = next(m for m in rows if m["id"] == pid)
        assert row["acks"]["missing"] == ["backend"], rows


def test_non_proposals_are_not_annotated(db_path: Path, as_role):
    as_role("frontend")
    plain = server.send_message(to="backend", topic="fyi", body="x")["id"]
    row = next(m for m in L(server.list_messages()) if m["id"] == plain)
    assert "acks" not in row


# --- item 10: two numbers instead of one -----------------------------------


def test_reading_moves_a_message_between_the_two_counters(db_path: Path, as_role):
    as_role("frontend")
    mid = server.send_message(to="backend", topic="t", body="b")["id"]
    as_role("backend")

    counts = server.channel_status()["counts"]
    assert (counts["unread"], counts["unopened"], counts["opened_unmarked"]) == (1, 1, 0)

    L(server.read_inbox())
    counts = server.channel_status()["counts"]
    assert (counts["unread"], counts["unopened"], counts["opened_unmarked"]) == (1, 0, 1)

    server.mark_read(message_id=mid)
    counts = server.channel_status()["counts"]
    assert (counts["unread"], counts["unopened"], counts["opened_unmarked"]) == (0, 0, 0)


def test_unread_stays_the_sum(db_path: Path, as_role):
    as_role("frontend")
    for n in range(4):
        server.send_message(to="backend", topic=f"t{n}", body="b")
    as_role("backend")
    L(server.read_inbox(limit=2))  # newest two only
    counts = server.channel_status()["counts"]
    assert counts["unopened"] + counts["opened_unmarked"] == counts["unread"] == 4
    assert counts["opened_unmarked"] == 2


def test_marking_read_without_reading_does_not_break_the_split(db_path: Path, as_role):
    as_role("frontend")
    mid = server.send_message(to="backend", topic="t", body="b")["id"]
    as_role("backend")
    server.mark_read(message_id=mid)  # never called read_inbox
    counts = server.channel_status()["counts"]
    assert (counts["unread"], counts["unopened"], counts["opened_unmarked"]) == (0, 0, 0)


def test_reading_does_not_decrement_unread(db_path: Path, as_role):
    """The old contract still holds: only mark_read clears a message."""
    as_role("frontend")
    server.send_message(to="backend", topic="t", body="b")
    as_role("backend")
    L(server.read_inbox())
    L(server.read_inbox())
    assert server.channel_status()["counts"]["unread"] == 1
