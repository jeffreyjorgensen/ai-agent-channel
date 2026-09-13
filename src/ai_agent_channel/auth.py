"""Channel/token registry and per-request identity for the HTTP transport.

In stdio mode identity comes from the ``AI_AGENT_CHANNEL_ROLE`` env var and
there is exactly one mailbox DB — this module is not involved. In HTTP mode
one server hosts many channels: each channel is its own SQLite mailbox file
under ``<data_dir>/channels/<name>.db`` (full isolation, db/ untouched),
and every request authenticates with a bearer token that maps to either

- the admin identity (token == ``AI_AGENT_CHANNEL_ADMIN_TOKEN`` env var):
  management tools only, no mailbox; or
- a role identity (a row in ``admin.db``): one fixed (channel, role) pair,
  mailbox tools only.

A channel has 2..MAX_ROLES roles (one token each). The registry's role list
is the channel's membership: ``send_message`` must target a member, and a
protected pin needs the consensus of the electorate its consent round
declared — every member when it declared none. The tools read the
membership through ``Identity.peers``.

The resolved identity travels to the tool call via ``CURRENT_IDENTITY``
(a contextvar set by the ASGI auth middleware). The HTTP transport must run
stateless (one request == one task tree) so the contextvar reaches the tool;
see http.py.

Like db/, the helpers here take a connection and return plain dicts; only
the path resolvers read env vars. Role tokens are stored hashed (sha256) —
plaintext is returned exactly once, at creation/rotation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db.connection import enable_wal

ADMIN_TOKEN_ENV = "AI_AGENT_CHANNEL_ADMIN_TOKEN"
DATA_DIR_ENV = "AI_AGENT_CHANNEL_DATA_DIR"
DEFAULT_DATA_DIR = Path.home() / ".ai-agent-channel"
TOKEN_PREFIX = "cct_"
# Different prefix so the two never get confused by eye in a config file.
VIEW_TOKEN_PREFIX = "ccv_"
# The security property is SINGLE USE, not brevity: once redeemed, the value
# left in browser history opens nothing. The TTL is defence in depth, so it
# only has to be short enough that an unclicked link does not linger — long
# enough that "generate, read the message, click" fits comfortably. Sixty
# seconds did not: the first real link expired before it was opened.
BOARD_NONCE_TTL_S = 300
# A board cookie is a session, not the key: what the browser holds is a
# random secret whose hash lives in board_sessions, so a copy of admin.db
# opens no board. It dies with its key and never outlives it.
BOARD_SESSION_TTL_S = 7 * 24 * 3600
# View keys expire server-side. Any role can mint one, so without a horizon
# a key handed out once stays a working view long after anyone remembers it.
VIEW_KEY_TTL_ENV = "AI_AGENT_CHANNEL_VIEW_KEY_TTL_DAYS"
VIEW_KEY_TTL_DAYS_DEFAULT = 30
# Accepted range. Outside it (or not a number at all: "nan", "inf", "1e309")
# the default applies with a warning — a typo in .env must neither crash the
# live server nor mint keys that effectively never expire.
VIEW_KEY_TTL_DAYS_MIN = 1
VIEW_KEY_TTL_DAYS_MAX = 3650
# Live keys per issuer (a role, or the admin) per channel; minting past the
# cap revokes the oldest, so a looping agent cannot pile up open views.
MAX_VIEW_KEYS_PER_ISSUER = 20
# Not a technical limit — a guard against the anti-pattern of using one
# charter-governed team channel as an event bus for dozens of agents.
MAX_ROLES = 12

# Channel names become file names; roles travel in URLs/snapshots — keep both
# to a safe slug alphabet.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

ADMIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    name TEXT PRIMARY KEY,
    roles TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL REFERENCES channels(name),
    role TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    revoked_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_channel ON tokens(channel, role);

-- One-time values exchanged for a board cookie. They exist so the durable
-- secret never travels in a URL: query strings land in browser history,
-- proxy logs and Referer headers, and a link forwarded once keeps working
-- forever. A nonce is burned on first use and expires after
-- BOARD_NONCE_TTL_S, so what is left in the history is a value that no
-- longer opens anything.
CREATE TABLE IF NOT EXISTS board_nonces (
    nonce_hash TEXT PRIMARY KEY,
    channel TEXT NULL,
    view_token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT NULL
);

-- What a redeemed nonce turns into. Only the hash of the cookie's secret is
-- stored; the session is valid only while its view key is.
CREATE TABLE IF NOT EXISTS board_sessions (
    session_hash TEXT PRIMARY KEY,
    view_token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_board_sessions_key ON board_sessions(view_token_hash);
"""

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

# Guarded like db.MIGRATIONS: each statement fails harmlessly once applied.
# Migrates pre-multi-role registries (fixed role_a/role_b pair) to the JSON
# `roles` list; `roles` stores a JSON array of 2..MAX_ROLES slugs.
ADMIN_MIGRATIONS = [
    # Access KIND, not just which role. A role token is a write key — it can
    # pin, send and resolve — and handing one to a read-only viewing surface
    # makes that surface as dangerous as the mailbox itself, however tidily
    # the token is transported. 'view' is a separate kind: issued
    # independently, never derived from a role token and not convertible
    # back into one.
    "ALTER TABLE tokens ADD COLUMN kind TEXT NOT NULL DEFAULT 'role'",
    "ALTER TABLE channels ADD COLUMN roles TEXT",
    "UPDATE channels SET roles = json_array(role_a, role_b) WHERE roles IS NULL",
    "ALTER TABLE channels DROP COLUMN role_a",
    "ALTER TABLE channels DROP COLUMN role_b",
    # View-key provenance and horizon. NULL issuer = issued by the admin (or
    # before provenance was recorded); rotating a role revokes what it issued.
    "ALTER TABLE tokens ADD COLUMN issued_by_role TEXT NULL",
    "ALTER TABLE tokens ADD COLUMN issued_by_token_hash TEXT NULL",
    "ALTER TABLE tokens ADD COLUMN expires_at TEXT NULL",
    # 1 = a view key minted before provenance was recorded. Its issuer may
    # have been any role of the channel, so rotating ANY role of that channel
    # revokes it (see rotate_token): losing a page view beats keeping a view
    # that a leaked token handed out.
    "ALTER TABLE tokens ADD COLUMN issuer_unknown INTEGER NOT NULL DEFAULT 0",
]

# PRAGMA user_version of admin.db once ADMIN_SCHEMA + ADMIN_MIGRATIONS and the
# one-time backfills below have run. Bump it whenever either list grows, so
# existing registries migrate once on their next open.
ADMIN_SCHEMA_VERSION = 1

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    """The authenticated caller of the current HTTP request.

    Three kinds, deliberately disjoint:

    * admin  — management tools, no mailbox, may view any board;
    * role   — one channel's mailbox, full write access, NO board (an agent
      reads the channel through MCP; the board is for the human);
    * viewer — one channel's board, read-only, and rejected everywhere else.
      `role` is None, so every mailbox tool refuses it on the same code path
      that refuses the admin token.
    """

    is_admin: bool
    channel: str | None = None
    role: str | None = None
    peers: tuple[str, ...] = ()  # the channel's other roles, in charter order
    db_path: Path | None = None
    is_viewer: bool = False
    # hash of the bearer that authenticated, so what it issues can be traced
    # back to it and revoked with it
    token_hash: str | None = None


# Set by the HTTP auth middleware for the duration of one request; None in
# stdio mode (tools/common.py then falls back to AI_AGENT_CHANNEL_ROLE).
CURRENT_IDENTITY: ContextVar[Identity | None] = ContextVar(
    "ai_agent_channel_identity", default=None
)


def data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_DATA_DIR


def admin_db_path() -> Path:
    return data_dir() / "admin.db"


def channel_db_path(channel: str) -> Path:
    return data_dir() / "channels" / f"{channel}.db"


# raw values already warned about, so a bad setting is logged once per value
# rather than on every key minted
_ttl_warned: set[str] = set()


def view_key_ttl_days() -> float:
    """The configured view-key lifetime in days, validated.

    Anything that is not a finite number within
    [VIEW_KEY_TTL_DAYS_MIN, VIEW_KEY_TTL_DAYS_MAX] falls back to the default
    with a warning. Refusing to start would take a live server down over a
    cosmetic setting; ``nan`` or ``inf`` silently accepted would either crash
    ``int()`` or mint keys that never expire.
    """
    raw = os.environ.get(VIEW_KEY_TTL_ENV, "").strip()
    if not raw:
        return VIEW_KEY_TTL_DAYS_DEFAULT
    try:
        days = float(raw)
    except ValueError:
        days = math.nan
    if math.isfinite(days) and VIEW_KEY_TTL_DAYS_MIN <= days <= VIEW_KEY_TTL_DAYS_MAX:
        return days
    if raw not in _ttl_warned:
        _ttl_warned.add(raw)
        log.warning(
            "%s=%r is not a number of days between %d and %d; using the default %d",
            VIEW_KEY_TTL_ENV,
            raw,
            VIEW_KEY_TTL_DAYS_MIN,
            VIEW_KEY_TTL_DAYS_MAX,
            VIEW_KEY_TTL_DAYS_DEFAULT,
        )
    return VIEW_KEY_TTL_DAYS_DEFAULT


def view_key_ttl_s() -> int:
    return int(view_key_ttl_days() * 86400)


def _schema_statements(script: str) -> list[str]:
    """ADMIN_SCHEMA as single statements. executescript() cannot be used
    inside the migration transaction (it COMMITs first), and a naive split
    on ';' would cut the comments, which contain semicolons."""
    statements: list[str] = []
    current: list[str] = []
    for line in script.splitlines():
        if line.lstrip().startswith("--"):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(current).strip())
            current = []
    if "\n".join(current).strip():
        statements.append("\n".join(current).strip())
    return statements


def _connect(path: Path) -> sqlite3.Connection:
    # the registry holds every token hash: owner-only, where we create it
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        enable_wal(conn)
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """One atomic write. The connection runs in autocommit mode, so a helper
    issuing several statements would otherwise commit each on its own: a
    failure halfway leaves half a rotation, and two concurrent add_role calls
    both read the old role list and one role is lost. BEGIN IMMEDIATE takes
    the write lock before the first read, so the second writer waits
    (busy_timeout) and then sees the first one's result. Nested use joins
    the outer transaction."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring admin.db to ADMIN_SCHEMA_VERSION. Runs once per registry, not
    once per request: the version is re-checked under the write lock, so two
    processes starting together migrate it exactly once."""
    with write_tx(conn):
        if conn.execute("PRAGMA user_version").fetchone()[0] >= ADMIN_SCHEMA_VERSION:
            return
        for stmt in _schema_statements(ADMIN_SCHEMA):
            conn.execute(stmt)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(tokens)")}
        had_provenance = "issued_by_role" in columns
        for stmt in ADMIN_MIGRATIONS:
            with suppress(sqlite3.OperationalError):  # already applied / fresh schema
                conn.execute(stmt)
        if not had_provenance:
            # every view key that exists now predates provenance
            conn.execute(
                "UPDATE tokens SET issuer_unknown = 1 WHERE kind = 'view' "
                "AND issued_by_role IS NULL AND issued_by_token_hash IS NULL"
            )
        # keys issued before expiry existed get the horizon counted from
        # their creation — once, here; a NULL horizon never means "forever"
        conn.execute(
            "UPDATE tokens SET expires_at = strftime('%Y-%m-%dT%H:%M:%fZ', "
            "created_at, ?) WHERE kind = 'view' AND expires_at IS NULL",
            (f"+{view_key_ttl_s()} seconds",),
        )
        conn.execute(f"PRAGMA user_version = {ADMIN_SCHEMA_VERSION:d}")


def migrate_admin_db(path: Path | None = None) -> None:
    """Create or upgrade the registry. The HTTP server calls this at startup;
    open_admin_db() also does it lazily for a registry it finds behind."""
    with open_admin_db(path):
        pass


@contextmanager
def open_admin_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path or admin_db_path())
    try:
        # one header read on the hot path; the schema script runs only for a
        # registry that has not been migrated yet
        if conn.execute("PRAGMA user_version").fetchone()[0] < ADMIN_SCHEMA_VERSION:
            _migrate(conn)
        yield conn
    finally:
        conn.close()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token(prefix: str = TOKEN_PREFIX) -> str:
    return prefix + secrets.token_urlsafe(24)


def _check_slug(value: str, field: str) -> str:
    value = (value or "").strip()
    if not NAME_RE.match(value):
        raise ValueError(f"'{field}' must match {NAME_RE.pattern} (lowercase slug), got '{value}'")
    return value


def is_admin_token(token: str) -> bool:
    expected = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    # bytes, not str: compare_digest raises TypeError on non-ASCII str, and a
    # header byte outside ASCII is the caller's garbage, not our 500
    return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())


def authenticate(token: str) -> Identity | None:
    """Resolve a bearer token to an Identity, or None if it grants nothing."""
    if not token:
        return None
    if is_admin_token(token):
        return Identity(is_admin=True)
    token_hash = _hash_token(token)
    with open_admin_db() as conn:
        row = conn.execute(
            f"""
            SELECT t.channel, t.role, t.kind, c.roles
            FROM tokens t JOIN channels c ON c.name = t.channel
            WHERE t.token_hash = ? AND t.revoked_at IS NULL
              AND c.deleted_at IS NULL
              AND (t.kind != 'view' OR {_VIEW_KEY_LIVE})
            """,
            (token_hash,),
        ).fetchone()
    if row is None:
        return None
    if row["kind"] == "view":
        # No role, so `_role()` in tools/common.py refuses it exactly where it
        # refuses the admin token — the read-only guarantee does not depend
        # on remembering to check a flag in each tool.
        return Identity(
            is_admin=False,
            channel=row["channel"],
            role=None,
            db_path=channel_db_path(row["channel"]),
            is_viewer=True,
            token_hash=token_hash,
        )
    roles = json.loads(row["roles"])
    return Identity(
        is_admin=False,
        channel=row["channel"],
        role=row["role"],
        peers=tuple(r for r in roles if r != row["role"]),
        db_path=channel_db_path(row["channel"]),
        token_hash=token_hash,
    )


def _issue_token(conn: sqlite3.Connection, *, channel: str, role: str, kind: str = "role") -> str:
    token = _new_token(VIEW_TOKEN_PREFIX if kind == "view" else TOKEN_PREFIX)
    conn.execute(
        "INSERT INTO tokens (token_hash, channel, role, kind) VALUES (?, ?, ?, ?)",
        (_hash_token(token), channel, role, kind),
    )
    return token


# A view key (alias `t`) opens anything only while it is unexpired and the
# token that issued it, if one did, is itself still live. A NULL expires_at
# is NOT "never expires": the migration gives every key a horizon, and a row
# that somehow lacks one opens nothing.
_VIEW_KEY_LIVE = f"""(
    t.expires_at > {_NOW}
    AND (t.issued_by_token_hash IS NULL OR EXISTS (
        SELECT 1 FROM tokens i
        WHERE i.token_hash = t.issued_by_token_hash AND i.revoked_at IS NULL))
)"""


def create_view_token(
    conn: sqlite3.Connection,
    *,
    channel: str,
    label: str = "board",
    issued_by_role: str | None = None,
    issued_by_token_hash: str | None = None,
) -> dict[str, Any]:
    """A read-only key for the board, issued in its own right.

    Not derived from any role token and not convertible into one: it carries
    no role, so it cannot address a mailbox, and the middleware refuses it
    anywhere except a board GET. Losing one costs a page view, not the
    ability to pin a charter or close somebody's debt.

    It records who issued it, expires after ``view_key_ttl_s()``, and dies
    with the role token that issued it.
    """
    label = _check_slug(label, "label")
    token = _new_token(VIEW_TOKEN_PREFIX)
    with write_tx(conn):
        _get_channel(conn, channel)
        row = conn.execute(
            "INSERT INTO tokens (token_hash, channel, role, kind, issued_by_role, "
            "issued_by_token_hash, expires_at) VALUES (?, ?, ?, 'view', ?, ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)) RETURNING expires_at",
            (
                _hash_token(token),
                channel,
                label,
                issued_by_role,
                issued_by_token_hash,
                f"+{view_key_ttl_s()} seconds",
            ),
        ).fetchone()
        # past the cap the oldest live keys of the same issuer go
        conn.execute(
            f"UPDATE tokens SET revoked_at = {_NOW} WHERE id IN ("
            "SELECT id FROM tokens WHERE channel = ? AND kind = 'view' "
            "AND revoked_at IS NULL AND issued_by_role IS ? "
            "ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (channel, issued_by_role, MAX_VIEW_KEYS_PER_ISSUER),
        )
    return {
        "channel": channel,
        "label": label,
        "token": token,
        "expires_at": row["expires_at"],
    }


def list_view_tokens(conn: sqlite3.Connection, *, channel: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT role AS label, issued_by_role, created_at, expires_at, revoked_at "
        "FROM tokens WHERE channel = ? AND kind = 'view' ORDER BY id",
        (channel,),
    ).fetchall()
    return [dict(r) for r in rows]


def revoke_view_tokens(
    conn: sqlite3.Connection, *, channel: str, issued_by_role: str | None = None
) -> int:
    """Revoke a channel's view keys — all of them, or only one role's."""
    sql = (
        f"UPDATE tokens SET revoked_at = {_NOW} "
        "WHERE channel = ? AND kind = 'view' AND revoked_at IS NULL"
    )
    params: tuple[Any, ...] = (channel,)
    if issued_by_role is not None:
        # a key whose issuer was never recorded may have been this role's
        sql += " AND (issued_by_role = ? OR issuer_unknown = 1)"
        params += (issued_by_role,)
    with write_tx(conn):
        cur = conn.execute(sql, params)
        _purge_board_state(conn)
    return cur.rowcount


def mint_board_nonce(conn: sqlite3.Connection, *, view_token: str) -> str:
    """A single-use value to put in a link, so the durable one never is."""
    nonce = secrets.token_urlsafe(18)
    conn.execute(
        "INSERT INTO board_nonces (nonce_hash, view_token_hash, expires_at) "
        "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?))",
        (_hash_token(nonce), _hash_token(view_token), f"+{BOARD_NONCE_TTL_S} seconds"),
    )
    return nonce


def _purge_board_state(conn: sqlite3.Connection) -> None:
    """Drop nonces and sessions that can no longer open anything. Called
    opportunistically on the board paths, so neither table grows forever."""
    conn.execute(f"DELETE FROM board_nonces WHERE used_at IS NOT NULL OR expires_at <= {_NOW}")
    conn.execute(
        f"DELETE FROM board_sessions WHERE expires_at <= {_NOW} OR NOT EXISTS ("
        "SELECT 1 FROM tokens t WHERE t.token_hash = board_sessions.view_token_hash "
        "AND t.revoked_at IS NULL)"
    )


def redeem_board_nonce(conn: sqlite3.Connection, *, nonce: str) -> dict[str, str] | None:
    """Burn a nonce and open a board session for the key it stands for.

    Returns ``{"channel", "session"}`` — the session secret goes into the
    cookie and only its hash is stored — or None. Burning happens in the same
    statement that reads the nonce, so two concurrent redemptions cannot both
    succeed.
    """
    with write_tx(conn):
        row = conn.execute(
            f"UPDATE board_nonces SET used_at = {_NOW} "
            f"WHERE nonce_hash = ? AND used_at IS NULL AND expires_at > {_NOW} "
            "RETURNING view_token_hash",
            (_hash_token(nonce),),
        ).fetchone()
        _purge_board_state(conn)
        if row is None:
            return None
        key = conn.execute(
            f"""
            SELECT t.channel, t.expires_at FROM tokens t
            JOIN channels c ON c.name = t.channel
            WHERE t.token_hash = ? AND t.kind = 'view' AND t.revoked_at IS NULL
              AND c.deleted_at IS NULL AND {_VIEW_KEY_LIVE}
            """,
            (row["view_token_hash"],),
        ).fetchone()
        if key is None:
            return None
        session = secrets.token_urlsafe(32)
        # a session never outlives its key (a live key always has a horizon)
        conn.execute(
            "INSERT INTO board_sessions (session_hash, view_token_hash, expires_at) "
            "VALUES (?, ?, min(strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?), ?))",
            (
                _hash_token(session),
                row["view_token_hash"],
                f"+{BOARD_SESSION_TTL_S} seconds",
                key["expires_at"],
            ),
        )
    return {"channel": key["channel"], "session": session}


def viewer_from_session(session: str) -> Identity | None:
    """Resolve a board cookie's session secret to a viewer, or None."""
    if not session:
        return None
    with open_admin_db() as conn:
        row = conn.execute(
            f"""
            SELECT t.channel, t.token_hash FROM board_sessions s
            JOIN tokens t ON t.token_hash = s.view_token_hash
            JOIN channels c ON c.name = t.channel
            WHERE s.session_hash = ? AND s.expires_at > {_NOW}
              AND t.kind = 'view' AND t.revoked_at IS NULL
              AND c.deleted_at IS NULL AND {_VIEW_KEY_LIVE}
            """,
            (_hash_token(session),),
        ).fetchone()
    if row is None:
        return None
    return Identity(
        is_admin=False,
        channel=row["channel"],
        role=None,
        db_path=channel_db_path(row["channel"]),
        is_viewer=True,
        token_hash=row["token_hash"],
    )


def create_channel(conn: sqlite3.Connection, *, name: str, roles: list[str]) -> dict[str, Any]:
    name = _check_slug(name, "name")
    if not isinstance(roles, (list, tuple)) or len(roles) < 2:
        raise ValueError("a channel needs at least two roles")
    if len(roles) > MAX_ROLES:
        raise ValueError(
            f"a channel holds at most {MAX_ROLES} roles — a bigger crowd is "
            f"an event bus, not a charter-governed team; split it"
        )
    roles = [_check_slug(r, "roles") for r in roles]
    if len(set(roles)) != len(roles):
        raise ValueError("roles must be DIFFERENT, got duplicates")
    with write_tx(conn):
        existing = conn.execute(
            "SELECT deleted_at FROM channels WHERE name = ?", (name,)
        ).fetchone()
        if existing is not None:
            state = "deleted" if existing["deleted_at"] else "active"
            raise ValueError(f"channel '{name}' already exists ({state}) — pick another name")
        conn.execute(
            "INSERT INTO channels (name, roles) VALUES (?, ?)",
            (name, json.dumps(roles)),
        )
        tokens = {role: _issue_token(conn, channel=name, role=role) for role in roles}
        row = conn.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
    return {
        "name": name,
        "roles": roles,
        "created_at": row["created_at"],
        # plaintext is shown exactly once — only hashes are stored
        "tokens": tokens,
    }


def list_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, roles, created_at FROM channels WHERE deleted_at IS NULL ORDER BY name"
    ).fetchall()
    return [
        {"name": r["name"], "roles": json.loads(r["roles"]), "created_at": r["created_at"]}
        for r in rows
    ]


def _get_channel(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM channels WHERE name = ? AND deleted_at IS NULL", (name,)
    ).fetchone()
    if row is None:
        raise ValueError(f"channel '{name}' not found")
    return row


def rotate_token(conn: sqlite3.Connection, *, channel: str, role: str) -> dict[str, Any]:
    with write_tx(conn):
        row = _get_channel(conn, channel)
        roles = json.loads(row["roles"])
        if role not in roles:
            raise ValueError(f"channel '{channel}' has roles {roles}, not '{role}'")
        conn.execute(
            "UPDATE tokens SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE channel = ? AND role = ? AND kind = 'role' AND revoked_at IS NULL",
            (channel, role),
        )
        # a rotation answers a leak, so whatever the leaked token issued goes
        # too — including keys whose issuer was never recorded
        revoke_view_tokens(conn, channel=channel, issued_by_role=role)
        token = _issue_token(conn, channel=channel, role=role)
    return {"channel": channel, "role": role, "token": token}


def add_role(conn: sqlite3.Connection, *, channel: str, role: str) -> dict[str, Any]:
    role = _check_slug(role, "role")
    # read-modify-write of the JSON role list: under one write lock, or two
    # concurrent calls both append to the same old list and one role is lost
    with write_tx(conn):
        row = _get_channel(conn, channel)
        roles = json.loads(row["roles"])
        if role in roles:
            raise ValueError(f"channel '{channel}' already has role '{role}'")
        if len(roles) + 1 > MAX_ROLES:
            raise ValueError(
                f"a channel holds at most {MAX_ROLES} roles — a bigger crowd is "
                f"an event bus, not a charter-governed team; split it"
            )
        roles.append(role)
        conn.execute("UPDATE channels SET roles = ? WHERE name = ?", (json.dumps(roles), channel))
        token = _issue_token(conn, channel=channel, role=role)
    return {"channel": channel, "role": role, "token": token, "roles": roles}


def delete_channel(conn: sqlite3.Connection, *, name: str) -> dict[str, Any]:
    with write_tx(conn):
        _get_channel(conn, name)
        conn.execute(
            "UPDATE tokens SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE channel = ? AND revoked_at IS NULL",
            (name,),
        )
        conn.execute(
            "UPDATE channels SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE name = ?",
            (name,),
        )
    # the mailbox file stays on disk (audit/history) — remove it manually if
    # the data must go
    return {"name": name, "deleted": True, "db_path": str(channel_db_path(name))}
