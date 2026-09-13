"""Opening a database upgrades it in place, once, or fails loudly."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from ai_agent_channel import db
from helpers import present

# The first shape of the mailbox: the columns every later migration adds are
# missing, and so are the tables introduced since.
OLD_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    topic TEXT NOT NULL,
    body TEXT NOT NULL,
    action_required INTEGER NOT NULL DEFAULT 0,
    reply_to INTEGER NULL REFERENCES messages(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    read_at TEXT NULL
);
CREATE TABLE pinned_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    version TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT INTO messages (from_role, to_role, topic, body, action_required, read_at)
VALUES ('frontend', 'backend', 'old debt', 'x', 1, '2026-01-01T00:00:00.000Z');
INSERT INTO pinned_entries (key, title, body, version, updated_by)
VALUES ('notes', 'Notes', 'body', 'v1', 'frontend');
"""


def _shape(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(str(path))
    try:
        objects = conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        tables = {
            name: {
                col[1]: (col[2], col[3], col[4], col[5])
                for col in conn.execute(f"PRAGMA table_info('{name}')")
            }
            for kind, name in objects
            if kind in ("table", "view")
        }
        return {
            "objects": objects,
            "tables": tables,
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        }
    finally:
        conn.close()


def _split(script: str) -> list[str]:
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    return out


def _old_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.close()


def test_an_old_database_upgrades_to_exactly_the_fresh_schema(tmp_path: Path):
    fresh, old = tmp_path / "fresh.db", tmp_path / "old.db"
    db.init_db(fresh)
    _old_db(old)
    with db.open_db(old) as conn:
        row = conn.execute("SELECT status, opened_at FROM messages").fetchone()
        # data kept and backfilled
        assert row["status"] == "open"
        assert row["opened_at"] == "2026-01-01T00:00:00.000Z"
        assert (
            present(db.pin_get(conn, key="notes"))["body_sha256"]
            == db.body_digest("body")["body_sha256"]
        )
    assert _shape(old) == _shape(fresh)
    assert _shape(old)["user_version"] == db.SCHEMA_VERSION


def test_a_locked_database_is_not_opened_half_migrated(tmp_path: Path, monkeypatch):
    path = tmp_path / "m.db"
    # the database of the previous release: every table and index, one
    # column short, no schema marker
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.executescript(OLD_SCHEMA.split("INSERT", 1)[0])
    for stmt in [*_split(db.SCHEMA), *db.MIGRATIONS, *_split(db.FTS_SCHEMA)]:
        if "revised_at" in stmt and stmt.startswith("ALTER"):
            continue
        try:
            raw.execute(stmt)
        except sqlite3.OperationalError as exc:
            assert "duplicate column name" in str(exc)
    raw.execute(
        "INSERT INTO channel_meta (key, value) VALUES ('fts_version', ?)",
        (db.FTS_VERSION,),
    )
    raw.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    monkeypatch.setattr(db.connection, "BUSY_TIMEOUT_MS", 100)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"), db.open_db(path):
            pass
    finally:
        raw.execute("ROLLBACK")
        raw.close()
    with db.open_db(path) as conn:
        columns = {c[1] for c in conn.execute("PRAGMA table_info('messages')")}
        assert "revised_at" in columns


def test_opening_a_current_database_takes_no_write_lock(tmp_path: Path, monkeypatch):
    path = tmp_path / "m.db"
    db.init_db(path)
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("BEGIN IMMEDIATE")
    raw.execute(
        "INSERT INTO messages (from_role, to_role, topic, body) VALUES ('a', 'b', 't', 'x')"
    )
    monkeypatch.setattr(db.connection, "BUSY_TIMEOUT_MS", 3000)
    started = time.monotonic()
    try:
        with db.open_db(path) as conn:
            db.channel_summary(conn, role="b")
    finally:
        raw.execute("ROLLBACK")
        raw.close()
    assert time.monotonic() - started < 1.0


def test_a_migration_error_other_than_an_existing_column_propagates(tmp_path: Path, monkeypatch):
    path = tmp_path / "m.db"
    monkeypatch.setattr(
        db.schema, "MIGRATIONS", [*db.MIGRATIONS, "ALTER TABLE nowhere ADD COLUMN x"]
    )
    with pytest.raises(sqlite3.OperationalError, match="no such table"), db.open_db(path):
        pass


@pytest.mark.parametrize("recorded_version", ["1", db.FTS_VERSION])
def test_an_index_with_another_tokenizer_is_rebuilt(tmp_path: Path, recorded_version):
    """Recorded as an old generation, or recorded as current by a release
    that kept the old tokenizer — either way the index is replaced."""
    path = tmp_path / "m.db"
    with db.open_db(path) as conn:
        db.insert_message(
            conn,
            from_role="a",
            to_role="b",
            topic="key rotated",
            body="rotation schedule",
            action_required=False,
            reply_to=None,
        )
        for stmt in db.FTS_OBJECTS:
            conn.execute(stmt)
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "topic, body, content='messages', content_rowid='id')"
        )
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
        db.meta_set(conn, key="fts_version", value=recorded_version)
        conn.execute("PRAGMA user_version = 0")
        assert db.search_messages(conn, query="rotat") == []  # word tokenizer

    with db.open_db(path) as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'messages_fts'").fetchone()[
            "sql"
        ]
        assert "trigram" in sql
        hits = db.search_messages(conn, query="rotat")
        assert [h["topic"] for h in hits] == ["key rotated"]
        assert hits[0]["match"] == "fts"
        # the triggers came back with the table
        db.insert_message(
            conn,
            from_role="a",
            to_role="b",
            topic="later",
            body="rotating",
            action_required=False,
            reply_to=None,
        )
        assert len(db.search_messages(conn, query="rotat")) == 2


def test_init_db_closes_its_connection(tmp_path: Path, monkeypatch):
    opened: list[sqlite3.Connection] = []
    real = db.connect

    def tracking(path: Path) -> sqlite3.Connection:
        conn = real(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db.connection, "connect", tracking)
    db.init_db(tmp_path / "m.db")
    assert opened
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
