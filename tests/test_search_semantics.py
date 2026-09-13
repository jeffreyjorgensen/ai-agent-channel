"""The scan that answers short-term queries means what the index means."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from ai_agent_channel import db, server
from helpers import L


def _insert(conn: sqlite3.Connection, topic: str, body: str, **kw: Any) -> int:
    kw.setdefault("from_role", "frontend")
    kw.setdefault("to_role", "backend")
    return db.insert_message(
        conn, topic=topic, body=body, action_required=False, reply_to=None, **kw
    )["id"]


@pytest.fixture
def conn(db_path: Path):
    with db.open_db() as c:
        yield c


def _ids(rows: list[dict[str, Any]]) -> set[int]:
    return {r["id"] for r in rows}


def test_not_excludes_in_the_scan(conn):
    with_id = _insert(conn, "t", "merchant id here")
    without = _insert(conn, "t", "merchant xyz")
    hits = db.search_messages(conn, query="merchant NOT id")
    assert _ids(hits) == {without}
    assert all(h["match"] == "substring" for h in hits)
    assert with_id not in _ids(hits)


def test_or_widens_in_the_scan(conn):
    a = _insert(conn, "t", "merchant id here")
    b = _insert(conn, "t", "xyz only")
    _insert(conn, "t", "nothing")
    assert _ids(db.search_messages(conn, query="xyz OR id")) == {a, b}


def test_a_trailing_star_is_not_a_literal_character(conn):
    a = _insert(conn, "t", "merchant id")
    assert _ids(db.search_messages(conn, query="merch* id")) == {a}


def test_column_filter_is_honoured(conn):
    in_topic = _insert(conn, "id card", "x")
    _insert(conn, "x", "id card")
    assert _ids(db.search_messages(conn, query="topic:id")) == {in_topic}


def test_unsupported_syntax_with_a_short_term_is_refused(conn):
    _insert(conn, "t", "merchant id")
    with pytest.raises(ValueError, match="shorter than 3"):
        db.search_messages(conn, query="merchant + id")


def test_an_unparseable_query_is_searched_literally_by_both_paths(conn):
    a = _insert(conn, "t", "use C++ or go")
    assert _ids(db.search_messages(conn, query="C++ go")) == {a}
    assert _ids(db.search_messages(conn, query="C++ goes")) == set()


def test_a_doubled_quote_inside_a_phrase_is_a_literal_quote_in_the_scan(conn):
    # 'a"' is shorter than a trigram, so the scan answers
    quoted = _insert(conn, "t", 'the value a" ends here')
    _insert(conn, "t", "the value a ends here")
    assert _ids(db.search_messages(conn, query='"a"""')) == {quoted}


def test_an_unterminated_phrase_is_searched_literally_in_the_scan(conn):
    opened = _insert(conn, "t", 'say "a to open')
    _insert(conn, "t", "say a to open")
    assert _ids(db.search_messages(conn, query='"a')) == {opened}


VOCAB = ["alpha", "bravo", "charlie", "delta", "Echo", "foxtrot"]


def _random_query(rng: random.Random, depth: int = 0) -> str:
    roll = rng.random()
    if depth > 2 or roll < 0.35:
        word = rng.choice(VOCAB)
        if rng.random() < 0.2:
            return f'"{word} {rng.choice(VOCAB)}"'
        return word + ("*" if rng.random() < 0.1 else "")
    left = _random_query(rng, depth + 1)
    right = _random_query(rng, depth + 1)
    op = rng.choice([" AND ", " OR ", " NOT ", " "])
    if rng.random() < 0.3:
        return f"({left}{op if op.strip() else ' AND '}{right})"
    return f"{left}{op}{right}"


def test_the_scan_agrees_with_the_index_on_random_queries(conn, monkeypatch):
    rng = random.Random(20260913)
    for _ in range(40):
        words = rng.sample(VOCAB, rng.randint(1, 4))
        topic = " ".join(words[:1])
        body = " ".join(w.upper() if rng.random() < 0.3 else w for w in words[1:])
        _insert(conn, topic, body or "-")
    checked = 0
    for _ in range(300):
        query = _random_query(rng)
        try:
            by_index = _ids(db.search_messages(conn, query=query, limit=1000))
        except ValueError:
            continue
        with monkeypatch.context() as m:
            # every term now counts as too short for the index
            m.setattr(db.search, "FTS_MIN_TERM", 100)
            by_scan = db.search_messages(conn, query=query, limit=1000)
        assert all(r["match"] == "substring" for r in by_scan)
        assert _ids(by_scan) == by_index, query
        checked += 1
    assert checked > 200


def test_to_role_finds_broadcasts_on_both_paths(db_path: Path, as_role):
    for role in ("backend", "infra"):
        as_role(role)
        server.send_message(to="frontend", topic="hello", body="present")
    as_role("frontend")
    sent = server.send_message(to="*", topic="announcement", body="hello world", kind="status")[
        "id"
    ]
    as_role("backend")
    assert sent in _ids(L(server.search_messages(query="hello world", to_role="backend")))
    assert sent in _ids(L(server.search_messages(query="hello wo", to_role="backend")))


class _LockedOnMatch:
    """A connection whose full-text queries fail the way a busy database does."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: Any) -> Any:
        if "MATCH" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)


def test_a_locked_database_is_not_reported_as_a_bad_query(conn):
    _insert(conn, "t", "merchant")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.search_messages(_LockedOnMatch(conn), query="merchant")  # type: ignore[arg-type]
