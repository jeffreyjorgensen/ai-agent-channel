"""wait_for_mail computes the channel summary once per poll."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_agent_channel import db, server
from ai_agent_channel.db import summary as summary_module


async def test_one_summary_per_poll(db_path: Path, as_role, monkeypatch, fast_wait):
    as_role("backend")
    server.channel_status()  # creates the DB
    calls = 0
    real = summary_module.channel_summary

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(summary_module, "channel_summary", counted)
    monkeypatch.setattr(db, "channel_summary", counted)
    result = await server.wait_for_mail(timeout_s=1, poll_interval_s=0.1)
    assert result["timed_out"] is True
    # the entry snapshot plus one per poll, each computing the summary once
    assert calls == fast_wait.polls + 1


def test_actionable_ids_reuses_a_given_summary(db_path: Path, as_role, monkeypatch):
    as_role("frontend")
    server.send_message(to="backend", topic="t", body="b")
    with db.open_db() as conn:
        given = db.channel_summary(conn, role="backend")
        monkeypatch.setattr(
            summary_module, "channel_summary", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
        )
        ids = db.actionable_ids(conn, role="backend", summary=given)
    assert len(ids["unread"]) == 1
