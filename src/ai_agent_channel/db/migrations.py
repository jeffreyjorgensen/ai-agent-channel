"""Schema versioning: numbered, ordered upgrade steps and the rule for which
of them a database still needs.

PRAGMA user_version holds the number of the last step applied. A database
is read as follows:

* 0 (created before the marker existed) or a value >= LEGACY_HASH_FLOOR
  (a crc32 stamp written by releases that derived the version from the
  schema text): schema version 0 — every step runs. Step 1 is idempotent,
  so this is safe for any shape those releases produced.
* 1 .. SCHEMA_VERSION - 1: the steps after it run.
* SCHEMA_VERSION: nothing to do.
* SCHEMA_VERSION + 1 .. LEGACY_HASH_FLOOR - 1: written by a newer build.
  Opened as is, never migrated and never re-stamped.

Every step must be idempotent (a column that exists is skipped, backfills
only touch rows that still need them): a release that stamps a crc32 value
may reopen a database that already has later steps applied.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import NamedTuple

from . import schema
from .blobs import body_digest
from .schema import FTS_OBJECTS, FTS_SCHEMA, FTS_TOKENIZER, FTS_VERSION, SCHEMA, meta_get, meta_set

# Values at or above this are crc32-derived stamps from earlier releases;
# hand-numbered versions stay far below it.
LEGACY_HASH_FLOOR = 10_000

# The crc32 stamp written by the last release that used them.
BASELINE_HASH_VERSION = 184234251


class Step(NamedTuple):
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def add_columns(conn: sqlite3.Connection, stmts: list[str]) -> None:
    """Run statements, skipping an ALTER whose column already exists. Any
    other error (a lock, a full disk, a typo) propagates."""
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise


def apply_migrations(conn: sqlite3.Connection) -> None:
    """The column additions and backfills of schema version 1."""
    add_columns(conn, schema.MIGRATIONS)


def ensure_fts(conn: sqlite3.Connection) -> bool:
    """Create the FTS index and (re)build it when its generation is stale.

    Returns False when this SQLite build has no FTS5; search then scans. An
    index from another generation or with another tokenizer is dropped and
    recreated, since CREATE ... IF NOT EXISTS would keep the old one.
    """
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
    ).fetchone()
    current = (
        existing is not None
        and meta_get(conn, key="fts_version") == FTS_VERSION
        and f"'{FTS_TOKENIZER}'" in (existing["sql"] or "")
    )
    if existing is not None and not current:
        for stmt in FTS_OBJECTS:
            conn.execute(stmt)
    try:
        for stmt in schema.statements(FTS_SCHEMA):
            conn.execute(stmt)
    except sqlite3.OperationalError as exc:
        if "no such module" in str(exc):
            return False
        raise
    if not current:
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
        meta_set(conn, key="fts_version", value=FTS_VERSION)
    return True


def ensure_pin_digests(conn: sqlite3.Connection) -> None:
    """Fill digests of pin versions written before the digest columns.
    SQLite has no sha256(), so this runs in Python."""
    rows = conn.execute("SELECT id, body FROM pinned_entries WHERE body_sha256 IS NULL").fetchall()
    for row in rows:
        d = body_digest(row["body"])
        conn.execute(
            "UPDATE pinned_entries SET body_sha256 = ?, body_length_bytes = ?, "
            "body_length_chars = ? WHERE id = ?",
            (d["body_sha256"], d["body_length_bytes"], d["body_length_chars"], row["id"]),
        )


def _step_base_schema(conn: sqlite3.Connection) -> None:
    for stmt in schema.statements(SCHEMA):
        conn.execute(stmt)
    apply_migrations(conn)
    ensure_fts(conn)
    ensure_pin_digests(conn)


def _step_event_pin_keys(conn: sqlite3.Connection) -> None:
    # JSON array of the pin keys a cleanup pass ran under, so undo reads
    # them from a field instead of parsing the free-text note.
    add_columns(conn, ["ALTER TABLE message_events ADD COLUMN pin_keys TEXT NULL"])


# Append only. Never renumber or edit a released step; add a new one.
STEPS: tuple[Step, ...] = (
    Step(1, "base schema", _step_base_schema),
    Step(2, "message_events.pin_keys", _step_event_pin_keys),
)

SCHEMA_VERSION = STEPS[-1].version


def effective_version(stored: int) -> int:
    """The schema version a stored user_version stands for (see module doc)."""
    if stored <= 0 or stored >= LEGACY_HASH_FLOOR:
        return 0
    return stored


def is_newer(stored: int, current: int | None = None) -> bool:
    """True when the database was stamped by a build newer than this one."""
    current = SCHEMA_VERSION if current is None else current
    return current < stored < LEGACY_HASH_FLOOR


def pending_steps(stored: int, steps: tuple[Step, ...] | None = None) -> list[Step]:
    steps = STEPS if steps is None else steps
    base = effective_version(stored)
    return [s for s in steps if s.version > base]
