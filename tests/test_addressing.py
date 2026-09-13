"""Who a message is addressed to, applied the same way everywhere.

That a wide reply keeps to its parent's audience is part of the acceptance
contract (T-14, tests/acceptance/test_requirements_part1.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import db, server
from helpers import L

ROLES = ("backend", "infra", "content")


@pytest.fixture
def roster(db_path: Path, as_role):
    """stdio infers the roster from traffic, so every role says hello."""
    for role in ROLES:
        as_role(role)
        server.send_message(to="frontend", topic="hello", body="present")
    as_role("frontend")


@pytest.mark.parametrize("to", [["*"], ["backend", "*"]])
def test_star_inside_a_list_is_refused(roster, to):
    with pytest.raises(ValueError, match="cannot appear inside a list"):
        server.send_message(to=to, topic="t", body="x", action_required=True, kind="bug")


def test_a_broadcast_recipient_may_use_a_proposal_as_approval_in_stdio(roster, as_role):
    pid = server.send_message(
        to="*",
        topic="notes change",
        body="new notes",
        kind="proc",
        pin_key="notes",
        about_message_id=None,
        voters="*",
    )["id"]
    for role in ROLES:
        as_role(role)
        server.acknowledge(message_id=pid, decision="agree")
    as_role("backend")
    assert (
        server.pin_set(
            key="notes",
            title="n",
            version="1",
            body="new notes",
            approved_by=pid,
            dry_run=True,
        )["ok"]
        is True
    )


def test_a_proposal_linked_to_one_pin_does_not_approve_another(roster, as_role):
    pid = server.send_message(
        to="backend",
        topic="glossary tweak",
        body="unlike team-charter, this...",
        kind="proc",
        pin_key="glossary",
        about_message_id=None,
    )["id"]
    as_role("backend")
    server.acknowledge(message_id=pid, decision="agree")
    as_role("frontend")
    preview = server.pin_set(
        key="team-charter",
        title="c",
        version="1",
        body="z",
        approved_by=pid,
        dry_run=True,
    )
    assert preview["ok"] is False
    assert "'glossary'" in preview["problem"]


def test_acking_retires_a_broadcast_nudge(roster, as_role):
    pid = server.send_message(
        to=["backend", "infra"],
        topic="proposal",
        body="x",
        kind="proc",
        pin_key=None,
        about_message_id=None,
    )["id"]
    nudge = server.send_message(
        to=["backend", "infra"],
        topic="vote please",
        body="x",
        kind="status",
        about_message_id=pid,
    )["id"]
    as_role("backend")
    result = server.acknowledge(message_id=pid, decision="agree")
    assert result["superseded"] == [nudge]


def test_board_thread_shows_a_broadcast_with_addenda(roster):
    pid = server.send_message(
        to="*",
        topic="proposal",
        body="shared",
        kind="proc",
        pin_key=None,
        about_message_id=None,
        addenda={"backend": "for you"},
    )["id"]
    with db.open_db() as conn:
        thread = db.board_thread(conn, message_id=pid)
    assert thread[0]["addenda"] == {"backend": "for you"}
    assert thread[0]["to"] == sorted(ROLES)


def test_since_is_compared_as_an_instant(db_path: Path, as_role):
    as_role("frontend")
    before = server.send_message(to="backend", topic="before", body="x")["id"]
    after = server.send_message(to="backend", topic="after", body="x")["id"]
    with db.open_db() as conn:
        conn.execute(
            "UPDATE messages SET created_at = '2026-08-09T06:59:59.999Z' WHERE id = ?",
            (before,),
        )
        conn.execute(
            "UPDATE messages SET created_at = '2026-08-09T07:00:00.000Z' WHERE id = ?",
            (after,),
        )
    for since in (
        "2026-08-09 10:00:00+03:00",
        "2026-08-09T07:00:00Z",
        "2026-08-09T07:00:00",
        "2026-08-09T07:00:00.000+00:00",
    ):
        assert [m["id"] for m in L(server.list_messages(since=since))] == [after], since


def test_an_unparseable_since_is_refused(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="'since' must be an ISO-8601"):
        server.list_messages(since="yesterday")
