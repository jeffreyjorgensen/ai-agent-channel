"""Unit tests for the channel/token registry (auth.py) — no HTTP involved."""

from __future__ import annotations

import pytest

from ai_agent_channel import auth
from helpers import authenticated


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(auth.ADMIN_TOKEN_ENV, "test-admin-token")
    return tmp_path


def test_create_channel_returns_two_tokens(data_dir):
    with auth.open_admin_db() as conn:
        created = auth.create_channel(conn, name="proj", roles=["frontend", "backend"])
    assert created["name"] == "proj"
    assert created["roles"] == ["frontend", "backend"]
    assert set(created["tokens"]) == {"frontend", "backend"}
    for token in created["tokens"].values():
        assert token.startswith(auth.TOKEN_PREFIX)
    # tokens are stored hashed, never in plaintext
    with auth.open_admin_db() as conn:
        stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM tokens")]
    assert all(t not in stored for t in created["tokens"].values())


def test_resolve_role_token(data_dir):
    with auth.open_admin_db() as conn:
        created = auth.create_channel(conn, name="proj", roles=["frontend", "backend"])
    ident = auth.authenticate(created["tokens"]["frontend"])
    assert ident is not None and not ident.is_admin
    assert (ident.channel, ident.role, ident.peers) == ("proj", "frontend", ("backend",))
    assert ident.db_path == auth.channel_db_path("proj")
    # peer is symmetric
    other = authenticated(created["tokens"]["backend"])
    assert (other.role, other.peers) == ("backend", ("frontend",))


def test_resolve_admin_and_garbage(data_dir):
    admin = auth.authenticate("test-admin-token")
    assert admin is not None and admin.is_admin and admin.role is None
    assert auth.authenticate("") is None
    assert auth.authenticate("cct_nonsense") is None


def test_admin_token_not_configured(data_dir, monkeypatch):
    monkeypatch.delenv(auth.ADMIN_TOKEN_ENV)
    assert auth.authenticate("test-admin-token") is None


def test_channel_validation(data_dir):
    with auth.open_admin_db() as conn:
        with pytest.raises(ValueError, match="lowercase slug"):
            auth.create_channel(conn, name="Bad Name!", roles=["a", "b"])
        with pytest.raises(ValueError, match="lowercase slug"):
            auth.create_channel(conn, name="ok", roles=["../etc", "b"])
        with pytest.raises(ValueError, match="DIFFERENT"):
            auth.create_channel(conn, name="ok", roles=["same", "same"])
        auth.create_channel(conn, name="proj", roles=["a", "b"])
        with pytest.raises(ValueError, match="already exists"):
            auth.create_channel(conn, name="proj", roles=["x", "y"])


def test_rotate_token_revokes_old(data_dir):
    with auth.open_admin_db() as conn:
        created = auth.create_channel(conn, name="proj", roles=["a", "b"])
        rotated = auth.rotate_token(conn, channel="proj", role="a")
    assert auth.authenticate(created["tokens"]["a"]) is None
    assert authenticated(rotated["token"]).role == "a"
    # the other role's token is untouched
    assert authenticated(created["tokens"]["b"]).role == "b"
    with auth.open_admin_db() as conn:
        with pytest.raises(ValueError, match="not 'nope'"):
            auth.rotate_token(conn, channel="proj", role="nope")
        with pytest.raises(ValueError, match="not found"):
            auth.rotate_token(conn, channel="ghost", role="a")


def test_rotation_revokes_the_view_keys_that_role_issued(data_dir):
    with auth.open_admin_db() as conn:
        created = auth.create_channel(conn, name="proj", roles=["a", "b"])
        a_hash = auth._hash_token(created["tokens"]["a"])
        by_a = auth.create_view_token(
            conn, channel="proj", issued_by_role="a", issued_by_token_hash=a_hash
        )
        by_b = auth.create_view_token(conn, channel="proj", issued_by_role="b")
        by_admin = auth.create_view_token(conn, channel="proj")
        # a view key labelled like a role is not that role's token
        labelled_a = auth.create_view_token(conn, channel="proj", label="a")
        auth.rotate_token(conn, channel="proj", role="a")
    assert auth.authenticate(by_a["token"]) is None
    for survivor in (by_b, by_admin, labelled_a):
        assert authenticated(survivor["token"]).is_viewer


def test_admin_token_compare_survives_non_ascii(data_dir):
    # str compare_digest raises TypeError on non-ASCII; that was a 500
    assert auth.is_admin_token("tést-admin-token") is False
    assert auth.authenticate("\xe9\xff") is None


def test_delete_channel_revokes_everything(data_dir):
    with auth.open_admin_db() as conn:
        created = auth.create_channel(conn, name="proj", roles=["a", "b"])
        auth.delete_channel(conn, name="proj")
        assert auth.list_channels(conn) == []
    for token in created["tokens"].values():
        assert auth.authenticate(token) is None
    # the name is burned — no silent resurrection with new tokens
    with auth.open_admin_db() as conn, pytest.raises(ValueError, match="already exists"):
        auth.create_channel(conn, name="proj", roles=["a", "b"])


def test_legacy_admin_db_migrates_to_roles_list(data_dir):
    # a registry created before multi-role channels: fixed role_a/role_b pair
    import sqlite3

    legacy = sqlite3.connect(auth.admin_db_path())
    legacy.executescript(
        """
        CREATE TABLE channels (
            name TEXT PRIMARY KEY,
            role_a TEXT NOT NULL,
            role_b TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            deleted_at TEXT NULL
        );
        CREATE TABLE tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            channel TEXT NOT NULL REFERENCES channels(name),
            role TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            revoked_at TEXT NULL
        );
        INSERT INTO channels (name, role_a, role_b) VALUES ('old', 'frontend', 'backend');
        """
    )
    legacy.execute(
        "INSERT INTO tokens (token_hash, channel, role) VALUES (?, 'old', 'frontend')",
        (auth._hash_token("cct_legacy"),),
    )
    legacy.commit()
    legacy.close()

    with auth.open_admin_db() as conn:  # migrations run here
        assert auth.list_channels(conn) == [
            {
                "name": "old",
                "roles": ["frontend", "backend"],
                "created_at": conn.execute("SELECT created_at FROM channels").fetchone()[0],
            }
        ]
    ident = authenticated("cct_legacy")
    assert (ident.role, ident.peers) == ("frontend", ("backend",))


def test_add_role_grows_channel(data_dir):
    with auth.open_admin_db() as conn:
        created = auth.create_channel(conn, name="proj", roles=["frontend", "backend"])
        added = auth.add_role(conn, channel="proj", role="infra")
    assert added["role"] == "infra"
    assert added["roles"] == ["frontend", "backend", "infra"]
    assert added["token"].startswith(auth.TOKEN_PREFIX)
    # the new role authenticates and sees the existing members as peers
    ident = authenticated(added["token"])
    assert (ident.channel, ident.role) == ("proj", "infra")
    assert set(ident.peers) == {"frontend", "backend"}
    # existing tokens keep working and now count the newcomer as a peer
    fe = authenticated(created["tokens"]["frontend"])
    assert set(fe.peers) == {"backend", "infra"}


def test_add_role_validation(data_dir):
    with auth.open_admin_db() as conn:
        auth.create_channel(conn, name="proj", roles=["a", "b"])
        with pytest.raises(ValueError, match="already has role"):
            auth.add_role(conn, channel="proj", role="a")
        with pytest.raises(ValueError, match="lowercase slug"):
            auth.add_role(conn, channel="proj", role="Bad Name!")
        with pytest.raises(ValueError, match="not found"):
            auth.add_role(conn, channel="ghost", role="c")
        # fill up to the cap, then the next add is refused
        for i in range(len(["a", "b"]), auth.MAX_ROLES):
            auth.add_role(conn, channel="proj", role=f"r{i}")
        with pytest.raises(ValueError, match="at most"):
            auth.add_role(conn, channel="proj", role="overflow")


def test_create_channel_role_count_limits(data_dir):
    with auth.open_admin_db() as conn:
        with pytest.raises(ValueError, match="at least two"):
            auth.create_channel(conn, name="solo", roles=["only"])
        with pytest.raises(ValueError, match="at most"):
            auth.create_channel(conn, name="crowd", roles=[f"r{i}" for i in range(13)])
        created = auth.create_channel(conn, name="trio", roles=["a", "b", "c"])
    assert set(created["tokens"]) == {"a", "b", "c"}
    ident = authenticated(created["tokens"]["b"])
    assert ident.peers == ("a", "c")
