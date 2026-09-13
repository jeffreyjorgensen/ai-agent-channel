"""One message, many recipients — for the cases where "everyone" really is
the addressee, and never for debts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import db, server
from helpers import L

ROLES = ["backend", "infra", "content"]


@pytest.fixture
def channel(db_path: Path, as_role):
    """stdio mode infers the roster from traffic, so introduce everyone."""
    for role in ROLES:
        as_role(role)
        server.send_message(to="frontend", topic="hello", body="I am here")
    as_role("frontend")
    for _role in ROLES:
        server.mark_read(message_ids=[])  # no-op keeps the fixture explicit
    return ROLES


def _broadcast(to: str | list[str] = "*", **kw):
    # kind='proc' now demands an explicit answer for both, so the helper
    # states the default answer and each test overrides what it is about.
    kw.setdefault("pin_key", None)
    kw.setdefault("about_message_id", None)
    return server.send_message(to=to, topic="proposal", body="shared text", kind="proc", **kw)


# --- one text for everyone --------------------------------------------------


def test_one_message_one_body_many_recipients(channel, as_role):
    as_role("frontend")
    sent = _broadcast()
    assert sorted(sent["recipients"]) == sorted(ROLES)

    bodies = set()
    for role in ROLES:
        as_role(role)
        inbox = L(server.read_inbox())
        assert [m["id"] for m in inbox] == [sent["id"]]
        bodies.add(inbox[0]["body"])
        assert sorted(inbox[0]["to"]) == sorted(ROLES)
    # the whole point: not "four copies that happen to match" but one text
    assert len(bodies) == 1


def test_explicit_list_of_recipients(channel, as_role):
    as_role("frontend")
    sent = _broadcast(to=["backend", "infra"])
    assert sorted(sent["recipients"]) == ["backend", "infra"]
    as_role("content")
    assert L(server.read_inbox()) == []


def test_one_recipient_stays_a_plain_message(channel, as_role):
    as_role("frontend")
    sent = server.send_message(to=["backend"], topic="t", body="b")
    assert "recipients" not in sent
    as_role("backend")
    assert L(server.read_inbox())[0]["to"] == "backend"  # a string, not a list


# --- a debt stays point-to-point --------------------------------------------


def test_a_debt_cannot_be_broadcast(channel, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="action_required cannot be broadcast"):
        server.send_message(
            to="*",
            topic="please do it",
            body="x",
            action_required=True,
            kind="proc",
            pin_key=None,
            about_message_id=None,
        )
    with pytest.raises(ValueError, match="action_required cannot be broadcast"):
        server.send_message(
            to=["backend", "infra"],
            topic="please do it",
            body="x",
            action_required=True,
            kind="status",
        )


def test_only_proc_and_status_may_broadcast(channel, as_role):
    as_role("frontend")
    for kind in ("bug", "feat", "question", "answer", None):
        with pytest.raises(ValueError, match="may be sent to several roles"):
            server.send_message(to="*", topic="t", body="b", kind=kind)
    assert _broadcast()["recipients"]
    assert server.send_message(to="*", topic="t", body="b", kind="status")["recipients"]


# --- votes and reads are per addressee --------------------------------------


def test_votes_land_on_one_id_and_the_tally_sees_everyone(channel, as_role):
    as_role("frontend")
    sent = _broadcast(pin_key="team-charter")
    for role in ("backend", "infra"):
        as_role(role)
        server.acknowledge(message_id=sent["id"], decision="agree")

    state = server.get_acknowledgements(message_id=sent["id"])
    assert state["agreed"] == 2
    assert state["missing"] == ["content"]
    # …and that single id is what pin_set approves against
    as_role("content")
    server.acknowledge(message_id=sent["id"], decision="agree")
    as_role("frontend")
    assert (
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="shared text",
            version="v1",
            approved_by=sent["id"],
        )["version"]
        == "v1"
    )


def test_read_state_is_per_recipient(channel, as_role):
    as_role("frontend")
    sent = _broadcast()
    as_role("backend")
    server.mark_read(message_id=sent["id"])
    assert server.channel_status()["counts"]["unread"] == 0
    as_role("infra")
    assert server.channel_status()["counts"]["unread"] == 1
    assert [m["id"] for m in L(server.read_inbox())] == [sent["id"]]


def test_opened_is_per_recipient_too(channel, as_role):
    as_role("frontend")
    _broadcast()
    as_role("backend")
    L(server.read_inbox())
    assert server.channel_status()["counts"]["opened_unmarked"] == 1
    as_role("infra")
    assert server.channel_status()["counts"]["unopened"] == 1


def test_a_non_recipient_cannot_mark_it_read(channel, as_role):
    as_role("frontend")
    sent = _broadcast(to=["backend"])
    as_role("infra")
    with pytest.raises(PermissionError, match="addressed to"):
        server.mark_read(message_id=sent["id"])


# --- addenda ----------------------------------------------------------------


def test_addenda_keep_the_shared_body_identical(channel, as_role):
    as_role("frontend")
    sent = _broadcast(
        addenda={"backend": "for you — about the migration", "infra": "for you — about DNS"}
    )
    seen = {}
    for role in ROLES:
        as_role(role)
        msg = L(server.read_inbox())[0]
        seen[role] = (msg["body"], msg.get("addendum"))
    assert {b for b, _ in seen.values()} == {"shared text"}
    assert seen["backend"][1] == "for you — about the migration"
    assert seen["infra"][1] == "for you — about DNS"
    assert seen["content"][1] is None
    assert sent["id"]


def test_addenda_must_name_actual_recipients(channel, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match=r"\['designer'\]"):
        _broadcast(addenda={"designer": "x"})


# --- everything else keeps working ------------------------------------------


def test_broadcast_appears_in_awaiting_ack_and_threads(channel, as_role):
    as_role("frontend")
    sent = _broadcast()
    as_role("infra")
    assert [m["id"] for m in L(server.awaiting_ack())] == [sent["id"]]
    reply = server.send_message(
        to="frontend", topic="re", body="question", reply_to=sent["id"], kind="question"
    )
    thread = L(server.get_thread(message_id=sent["id"]))
    assert [m["id"] for m in thread] == [sent["id"], reply["id"]]
    assert sorted(thread[0]["to"]) == sorted(ROLES)


def test_broadcast_is_searchable_and_listable_per_recipient(channel, as_role):
    as_role("frontend")
    sent = _broadcast()
    assert [m["id"] for m in L(server.list_messages(to_role="content"))] == [sent["id"]]
    assert [m["id"] for m in L(server.search_messages(query="shared text"))] == [sent["id"]]


def test_cannot_broadcast_to_a_role_outside_the_channel(channel, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="cannot send to self"):
        server.send_message(
            to=["backend", "frontend"],
            topic="t",
            body="b",
            kind="proc",
            pin_key=None,
            about_message_id=None,
        )


def test_deliveries_view_covers_both_shapes(channel, as_role):
    as_role("frontend")
    direct = server.send_message(to="backend", topic="t", body="b")["id"]
    wide = _broadcast()["id"]
    with db.open_db() as conn:
        rows = conn.execute(
            "SELECT message_id, to_role FROM deliveries WHERE message_id IN (?, ?)",
            (direct, wide),
        ).fetchall()
    pairs = {(r["message_id"], r["to_role"]) for r in rows}
    assert (direct, "backend") in pairs
    assert {(wide, r) for r in ROLES} <= pairs
