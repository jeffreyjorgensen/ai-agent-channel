"""Search queries are bounded: an oversized query is refused with a clear
error on every engine, and anything within the bounds is answered without
recursion limits and with the same precedence as FTS5."""

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


@pytest.fixture(params=["index", "scan", "no-fts"])
def engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "scan":
        monkeypatch.setattr(db.search, "FTS_MIN_TERM", 100)
    elif request.param == "no-fts":
        monkeypatch.setattr(db.search, "fts_available", lambda conn: False)
    return request.param


def _insert(conn: sqlite3.Connection, body: str) -> int:
    return db.insert_message(
        conn,
        from_role="a",
        to_role="b",
        topic="t",
        body=body,
        action_required=False,
        reply_to=None,
    )["id"]


def _ids(rows: list[dict[str, Any]]) -> set[int]:
    return {r["id"] for r in rows}


def test_a_long_run_of_adjacent_terms_is_refused_not_a_crash(conn, engine):
    _insert(conn, "abc")
    with pytest.raises(ValueError, match="at most 256 terms"):
        db.search_messages(conn, query=" ".join(["abc"] * 1000))


def test_deeply_nested_parentheses_are_refused_not_a_crash(conn, engine):
    _insert(conn, "abc")
    with pytest.raises(ValueError, match="nest parentheses at most 256"):
        db.search_messages(conn, query="(" * 1000 + "abc" + ")" * 1000)


def test_an_overlong_query_is_refused(conn, engine):
    with pytest.raises(ValueError, match="at most 4096 characters"):
        db.search_messages(conn, query="abcd " * 1000)


def test_an_unparseable_query_with_too_many_pieces_is_refused(conn, engine):
    with pytest.raises(ValueError, match="at most 256 terms"):
        db.search_messages(conn, query="C++ " + " ".join(["abc"] * 300))


def test_queries_at_the_limits_are_answered(conn, engine):
    hit = _insert(conn, "alpha bravo")
    _insert(conn, "charlie")
    many = " OR ".join([f"zz{i}q" for i in range(255)] + ["bravo"])
    assert _ids(db.search_messages(conn, query=many)) == {hit}
    chain = "alpha " + " ".join(f"NOT zz{i}q" for i in range(255))
    assert _ids(db.search_messages(conn, query=chain)) == {hit}


def test_deep_nesting_within_the_limit_is_evaluated_without_recursion(conn, monkeypatch):
    monkeypatch.setattr(db.search, "fts_available", lambda conn: False)
    hit = _insert(conn, "alpha bravo")
    _insert(conn, "charlie")
    deep = "(" * 250 + "alpha" + ")" * 250
    assert _ids(db.search_messages(conn, query=deep)) == {hit}
    nested_or = "(" * 120 + "zzq OR (" * 120 + "bravo" + ")" * 240
    assert _ids(db.search_messages(conn, query=nested_or)) == {hit}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("alpha OR charlie bravo", {"ab", "ad", "bc"}),  # adjacency binds tightest
        ("alpha NOT bravo AND delta", {"ad"}),  # NOT binds tighter than AND
        ("bravo OR alpha NOT delta", {"ab", "bc"}),
        ("(alpha OR charlie) NOT bravo", {"ad", "c"}),
        ("alpha NOT bravo NOT delta", set()),
        ("alpha AND bravo OR charlie AND NOT", None),  # unparseable: literal terms
        ("topic:alpha", set()),
    ],
)
def test_precedence_is_the_same_on_every_engine(conn, engine, query, expected):
    docs = {
        "ab": _insert(conn, "alpha bravo"),
        "c": _insert(conn, "charlie"),
        "ad": _insert(conn, "alpha delta"),
        "bc": _insert(conn, "bravo charlie"),
    }
    got = _ids(db.search_messages(conn, query=query, limit=100))
    if expected is None:
        assert got == set()
    else:
        assert got == {docs[k] for k in expected}
