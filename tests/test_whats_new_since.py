"""A new build tells a role what changed since the build it last saw, not the
whole history again."""

from __future__ import annotations

from pathlib import Path

from ai_agent_channel import db, release, server
from ai_agent_channel.tools.meta import news_since


def test_news_since_filters_by_the_seen_builds_date():
    assert news_since(None) == [dict(i) for i in release.WHATS_NEW]
    assert news_since("not a build") == [dict(i) for i in release.WHATS_NEW]
    recent = news_since("2026-09-13.1")
    assert recent and all(i["since"] >= "2026-09-13" for i in recent)
    assert len(recent) < len(release.WHATS_NEW)
    assert news_since("2999-01-01.1") == []


def test_channel_status_sends_only_entries_newer_than_the_last_seen_build(db_path: Path, as_role):
    as_role("frontend")
    with db.open_db() as conn:
        db.meta_set(conn, key="seen_build:frontend", value="2026-09-13.1")
    news = server.channel_status()["server"]["whats_new"]
    assert news == news_since("2026-09-13.1")
    assert all(i["since"] != "2026-08-09" for i in news)
    assert "whats_new" not in server.channel_status()["server"]


def test_a_build_with_nothing_new_records_it_silently(db_path: Path, as_role):
    as_role("frontend")
    with db.open_db() as conn:
        db.meta_set(conn, key="seen_build:frontend", value="2999-01-01.1")
    assert "whats_new" not in server.channel_status()["server"]
    with db.open_db() as conn:
        assert db.meta_get(conn, key="seen_build:frontend") == release.BUILD
