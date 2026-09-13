"""The channel database's shape as SQL text: tables, the FTS index, the
column additions of the first schema generation, and the channel_meta side
table. Versioning and the steps that apply this text live in migrations.py."""

from __future__ import annotations

import sqlite3

# to_role of a message addressed to every other role in the channel; the
# actual recipients live in message_recipients.
BROADCAST = "*"

# The channel's timestamp, as SQL. Every stored time has this exact shape, so
# string comparison between stamps orders them correctly.
NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

# Schema version 1. Frozen: CREATE TABLE IF NOT EXISTS never reaches an
# existing table, so a new column goes into a new numbered step in
# migrations.py, not here.
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    topic TEXT NOT NULL,
    body TEXT NOT NULL,
    action_required INTEGER NOT NULL DEFAULT 0,
    reply_to INTEGER NULL REFERENCES messages(id),
    created_at TEXT NOT NULL DEFAULT ({NOW_SQL}),
    read_at TEXT NULL,
    status TEXT NULL,
    resolved_by TEXT NULL,
    resolved_at TEXT NULL,
    resolution_note TEXT NULL,
    kind TEXT NULL,
    work_status TEXT NULL,
    blocked_by INTEGER NULL,
    deleted_at TEXT NULL,
    -- The pin key a proposal changes.
    pin_key TEXT NULL,
    -- The message this one is about (a nudge points at its proposal).
    about_message_id INTEGER NULL,
    -- JSON array of the roles whose vote the round needs; NULL = every
    -- addressee. Does not change who receives the message.
    voters TEXT NULL,
    -- Set when a causal event retires the message; it stays readable.
    superseded_at TEXT NULL,
    -- Set when read_inbox returns the row; read_at is set by mark_read.
    -- For a broadcast both live per recipient in message_recipients.
    opened_at TEXT NULL,
    -- Broadcast only: JSON {{role: personal tail}} appended to the shared body.
    addenda TEXT NULL,
    -- 0 = a proposal opened for reading, not for a decision.
    decision_requested INTEGER NOT NULL DEFAULT 1,
    -- Set by revise_message; votes older than this stamp no longer count.
    revised_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_to_role_unread ON messages(to_role, read_at);
CREATE INDEX IF NOT EXISTS idx_topic ON messages(topic);
CREATE INDEX IF NOT EXISTS idx_reply_to ON messages(reply_to);

CREATE TABLE IF NOT EXISTS pinned_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    version TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ({NOW_SQL}),
    approved_by INTEGER NULL
);
CREATE INDEX IF NOT EXISTS idx_pins_key ON pinned_entries(key, id);

CREATE TABLE IF NOT EXISTS acknowledgements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    role TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT NULL,
    created_at TEXT NOT NULL DEFAULT ({NOW_SQL}),
    UNIQUE(message_id, role)
);
CREATE INDEX IF NOT EXISTS idx_ack_message ON acknowledgements(message_id);

CREATE TABLE IF NOT EXISTS message_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    event TEXT NOT NULL,
    role TEXT NOT NULL,
    note TEXT NULL,
    created_at TEXT NOT NULL DEFAULT ({NOW_SQL})
);
CREATE INDEX IF NOT EXISTS idx_events_message ON message_events(message_id);

-- One row per recipient of a broadcast (messages.to_role = '*'). A
-- point-to-point message keeps its recipient on the message row. A broadcast
-- is one row with one body, so every recipient votes on the same text.
CREATE TABLE IF NOT EXISTS message_recipients (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    to_role TEXT NOT NULL,
    read_at TEXT NULL,
    opened_at TEXT NULL,
    PRIMARY KEY (message_id, to_role)
);
CREATE INDEX IF NOT EXISTS idx_recipients_role ON message_recipients(to_role);

-- "Message m was delivered to role r", over both storage shapes.
CREATE VIEW IF NOT EXISTS deliveries AS
    SELECT id AS message_id, to_role, read_at, opened_at
    FROM messages WHERE to_role <> '*'
    UNION ALL
    SELECT message_id, to_role, read_at, opened_at FROM message_recipients;

-- Key/value bookkeeping that is not domain data (e.g. the FTS generation).
CREATE TABLE IF NOT EXISTS channel_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A document uploaded in pieces, sealed and hashed once, then referenced by
-- send_message / revise_message / pin_set.
CREATE TABLE IF NOT EXISTS content_blobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    label TEXT NULL,
    body TEXT NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NULL,
    length_bytes INTEGER NULL,
    length_chars INTEGER NULL,
    created_at TEXT NOT NULL DEFAULT ({NOW_SQL}),
    sealed_at TEXT NULL
);
"""

# The full-text index over messages. Separate from SCHEMA because FTS5 is a
# compile-time option: without it search_messages degrades to a scan.
# External content (`content='messages'`): the triggers keep it in sync.
#
# Tokenizer: trigram, so MATCH is an indexed case-insensitive substring search
# (inflected forms and markers such as 'ORDER-2291/A' match). Terms shorter
# than 3 characters cannot be looked up; search_messages scans for those.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    topic, body, content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, topic, body)
    VALUES (new.id, new.topic, new.body);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, topic, body)
    VALUES ('delete', old.id, old.topic, old.body);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au
AFTER UPDATE OF topic, body ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, topic, body)
    VALUES ('delete', old.id, old.topic, old.body);
    INSERT INTO messages_fts(rowid, topic, body)
    VALUES (new.id, new.topic, new.body);
END;
"""

# The index generation recorded in channel_meta. Bumping it (in a new
# migration step) drops, recreates and rebuilds the index. 2: trigram.
FTS_VERSION = "2"
FTS_TOKENIZER = "trigram"
FTS_OBJECTS = (
    "DROP TRIGGER IF EXISTS messages_fts_ai",
    "DROP TRIGGER IF EXISTS messages_fts_ad",
    "DROP TRIGGER IF EXISTS messages_fts_au",
    "DROP TABLE IF EXISTS messages_fts",
)

# Part of schema version 1: columns and backfills for databases created
# before the column existed. On a fresh database each ALTER fails with
# "duplicate column name", which is the expected no-op.
MIGRATIONS = [
    "ALTER TABLE messages ADD COLUMN status TEXT NULL",
    "ALTER TABLE messages ADD COLUMN resolved_by TEXT NULL",
    "ALTER TABLE messages ADD COLUMN resolved_at TEXT NULL",
    "ALTER TABLE messages ADD COLUMN resolution_note TEXT NULL",
    "ALTER TABLE messages ADD COLUMN kind TEXT NULL",
    "ALTER TABLE messages ADD COLUMN work_status TEXT NULL",
    "ALTER TABLE messages ADD COLUMN blocked_by INTEGER NULL",
    "ALTER TABLE messages ADD COLUMN deleted_at TEXT NULL",
    "ALTER TABLE pinned_entries ADD COLUMN approved_by INTEGER NULL",
    "ALTER TABLE messages ADD COLUMN pin_key TEXT NULL",
    "ALTER TABLE messages ADD COLUMN about_message_id INTEGER NULL",
    "ALTER TABLE messages ADD COLUMN superseded_at TEXT NULL",
    "ALTER TABLE messages ADD COLUMN opened_at TEXT NULL",
    "ALTER TABLE messages ADD COLUMN voters TEXT NULL",
    # Open obligations written before 'status' existed.
    "UPDATE messages SET status = 'open' WHERE action_required = 1 AND status IS NULL",
    # A message marked read was opened.
    "UPDATE messages SET opened_at = read_at WHERE read_at IS NOT NULL AND opened_at IS NULL",
    # Indexes over added columns: after the ALTERs, so not in SCHEMA.
    "CREATE INDEX IF NOT EXISTS idx_pin_key ON messages(pin_key)",
    "CREATE INDEX IF NOT EXISTS idx_about ON messages(about_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_superseded ON messages(superseded_at)",
    "ALTER TABLE pinned_entries ADD COLUMN body_sha256 TEXT NULL",
    "ALTER TABLE pinned_entries ADD COLUMN body_length_bytes INTEGER NULL",
    "ALTER TABLE pinned_entries ADD COLUMN body_length_chars INTEGER NULL",
    "ALTER TABLE messages ADD COLUMN addenda TEXT NULL",
    "ALTER TABLE messages ADD COLUMN decision_requested INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE messages ADD COLUMN revised_at TEXT NULL",
]


def meta_get(conn: sqlite3.Connection, *, key: str) -> str | None:
    row = conn.execute("SELECT value FROM channel_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, *, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO channel_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def now(conn: sqlite3.Connection) -> str:
    """The server's clock, in the channel's own timestamp format."""
    return conn.execute(f"SELECT {NOW_SQL} AS t").fetchone()["t"]


def statements(script: str) -> list[str]:
    """Split a SQL script into statements (trigger bodies stay whole).

    executescript() COMMITs any open transaction first, so it cannot be used
    inside the transaction that applies a migration."""
    out: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in buf.splitlines()
    ):
        out.append(buf.strip())
    return out
