"""Schema versions are hand-numbered steps: a database is upgraded once, a
database from a newer build is never touched, and databases stamped by the
releases that hashed the schema text upgrade in place."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from ai_agent_channel import db
from ai_agent_channel.db import connection, migrations

REAL_STEPS = migrations.STEPS
NEXT = REAL_STEPS[-1].version + 1


def _future_step(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_future ON messages(kind)")


FUTURE = migrations.Step(NEXT, "future index", _future_step)


def _user_version(path: Path) -> int:
    raw = sqlite3.connect(str(path))
    try:
        return raw.execute("PRAGMA user_version").fetchone()[0]
    finally:
        raw.close()


def _columns(path: Path) -> dict[str, list[str]]:
    raw = sqlite3.connect(str(path))
    try:
        tables = [
            r[0]
            for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'messages_fts%' ORDER BY name"
            )
        ]
        return {t: [c[1] for c in raw.execute(f"PRAGMA table_info('{t}')")] for t in tables}
    finally:
        raw.close()


def _build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra: tuple[migrations.Step, ...] = (),
    delay: float = 0.0,
) -> list[int]:
    """Install a build with these steps; returns the versions of the steps
    it runs, in order."""
    calls: list[int] = []

    def counted(step: migrations.Step) -> migrations.Step:
        def apply(conn: sqlite3.Connection) -> None:
            calls.append(step.version)
            if delay:
                time.sleep(delay)
            step.apply(conn)

        return migrations.Step(step.version, step.name, apply)

    steps = tuple(counted(s) for s in (*REAL_STEPS, *extra))
    monkeypatch.setattr(migrations, "STEPS", steps)
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", steps[-1].version)
    return calls


def test_steps_are_numbered_one_by_one_and_define_the_version():
    assert [s.version for s in REAL_STEPS] == list(range(1, len(REAL_STEPS) + 1))
    assert db.SCHEMA_VERSION == REAL_STEPS[-1].version < migrations.LEGACY_HASH_FLOOR


def test_a_fresh_database_runs_every_step_once(tmp_path: Path, monkeypatch):
    path = tmp_path / "m.db"
    calls = _build(monkeypatch)
    db.init_db(path)
    db.init_db(path)
    assert calls == [s.version for s in REAL_STEPS]
    assert _user_version(path) == db.SCHEMA_VERSION


def test_builds_of_different_versions_do_not_migrate_each_others_database(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "m.db"
    new = _build(monkeypatch, extra=(FUTURE,))
    db.init_db(path)
    assert new == list(range(1, NEXT + 1))
    for _ in range(3):
        old = _build(monkeypatch)
        with db.open_db(path) as conn:
            db.channel_summary(conn, role="b")
        assert old == []
        assert _user_version(path) == NEXT
        new = _build(monkeypatch, extra=(FUTURE,))
        db.init_db(path)
        assert new == []


def test_a_database_from_a_newer_build_is_opened_without_migrating_or_locking(
    tmp_path: Path, monkeypatch, caplog
):
    path = tmp_path / "m.db"
    db.init_db(path)
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute(f"PRAGMA user_version = {NEXT + 5}")
    raw.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    calls = _build(monkeypatch)
    monkeypatch.setattr(connection, "BUSY_TIMEOUT_MS", 100)
    try:
        with db.open_db(path) as conn:
            assert db.channel_summary(conn, role="b")["unread"] == 0
    finally:
        raw.execute("ROLLBACK")
        raw.close()
    assert calls == []
    assert _user_version(path) == NEXT + 5
    assert "newer than this build" in caplog.text


def test_opening_a_current_database_runs_no_step_and_takes_no_lock(tmp_path: Path, monkeypatch):
    path = tmp_path / "m.db"
    db.init_db(path)
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("BEGIN IMMEDIATE")
    calls = _build(monkeypatch)
    monkeypatch.setattr(connection, "BUSY_TIMEOUT_MS", 100)
    try:
        with db.open_db(path) as conn:
            db.channel_summary(conn, role="b")
    finally:
        raw.execute("ROLLBACK")
        raw.close()
    assert calls == []


def test_concurrent_first_opens_migrate_once(tmp_path: Path, monkeypatch):
    path = tmp_path / "m.db"
    # An existing but unmigrated file (user_version 0) already in WAL mode:
    # two connections switching a zero-byte file to WAL at once can fail with
    # "database is locked" before any migration starts.
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("CREATE TABLE probe (x)")
    raw.execute("DROP TABLE probe")
    raw.close()
    calls = _build(monkeypatch, delay=0.2)
    barrier = threading.Barrier(2)
    real_connect = connection.connect

    def synced(p: Path) -> sqlite3.Connection:
        conn = real_connect(p)
        barrier.wait(timeout=5)  # both openers read the version before either migrates
        return conn

    monkeypatch.setattr(connection, "connect", synced)
    errors: list[BaseException] = []

    def opener() -> None:
        try:
            with db.open_db(path) as conn:
                conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert calls == [s.version for s in REAL_STEPS]
    assert _user_version(path) == db.SCHEMA_VERSION


@pytest.mark.parametrize(
    ("stamp", "applied"),
    [
        # the last release that hashed the schema text, on the shape it created
        (migrations.BASELINE_HASH_VERSION, 1),
        # an older hash stamp
        (1_234_567_890, 1),
        # a hash release reopened a database that already had later steps
        (migrations.BASELINE_HASH_VERSION, len(REAL_STEPS)),
    ],
)
def test_a_hash_stamped_database_upgrades_in_place(
    tmp_path: Path, monkeypatch, stamp: int, applied: int
):
    path, fresh = tmp_path / "m.db", tmp_path / "fresh.db"
    conn = db.connect(path)
    try:
        with db.transaction(conn):
            for step in REAL_STEPS[:applied]:
                step.apply(conn)
            conn.execute(f"PRAGMA user_version = {stamp}")
        db.insert_message(
            conn,
            from_role="a",
            to_role="b",
            topic="key rotation",
            body="schedule",
            action_required=True,
            reply_to=None,
        )
    finally:
        conn.close()

    statements: list[str] = []
    real_connect = connection.connect

    def traced(p: Path) -> sqlite3.Connection:
        c = real_connect(p)
        c.set_trace_callback(statements.append)
        return c

    monkeypatch.setattr(connection, "connect", traced)
    with db.open_db(path) as conn:
        hits = db.search_messages(conn, query="rotation")
        assert [(h["topic"], h["match"]) for h in hits] == [("key rotation", "fts")]
        assert db.channel_summary(conn, role="b")["counts"]["open_obligations"] == 1
    assert _user_version(path) == db.SCHEMA_VERSION
    # the index was current: upgrading did not rebuild it
    assert not any("'rebuild'" in s for s in statements)
    db.init_db(fresh)
    assert _columns(path) == _columns(fresh)
    assert "pin_keys" in _columns(path)["message_events"]
