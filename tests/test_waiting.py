"""Waiting tools: they end on time, refuse what cannot arrive, and notice
new items even when a counter nets out."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from ai_agent_channel import db, server


async def test_wait_for_reply_does_not_sleep_past_its_deadline(db_path: Path, as_role, fast_wait):
    as_role("frontend")
    parent = server.send_message(to="backend", topic="q", body="?")["id"]
    started = time.monotonic()
    result = await server.wait_for_reply(message_id=parent, timeout_s=1, poll_interval_s=10)
    assert result["timed_out"] is True
    # the deadline is a tenth of one poll interval: overshooting it by a
    # whole interval would take ten times longer than this allows
    assert time.monotonic() - started < 10 * fast_wait.scale / 2


async def test_wait_for_mail_does_not_sleep_past_its_deadline(db_path: Path, as_role, fast_wait):
    as_role("backend")
    started = time.monotonic()
    result = await server.wait_for_mail(timeout_s=1, poll_interval_s=10)
    assert result["timed_out"] is True
    assert time.monotonic() - started < 10 * fast_wait.scale / 2


async def test_wait_for_reply_refuses_a_message_that_does_not_exist(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="message 999999 not found"):
        await server.wait_for_reply(message_id=999999, timeout_s=0)


async def test_new_mail_wakes_a_waiter_even_when_unread_nets_out(db_path: Path, as_role, fast_wait):
    as_role("frontend")
    old = server.send_message(to="backend", topic="old", body="x")["id"]
    as_role("backend")
    waiter = asyncio.create_task(server.wait_for_mail(timeout_s=30, poll_interval_s=0.5))
    await fast_wait.until_polled(1)  # the waiter has recorded what it carries
    # another session of backend reads the old one while a new one arrives,
    # both between two polls: 'unread' is 1 before and 1 after
    with db.open_db() as conn:
        db.mark_read(conn, message_id=old, role="backend")
        db.insert_message(
            conn,
            from_role="frontend",
            to_role="backend",
            topic="new",
            body="y",
            action_required=False,
            reply_to=None,
        )
    result = await asyncio.wait_for(waiter, timeout=5)
    assert result["timed_out"] is False
    assert result["pending"] == {"unread": 1}
    assert result["pending_at_entry"] == {"unread": 1}


async def test_an_item_that_leaves_and_returns_wakes_the_waiter(db_path: Path, as_role, fast_wait):
    as_role("frontend")
    debt = server.send_message(to="backend", topic="fix", body="x", action_required=True)["id"]
    as_role("backend")
    server.mark_read(message_id=debt)
    waiter = asyncio.create_task(server.wait_for_mail(timeout_s=30, poll_interval_s=0.2))
    await fast_wait.until_polled(1)
    with db.open_db() as conn:  # backend takes it ...
        db.set_work_status(
            conn,
            message_id=debt,
            role="backend",
            work_status="in_progress",
            note=None,
            blocked_by=None,
        )
    await fast_wait.until_polled()  # ... the waiter sees it gone ...
    with db.open_db() as conn:  # ... and frontend throws the ball back
        db.set_work_status(
            conn,
            message_id=debt,
            role="frontend",
            work_status="needs_you",
            note=None,
            blocked_by=None,
        )
    result = await asyncio.wait_for(waiter, timeout=5)
    assert result["timed_out"] is False
    assert "open_obligations_untaken" in result["pending"]


async def test_waiting_does_not_block_the_event_loop(db_path: Path, as_role, monkeypatch):
    as_role("backend")
    real = db.channel_summary

    def slow(conn, *, role):
        time.sleep(0.1)
        return real(conn, role=role)

    monkeypatch.setattr(db, "channel_summary", slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    # two blocking reads of 0.1s each (the entry snapshot and one poll): a
    # loop blocked by them would tick a handful of times at most
    task = asyncio.create_task(ticker())
    await server.wait_for_mail(timeout_s=0, poll_interval_s=0.1)
    task.cancel()
    assert ticks >= 10
