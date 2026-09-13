"""Multi-role channels: addressing, third-party acks, pin consensus.

Identity is injected straight into auth.CURRENT_IDENTITY (what the HTTP
middleware does), so these run as fast units without uvicorn. The stdio
behaviour (no identity → classic two-party rules) is covered by the
pre-existing test_pins/test_tools suites.
"""

from __future__ import annotations

import pytest

from ai_agent_channel import server
from helpers import L

ROLES = ("alpha", "beta", "gamma")


@pytest.fixture
def as_member(channel_identity):
    return channel_identity(ROLES)


def test_addressing_any_member_but_only_members(as_member):
    as_member("alpha")
    server.send_message(to="beta", topic="t", body="to beta")
    server.send_message(to="gamma", topic="t", body="to gamma")
    with pytest.raises(ValueError, match="cannot address 'omega'"):
        server.send_message(to="omega", topic="t", body="outsider")
    with pytest.raises(ValueError, match="cannot send to self"):
        server.send_message(to="alpha", topic="t", body="self")


def test_message_lifecycle_stays_two_party(as_member):
    as_member("alpha")
    sent = server.send_message(to="beta", topic="task", body="do it", action_required=True)
    # a third channel member is not a party to this debt
    as_member("gamma")
    with pytest.raises(PermissionError):
        server.set_work_status(message_id=sent["id"], work_status="in_progress")
    # ...but sees it (channel transparency) and may acknowledge it
    assert L(server.list_messages())[0]["id"] == sent["id"]
    server.acknowledge(message_id=sent["id"], decision="agree")
    # the debt itself surfaces only to the addressee
    as_member("beta")
    assert [m["id"] for m in L(server.open_obligations())] == [sent["id"]]
    assert L(server.open_obligations(to_role="gamma")) == []


def _propose_and_agree(proposer: str, agreeing: list[str], as_member) -> int:
    as_member(proposer)
    to = next(r for r in ROLES if r != proposer)
    msg = server.send_message(
        to=to,
        topic="proc: team-charter v-next",
        body="adopt the charter",
        kind="proc",
        pin_key=None,
        about_message_id=None,
    )
    for role in agreeing:
        as_member(role)
        server.acknowledge(message_id=msg["id"], decision="agree")
    return msg["id"]


def test_protected_pin_needs_consent_of_all_roles(as_member):
    # only beta agreed — gamma's consent is missing
    proposal = _propose_and_agree("alpha", ["beta"], as_member)
    as_member("alpha")
    with pytest.raises(ValueError, match=r"lacks 'agree' from \['gamma'\]"):
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="v1",
            version="v1",
            approved_by=proposal,
        )
    # gamma joins in — now the channel-wide contract may change
    as_member("gamma")
    server.acknowledge(message_id=proposal, decision="agree")
    as_member("alpha")
    pinned = server.pin_set(
        key="team-charter",
        title="Charter",
        body="v1",
        version="v1",
        approved_by=proposal,
    )
    assert pinned["version"] == "v1"


def test_any_consenting_member_may_apply_the_pin(as_member):
    # gamma pins a proposal that went alpha → beta: fine, everyone consented
    proposal = _propose_and_agree("alpha", ["beta", "gamma"], as_member)
    as_member("gamma")
    pinned = server.pin_set(
        key="team-charter",
        title="Charter",
        body="v1",
        version="v1",
        approved_by=proposal,
    )
    assert pinned["updated_by"] == "gamma"


def test_stale_consent_is_named_per_role(as_member):
    early = _propose_and_agree("alpha", ["beta", "gamma"], as_member)  # agreed first
    v1 = _propose_and_agree("alpha", ["beta", "gamma"], as_member)
    as_member("alpha")
    server.pin_set(key="team-charter", title="Charter", body="v1", version="v1", approved_by=v1)
    # the earlier proposal's agrees predate the pinned version — stale
    with pytest.raises(ValueError, match="consent is stale"):
        server.pin_set(
            key="team-charter",
            title="Charter",
            body="v2",
            version="v2",
            approved_by=early,
        )
