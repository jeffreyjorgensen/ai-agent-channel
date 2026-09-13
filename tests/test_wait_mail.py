"""wait_for_mail: an agent sleeping in the channel instead of ending its turn."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai_agent_channel import db, server
from ai_agent_channel.tools import waiting


async def test_a_standing_backlog_is_reported_but_does_not_wake_you(
    db_path: Path, as_role, fast_wait
):
    """The backlog you were already carrying is not an event.

    Waking on it made waiting useless exactly when it is needed: a role with
    one open round it is deliberately postponing was woken instantly, every
    call, and every round ended up moved by a human starting sessions by
    hand. It is still reported — hidden is not the fix — just not a reason.
    """
    as_role("frontend")
    server.send_message(to="backend", topic="hi", body="x")
    as_role("backend")
    result = await server.wait_for_mail(timeout_s=1, poll_interval_s=0.1)
    assert result["timed_out"] is True
    assert result["pending_at_entry"] == {"unread": 1}


async def test_the_old_behaviour_is_still_available(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="hi", body="x")
    as_role("backend")
    result = await server.wait_for_mail(timeout_s=30, ignore_backlog=False)
    assert result["pending"] == {"unread": 1}
    assert result["timed_out"] is False


async def test_times_out_on_a_quiet_channel(db_path: Path, as_role, fast_wait):
    as_role("backend")
    result = await server.wait_for_mail(timeout_s=1, poll_interval_s=0.1)
    assert result["timed_out"] is True
    assert result["retry"] is True
    assert result["waited_s"] == 1


async def test_wakes_up_when_mail_arrives_mid_wait(db_path: Path, as_role, monkeypatch, fast_wait):
    as_role("backend")

    async def sender():
        await fast_wait.until_polled()  # the waiter has taken its snapshot
        monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", "frontend")
        server.send_message(to="backend", topic="urgent", body="x", action_required=True)
        monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", "backend")

    waiter = asyncio.create_task(server.wait_for_mail(timeout_s=30, poll_interval_s=0.1))
    await sender()
    result = await asyncio.wait_for(waiter, timeout=5)
    assert result["timed_out"] is False
    assert result["pending"]["unread"] == 1
    assert result["pending"]["open_obligations_untaken"] == 1


async def test_wakes_on_exactly_what_the_stop_hook_blocks_on(db_path: Path, as_role):
    """The two must not drift: anything that keeps a session from stopping
    is something a sleeping session should be woken for."""
    as_role("frontend")
    proposal = server.send_message(
        to="backend", topic="proc", body="x", kind="proc", pin_key=None, about_message_id=None
    )
    as_role("backend")
    server.mark_read(message_id=proposal["id"])
    result = await server.wait_for_mail(timeout_s=5, poll_interval_s=0.1, ignore_backlog=False)
    assert set(result["pending"]) <= set(db.ACTIONABLE_COUNTS)
    assert result["pending"] == {"awaiting_ack": 1}


async def test_validation(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="'timeout_s'"):
        await server.wait_for_mail(timeout_s=-1)
    with pytest.raises(ValueError, match="'poll_interval_s'"):
        await server.wait_for_mail(poll_interval_s=0.01)


async def test_per_call_wait_is_capped(db_path: Path, as_role, monkeypatch, fast_wait):
    """A long wait must be a LOOP of short calls — one call may never outlive
    the MCP client's tool timeout."""
    as_role("backend")
    monkeypatch.setattr(waiting, "WAIT_CAP_S", 1)
    result = await server.wait_for_mail(timeout_s=3600, poll_interval_s=0.1)
    assert result["waited_s"] == 1


def test_cap_stays_below_client_tool_timeouts():
    assert waiting.WAIT_CAP_S <= 50
