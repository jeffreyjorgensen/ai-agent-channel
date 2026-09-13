"""A pin round declares WHO must consent to it.

The rule it replaces was "every role, always". That is right at two roles and
unusable at five: a member who joined for one subsystem, or who works in
another channel most of the week, holds a veto over every contract they were
never party to — and their silence is indistinguishable from a refusal. So the
electorate stops being implied and becomes something the round SAYS, checked
by pin_set against the statement the round made while everyone could see it.

Two properties keep this from being a way to decide things quietly, and both
are tested here: the letter still goes to every role, and a role outside the
electorate can still vote — it just cannot block.
"""

from __future__ import annotations

import pytest

from ai_agent_channel import db, server
from helpers import L, present

ROLES = ("alpha", "beta", "gamma", "delta")


@pytest.fixture
def member(channel_identity):
    """A registered channel of four roles — the electorate rule needs a roster."""
    return channel_identity(ROLES)


def _round(key: str = "team-charter", *, voters, frm: str = "alpha") -> int:
    """A pin proposal, addressed to everyone as the rules require."""
    return server.send_message(
        to=[r for r in ROLES if r != frm],
        topic=f"proposal for {key}",
        body=f"text {key}",
        kind="proc",
        pin_key=key,
        about_message_id=None,
        voters=voters,
    )["id"]


def _agree(member, role: str, mid: int) -> None:
    member(role)
    server.acknowledge(message_id=mid, decision="agree")


# --- the channel asks -------------------------------------------------------


def test_a_pin_round_without_an_electorate_is_refused(member):
    """The asking itself. An omitted 'voters' is not '*': it is no answer."""
    member("alpha")
    with pytest.raises(ValueError) as err:
        server.send_message(
            to=["beta", "gamma", "delta"],
            topic="proposal",
            body="text",
            kind="proc",
            pin_key="team-charter",
            about_message_id=None,
        )
    assert "voters" in str(err.value)


def test_the_refusal_names_both_ways_out(member):
    """A wall that does not name its door is a wall the agent works around."""
    member("alpha")
    with pytest.raises(ValueError) as err:
        _round(voters=server.OMITTED_VOTERS)
    text = str(err.value)
    assert "'*'" in text
    assert sorted(r for r in ROLES if r != "alpha")[0] in text


def test_a_proposal_without_a_pin_key_takes_no_electorate(member):
    """Who decides an ordinary message is who it was sent to — there is
    nothing for a declaration to mean."""
    member("alpha")
    with pytest.raises(ValueError) as err:
        server.send_message(
            to="beta",
            topic="question",
            body="text",
            kind="proc",
            pin_key=None,
            about_message_id=None,
            voters=["beta"],
        )
    assert "pin_key" in str(err.value)


# --- what a declaration may say ---------------------------------------------


def test_an_unknown_role_cannot_be_given_a_vote(member):
    member("alpha")
    with pytest.raises(ValueError) as err:
        _round(voters=["beta", "epsilon"])
    assert "epsilon" in str(err.value)


def test_an_electorate_of_nobody_is_refused(member):
    """Naming only yourself is a pin written on your own consent."""
    member("alpha")
    with pytest.raises(ValueError) as err:
        _round(voters=["alpha"])
    assert "empty" in str(err.value)


def test_listing_yourself_is_not_a_vote_you_owe(member):
    """'*' means "everyone else"; an explicit list containing the author must
    mean the same thing, or the two spellings of one intent disagree."""
    member("alpha")
    mid = _round(voters=["alpha", "beta"])
    with db.open_db() as conn:
        assert present(db.fetch_message(conn, mid))["voters"] == ["beta"]


def test_narrowing_the_electorate_does_not_narrow_the_audience(member):
    """The load-bearing half: everyone still READS the round."""
    member("alpha")
    with pytest.raises(ValueError) as err:
        server.send_message(
            to=["beta"],
            topic="proposal",
            body="text",
            kind="proc",
            pin_key="team-charter",
            about_message_id=None,
            voters=["beta"],
        )
    assert "gamma" in str(err.value) and "delta" in str(err.value)


# --- what the declaration then does -----------------------------------------


def test_star_is_exactly_the_old_all_roles_rule(member):
    member("alpha")
    mid = _round(voters="*")
    _agree(member, "beta", mid)
    _agree(member, "gamma", mid)

    member("alpha")
    with pytest.raises(ValueError) as err:
        server.pin_set(
            key="team-charter", title="Charter", version="1", body="body", approved_by=mid
        )
    assert "delta" in str(err.value)

    _agree(member, "delta", mid)
    member("alpha")
    assert (
        server.pin_set(
            key="team-charter", title="Charter", version="1", body="body", approved_by=mid
        )["key"]
        == "team-charter"
    )


def test_a_declared_electorate_closes_without_the_roles_it_left_out(member):
    """The whole point: beta agrees, the pin is written, and the two roles
    that were never party to it never had to answer."""
    member("alpha")
    mid = _round(voters=["beta"])
    _agree(member, "beta", mid)

    member("alpha")
    written = server.pin_set(
        key="team-charter", title="Charter", version="1", body="body", approved_by=mid
    )
    assert written["key"] == "team-charter"


def test_the_declared_electorate_is_still_a_quorum_of_its_own(member):
    """Narrowed is not waived: the roles that WERE named must all answer."""
    member("alpha")
    mid = _round(voters=["beta", "gamma"])
    _agree(member, "beta", mid)

    member("alpha")
    with pytest.raises(ValueError) as err:
        server.pin_set(
            key="team-charter", title="Charter", version="1", body="body", approved_by=mid
        )
    assert "gamma" in str(err.value)
    assert "delta" not in str(err.value)


def test_the_tally_counts_the_electorate_not_the_readers(member):
    member("alpha")
    mid = _round(voters=["beta"])
    member("beta")
    tally = server.get_acknowledgements(message_id=mid)
    assert tally["needed"] == 1
    assert tally["voters"] == ["beta"]


def test_a_role_outside_the_electorate_may_still_vote(member):
    """It is recorded, it is visible, and it does not move the count — the
    difference between "not asked" and "not allowed"."""
    member("alpha")
    mid = _round(voters=["beta"])
    _agree(member, "delta", mid)

    member("alpha")
    tally = server.get_acknowledgements(message_id=mid)
    assert tally["agreed"] == 0
    assert tally["missing"] == ["beta"]
    # named as a recipient's vote, not as an outsider's: delta WAS written to
    assert tally["from_non_voters"] == {"delta": "agree"}
    assert "from_non_recipients" not in tally


def test_a_vote_from_outside_the_electorate_cannot_approve_the_pin(member):
    member("alpha")
    mid = _round(voters=["beta"])
    _agree(member, "delta", mid)

    member("alpha")
    with pytest.raises(ValueError) as err:
        server.pin_set(
            key="team-charter", title="Charter", version="1", body="body", approved_by=mid
        )
    assert "beta" in str(err.value)


# --- the debt side ----------------------------------------------------------


def test_a_role_outside_the_electorate_owes_no_decision(member):
    """The bug class this avoids: a role billed for a vote it cannot be
    required to cast, on every stop, forever."""
    member("alpha")
    mid = _round(voters=["beta"])

    member("delta")
    assert [m["id"] for m in L(server.awaiting_ack())] == []
    assert server.channel_status()["counts"]["awaiting_ack"] == 0

    member("beta")
    assert [m["id"] for m in L(server.awaiting_ack())] == [mid]
    assert server.channel_status()["counts"]["awaiting_ack"] == 1


def test_an_undeclared_round_still_owes_everyone(member):
    """History keeps its rule. Messages written before the field exists have
    voters=NULL, and NULL must keep meaning "all of them"."""
    with db.open_db() as conn:
        mid = db.insert_message(
            conn,
            from_role="alpha",
            to_role=db.BROADCAST,
            topic="old",
            body="text",
            action_required=False,
            reply_to=None,
            kind="proc",
            pin_key="team-charter",
            recipients=["beta", "gamma", "delta"],
        )["id"]

    member("delta")
    assert [m["id"] for m in L(server.awaiting_ack())] == [mid]

    _agree(member, "beta", mid)
    member("alpha")
    with pytest.raises(ValueError) as err:
        server.pin_set(
            key="team-charter", title="Charter", version="1", body="body", approved_by=mid
        )
    assert "ALL roles" in str(err.value)
    assert "delta" in str(err.value) and "gamma" in str(err.value)


def test_the_electorate_is_projectable_like_any_other_field(member):
    """It is a fact about the round, so a caller asking for exactly that
    field must be able to get it — projections only ever narrow."""
    member("alpha")
    mid = _round(voters=["beta"])
    member("beta")
    rows = L(server.list_messages(fields=["id", "voters"]))
    assert {"id": mid, "voters": ["beta"]} in rows
