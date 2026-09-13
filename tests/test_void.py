"""One rule for 'this round is dead': a fresh void from a voter or the author.

The same predicate decides whether the pin key is free for a new round and
whether the proposal can still approve pin_set, so the two cannot disagree.

The acceptance contract (tests/acceptance/test_requirements_part2.py) holds
the positive cases: T-21 a voided round cannot approve, T-28 a voter's void
frees the key. This file keeps the edges around them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import db, server

ROLES = ("alpha", "beta", "gamma")


@pytest.fixture
def member(channel_identity):
    return channel_identity(ROLES)


def _round(member, voters) -> int:
    member("alpha")
    return server.send_message(
        to="*",
        topic="glossary v2",
        body="text",
        kind="proc",
        pin_key="glossary",
        about_message_id=None,
        voters=voters,
    )["id"]


def _vote(member, role: str, pid: int, decision: str) -> None:
    member(role)
    server.acknowledge(message_id=pid, decision=decision)


def test_a_void_from_outside_the_electorate_closes_nothing(member):
    pid = _round(member, ["beta"])
    _vote(member, "gamma", pid, "void")
    with db.open_db() as conn:
        assert [r["id"] for r in db.open_rounds_for_pin(conn, key="glossary")] == [pid]
    member("beta")
    with pytest.raises(ValueError, match="already has an open round"):
        server.send_message(
            to="*",
            topic="competing",
            body="x",
            kind="proc",
            pin_key="glossary",
            about_message_id=None,
            voters="*",
        )
    _vote(member, "beta", pid, "agree")
    member("alpha")
    assert (
        server.pin_set(
            key="glossary",
            title="g",
            version="2",
            body="text",
            approved_by=pid,
            dry_run=True,
        )["ok"]
        is True
    )


def test_a_void_cast_before_a_revision_no_longer_closes_the_round(db_path: Path, as_role):
    for role in ("backend", "infra"):
        as_role(role)
        server.send_message(to="frontend", topic="hello", body="present")
    as_role("frontend")
    pid = server.send_message(
        to="*",
        topic="notes v1",
        body="draft 1",
        kind="proc",
        pin_key="notes",
        about_message_id=None,
    )["id"]
    as_role("backend")
    server.acknowledge(message_id=pid, decision="void")
    as_role("frontend")
    server.revise_message(message_id=pid, body="draft 2")
    with db.open_db() as conn:
        assert [r["id"] for r in db.open_rounds_for_pin(conn, key="notes")] == [pid]
    as_role("infra")
    server.acknowledge(message_id=pid, decision="agree")
    as_role("frontend")
    assert (
        server.pin_set(
            key="notes",
            title="n",
            version="1",
            body="draft 2",
            approved_by=pid,
            dry_run=True,
        )["ok"]
        is True
    )
