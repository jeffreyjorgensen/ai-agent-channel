"""Full-text search over the channel history (FTS5, with a LIKE fallback)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai_agent_channel import db, server
from helpers import L


@pytest.fixture
def corpus(db_path: Path, as_role):
    as_role("frontend")
    ids = {
        "merchant": server.send_message(
            to="backend",
            topic="merchant_id in the checkout payload",
            body="the field is missing for guest orders",
            kind="bug",
        )["id"],
        "auth": server.send_message(
            to="backend",
            topic="auth tokens",
            body="rotate the merchant secret every 90 days",
            kind="proc",
            pin_key=None,
            about_message_id=None,
        )["id"],
        "unrelated": server.send_message(
            to="backend", topic="lunch", body="pizza at one", kind="status"
        )["id"],
    }
    as_role("backend")
    ids["reply"] = server.send_message(
        to="frontend",
        topic="re: merchant_id",
        body="fixed, guest orders now carry it",
        kind="answer",
        reply_to=ids["merchant"],
    )["id"]
    as_role("frontend")
    return ids


def test_search_finds_by_body_and_topic(corpus):
    hits = L(server.search_messages(query="guest orders"))
    assert {m["id"] for m in hits} == {corpus["merchant"], corpus["reply"]}
    assert all(m["match"] == "fts" for m in hits)
    assert any("guest" in m["snippet"] for m in hits)


def test_search_ands_bare_terms(corpus):
    # both words must appear somewhere in the message
    assert [m["id"] for m in L(server.search_messages(query="merchant secret"))] == [corpus["auth"]]
    assert L(server.search_messages(query="pizza tokens")) == []


def test_search_is_substring_not_word_based(db_path: Path, as_role):
    """The reason for the trigram tokenizer: a word-boundary index would
    make 'rotat' find nothing, because 'rotate', 'rotation' and 'rotated'
    are unrelated tokens to it."""
    as_role("frontend")
    for topic in ("plan to rotate the keys", "rotation done", "keys rotated on Friday"):
        server.send_message(to="backend", topic=topic, body="details")
    server.send_message(to="backend", topic="lunch", body="pizza")

    assert len(L(server.search_messages(query="rotat"))) == 3
    assert len(L(server.search_messages(query="rotation"))) == 1
    assert all(m["match"] == "fts" for m in L(server.search_messages(query="rotat")))


def test_short_terms_fall_back_to_a_scan(corpus):
    """Below the trigram window the index cannot answer at all — the tool
    still must, so it scans and says so."""
    hits = L(server.search_messages(query="90"))
    assert [m["id"] for m in hits] == [corpus["auth"]]
    assert hits[0]["match"] == "substring"


def test_search_supports_or_phrase_and_prefix(corpus):
    assert {m["id"] for m in L(server.search_messages(query="pizza OR tokens"))} == {
        corpus["unrelated"],
        corpus["auth"],
    }
    assert [m["id"] for m in L(server.search_messages(query='"pizza at one"'))] == [
        corpus["unrelated"]
    ]
    assert {m["id"] for m in L(server.search_messages(query="merch*"))} >= {
        corpus["merchant"],
        corpus["auth"],
    }


def test_search_punctuation_falls_back_to_quoted_terms(corpus):
    # a bare ':' / '-' is FTS syntax, not a word — the retry quotes the terms
    # instead of blowing up in the agent's face
    assert [m["id"] for m in L(server.search_messages(query="re: merchant_id"))] == [
        corpus["reply"]
    ]


def test_search_filters(corpus):
    assert [m["id"] for m in L(server.search_messages(query="merchant", kind="proc"))] == [
        corpus["auth"]
    ]
    assert [m["id"] for m in L(server.search_messages(query="merchant", from_role="backend"))] == [
        corpus["reply"]
    ]
    assert L(server.search_messages(query="merchant", to_role="nobody")) == []


def test_search_excludes_deleted(corpus):
    server.mark_read(message_id=corpus["reply"])
    server.delete_message(corpus["reply"])
    assert [m["id"] for m in L(server.search_messages(query="guest orders"))] == [
        corpus["merchant"]
    ]


def test_search_requires_query_and_bounds_limit(corpus):
    with pytest.raises(ValueError, match="'query' is required"):
        L(server.search_messages(query="   "))
    with pytest.raises(ValueError, match="'limit'"):
        L(server.search_messages(query="merchant", limit=0))


def test_index_backfills_a_pre_fts_database(tmp_path: Path, monkeypatch, as_role):
    """A DB written before the FTS index existed gets it on the next open —
    the rebuild is what makes old history searchable at all."""
    path = tmp_path / "old.db"
    monkeypatch.setenv("AI_AGENT_CHANNEL_DB", str(path))
    with db.open_db() as conn:
        db.insert_message(
            conn,
            from_role="a",
            to_role="b",
            topic="legacy topic",
            body="written before the index",
            action_required=False,
            reply_to=None,
        )
        # simulate the pre-FTS state: index and its marker gone
        conn.execute("DROP TABLE messages_fts")
        conn.execute("DELETE FROM channel_meta WHERE key = 'fts_version'")

    as_role("b")
    hits = L(server.search_messages(query="legacy"))
    assert [m["topic"] for m in hits] == ["legacy topic"]


def test_falls_back_to_substring_without_fts5(corpus, monkeypatch):
    monkeypatch.setattr(db.search, "fts_available", lambda conn: False)
    hits = L(server.search_messages(query="merchant"))
    assert {m["id"] for m in hits} >= {corpus["merchant"], corpus["auth"]}
    assert all(m["match"] == "substring" for m in hits)


def test_fts5_trigram_is_available_in_this_build():
    """FTS5 is optional at the SQLite level and search_messages degrades to a
    LIKE scan without it (hits tagged match: "substring"). The degraded path
    has tests of its own; this one proves the normal path is the one the rest
    of the suite exercised, on every interpreter the suite runs on."""
    with sqlite3.connect(":memory:") as conn:
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
        except sqlite3.OperationalError as exc:
            pytest.fail(f"sqlite {sqlite3.sqlite_version}: fts5 trigram not available: {exc}")
