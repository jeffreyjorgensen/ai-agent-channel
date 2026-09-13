"""Opening a channel database: its path, connection settings, transactions,
and bringing the schema up to date on open."""

from __future__ import annotations

import functools
import itertools
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, TypeVar, cast

from . import migrations

DEFAULT_DB_DIR = Path.home() / ".ai-agent-channel"
DEFAULT_DB_FILENAME = "messages.db"

# How long a connection waits for another writer before "database is locked".
BUSY_TIMEOUT_MS = 5000

log = logging.getLogger(__name__)
_warned_newer: set[tuple[str, int]] = set()


def get_db_path() -> Path:
    override = os.environ.get("AI_AGENT_CHANNEL_DB")
    if override:
        return Path(override).expanduser()
    return DEFAULT_DB_DIR / DEFAULT_DB_FILENAME


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_MS)};")
        enable_wal(conn)
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except BaseException:
        conn.close()
        raise
    return conn


def enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the file to WAL (persistent, so usually a no-op read).

    The switch on a brand-new file needs an exclusive lock and SQLite does not
    run the busy handler for it, so concurrent first opens can fail at once
    with "database is locked". Retry until the busy timeout instead.
    """
    if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
        return
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    delay = 0.005
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


_savepoint_ids = itertools.count()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One atomic unit of work: BEGIN IMMEDIATE, COMMIT, or ROLLBACK on error.

    IMMEDIATE takes the write lock up front, so a check and the write that
    depends on it cannot interleave with another writer. Reentrant: inside
    an open transaction it becomes a SAVEPOINT, so nested helpers compose and
    a caught inner failure undoes only its own part. A COMMIT that fails is
    rolled back, so the connection is never left inside a transaction.
    """
    if conn.in_transaction:
        name = f"sp_{next(_savepoint_ids)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


_F = TypeVar("_F", bound=Callable[..., Any])


def atomic(fn: _F) -> _F:
    """Run a db function whose first argument is the connection in one
    transaction (a savepoint when the caller already holds one)."""

    @functools.wraps(fn)
    def wrapper(conn: sqlite3.Connection, *args: Any, **kwargs: Any) -> Any:
        with transaction(conn):
            return fn(conn, *args, **kwargs)

    return cast(_F, wrapper)


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _warn_newer(conn: sqlite3.Connection, stored: int) -> None:
    row = conn.execute("PRAGMA database_list").fetchone()
    marker = (row[2] if row else "", stored)
    if marker in _warned_newer:
        return
    _warned_newer.add(marker)
    log.warning(
        "database %s has schema version %d, newer than this build's %d; opened without migrating",
        marker[0],
        stored,
        migrations.SCHEMA_VERSION,
    )


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring the database to the current schema version.

    The version check is a header read that takes no lock. Only a database
    that is behind takes the write lock, and it re-checks under it so
    concurrent openers upgrade once. A database from a newer build is left
    untouched. Errors (including "database is locked") propagate: opening a
    half-migrated database would fail later in a less clear way.
    """
    current = migrations.SCHEMA_VERSION
    stored = user_version(conn)
    if stored == current:
        return
    if migrations.is_newer(stored, current):
        _warn_newer(conn, stored)
        return
    with transaction(conn):
        stored = user_version(conn)
        if stored == current or migrations.is_newer(stored, current):
            return
        for step in migrations.pending_steps(stored):
            step.apply(conn)
        conn.execute(f"PRAGMA user_version = {int(current)}")


def init_db(path: Path | None = None) -> None:
    path = path or get_db_path()
    with closing(connect(path)) as conn:
        ensure_schema(conn)


@contextmanager
def open_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = path or get_db_path()
    conn = connect(path)
    try:
        ensure_schema(conn)
        yield conn
    finally:
        conn.close()
