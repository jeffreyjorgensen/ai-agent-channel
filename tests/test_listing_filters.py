"""list_messages filters: topic/text are literal substrings, and `since`
rejects instants it cannot represent with a clear error."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ai_agent_channel import db


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with db.open_db(tmp_path / "m.db") as c:
        yield c


def _insert(conn: sqlite3.Connection, topic: str, body: str) -> int:
    return db.insert_message(
        conn,
        from_role="a",
        to_role="b",
        topic=topic,
        body=body,
        action_required=False,
        reply_to=None,
    )["id"]


def _list(conn: sqlite3.Connection, **kw: Any) -> list[int]:
    args: dict[str, Any] = {
        "topic": None,
        "from_role": None,
        "to_role": None,
        "unread_only": False,
        "since": None,
        "limit": 100,
    }
    args.update(kw)
    return [m["id"] for m in db.list_messages(conn, **args)]


def test_wildcards_in_topic_and_text_filters_are_literal(conn):
    percent = _insert(conn, "100% done", "x")
    _insert(conn, "1000 rows", "x")
    underscore = _insert(conn, "t", "merchant_id")
    _insert(conn, "t", "merchant-id")
    backslash = _insert(conn, "t", r"C:\temp")
    assert _list(conn, topic="100%") == [percent]
    assert _list(conn, text="merchant_id") == [underscore]
    assert _list(conn, text="_") == [underscore]
    assert _list(conn, text="\\") == [backslash]
    # still case-insensitive
    assert _list(conn, topic="100% DONE") == [percent]


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_an_instant_outside_the_representable_range_is_a_clear_error(conn, value):
    with pytest.raises(ValueError, match="'since' must be an ISO-8601 timestamp"):
        db.normalize_timestamp(value, field="since")
    with pytest.raises(ValueError, match="'since'"):
        _list(conn, since=value)


def test_an_offset_near_the_end_of_the_range_still_normalizes():
    assert (
        db.normalize_timestamp("9999-12-31T22:59:59-01:00", field="since")
        == "9999-12-31T23:59:59.000Z"
    )
