"""The registry (admin.db): view-key lifetime validation, keys without a
recorded issuer, one-time migration and atomic admin writes."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

import pytest

from ai_agent_channel import auth
from helpers import authenticated


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(auth.ADMIN_TOKEN_ENV, "test-admin-token")
    monkeypatch.delenv(auth.VIEW_KEY_TTL_ENV, raising=False)
    monkeypatch.setattr(auth, "_ttl_warned", set(), raising=False)
    return tmp_path


# --- AI_AGENT_CHANNEL_VIEW_KEY_TTL_DAYS --------------------------------------


@pytest.mark.parametrize(
    "raw", ["nan", "inf", "-inf", "1e309", "0", "0.5", "-3", "3651", "1e6", "soon"]
)
def test_an_invalid_view_key_ttl_falls_back_to_the_default(registry, monkeypatch, caplog, raw):
    monkeypatch.setenv(auth.VIEW_KEY_TTL_ENV, raw)
    with caplog.at_level(logging.WARNING, logger="ai_agent_channel.auth"):
        assert auth.view_key_ttl_s() == auth.VIEW_KEY_TTL_DAYS_DEFAULT * 86400
        # minting a key under the bad setting works and gets the default horizon
        with auth.open_admin_db() as conn:
            auth.create_channel(conn, name="proj", roles=["a", "b"])
            assert auth.create_view_token(conn, channel="proj")["expires_at"]
    warnings = [r for r in caplog.records if auth.VIEW_KEY_TTL_ENV in r.getMessage()]
    assert len(warnings) == 1  # once per value, not once per key


@pytest.mark.parametrize(("raw", "days"), [("1", 1), ("2.5", 2.5), ("3650", 3650), ("", 30)])
def test_a_valid_view_key_ttl_is_used(registry, monkeypatch, caplog, raw, days):
    monkeypatch.setenv(auth.VIEW_KEY_TTL_ENV, raw)
    with caplog.at_level(logging.WARNING, logger="ai_agent_channel.auth"):
        assert auth.view_key_ttl_s() == int(days * 86400)
    assert not caplog.records


def test_a_view_key_without_a_horizon_opens_nothing(registry):
    """NULL expires_at used to be read as "never expires" (and was quietly
    re-backfilled on every open). The migration gives every existing key a
    horizon once; after that a missing one is not a licence."""
    with auth.open_admin_db() as conn:
        auth.create_channel(conn, name="proj", roles=["a", "b"])
        key = auth.create_view_token(conn, channel="proj")
        nonce = auth.mint_board_nonce(conn, view_token=key["token"])
        conn.execute("UPDATE tokens SET expires_at = NULL WHERE kind = 'view'")
    assert auth.authenticate(key["token"]) is None
    with auth.open_admin_db() as conn:
        assert auth.redeem_board_nonce(conn, nonce=nonce) is None


# --- migration ----------------------------------------------------------------


def _legacy_registry(path) -> None:
    """A registry from before view-key provenance and expiry."""
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE channels (
            name TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT
            (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), deleted_at TEXT NULL, roles TEXT);
        CREATE TABLE tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
            channel TEXT NOT NULL REFERENCES channels(name), role TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            revoked_at TEXT NULL, kind TEXT NOT NULL DEFAULT 'role');
        CREATE TABLE board_nonces (
            nonce_hash TEXT PRIMARY KEY, channel TEXT NULL, view_token_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL, used_at TEXT NULL);
        """
    )
    for channel in ("chan", "other"):
        legacy.execute(
            "INSERT INTO channels (name, roles) VALUES (?, ?)", (channel, json.dumps(["a", "b"]))
        )
        for role in ("a", "b"):
            legacy.execute(
                "INSERT INTO tokens (token_hash, channel, role) VALUES (?, ?, ?)",
                (auth._hash_token(f"cct_{channel}_{role}"), channel, role),
            )
        # the label, not the issuer: a legacy key does not say who minted it
        legacy.execute(
            "INSERT INTO tokens (token_hash, channel, role, kind) VALUES (?, ?, 'board', 'view')",
            (auth._hash_token(f"ccv_{channel}"), channel),
        )
    legacy.commit()
    legacy.close()


def test_migration_runs_once_and_records_the_schema_version(registry, monkeypatch):
    _legacy_registry(auth.admin_db_path())
    calls = []
    real = auth._migrate
    monkeypatch.setattr(auth, "_migrate", lambda conn: (calls.append(1), real(conn))[1])
    auth.migrate_admin_db()
    versions = []
    for _ in range(5):
        with auth.open_admin_db() as conn:
            versions.append(conn.execute("PRAGMA user_version").fetchone()[0])
    assert calls == [1]
    assert versions == [auth.ADMIN_SCHEMA_VERSION] * 5
    for token in ("cct_chan_a", "cct_chan_b", "cct_other_a", "cct_other_b"):
        assert auth.authenticate(token) is not None  # existing tokens keep working
    assert authenticated("ccv_chan").is_viewer


def test_two_processes_migrating_at_once_both_succeed(registry):
    _legacy_registry(auth.admin_db_path())
    errors: list[BaseException] = []

    def migrate():
        try:
            auth.migrate_admin_db()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert authenticated("cct_chan_a").role == "a"


def test_rotating_a_role_revokes_view_keys_whose_issuer_is_unknown(registry):
    """A key minted before provenance was recorded may have been handed out
    by the role whose token leaked. Rotation answers a leak, so it goes —
    but only in that channel, and keys minted since keep their own record."""
    _legacy_registry(auth.admin_db_path())
    with auth.open_admin_db() as conn:
        by_admin = auth.create_view_token(conn, channel="chan")
        by_b = auth.create_view_token(conn, channel="chan", issued_by_role="b")
        auth.rotate_token(conn, channel="chan", role="a")
    assert auth.authenticate("ccv_chan") is None
    assert authenticated("ccv_other").is_viewer
    assert authenticated(by_admin["token"]).is_viewer
    assert authenticated(by_b["token"]).is_viewer


# --- atomic admin writes ------------------------------------------------------


def test_concurrent_add_role_loses_no_role(registry, monkeypatch):
    with auth.open_admin_db() as conn:
        auth.create_channel(conn, name="proj", roles=["a", "b"])
    real_get = auth._get_channel

    def slow_get(conn, name):
        # widen the window between reading the role list and writing it back
        row = real_get(conn, name)
        time.sleep(0.3)
        return row

    monkeypatch.setattr(auth, "_get_channel", slow_get)
    errors: list[BaseException] = []
    tokens: dict[str, str] = {}

    def add(role):
        try:
            with auth.open_admin_db() as conn:
                tokens[role] = auth.add_role(conn, channel="proj", role=role)["token"]
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(r,)) for r in ("c", "d")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with auth.open_admin_db() as conn:
        roles = auth.list_channels(conn)[0]["roles"]
    assert sorted(roles) == ["a", "b", "c", "d"]
    assert set(authenticated(tokens["c"]).peers) == {"a", "b", "d"}


def test_a_rotation_that_fails_halfway_changes_nothing(registry, monkeypatch):
    with auth.open_admin_db() as conn:
        created = auth.create_channel(conn, name="proj", roles=["a", "b"])
        key = auth.create_view_token(conn, channel="proj", issued_by_role="a")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(auth, "_issue_token", broken)
    with auth.open_admin_db() as conn, pytest.raises(sqlite3.OperationalError):
        auth.rotate_token(conn, channel="proj", role="a")
    # no half-rotation: the old token and its view key still work
    assert authenticated(created["tokens"]["a"]).role == "a"
    assert authenticated(key["token"]).is_viewer


def test_create_channel_is_all_or_nothing(registry, monkeypatch):
    real = auth._issue_token
    issued = []

    def second_fails(conn, **kwargs):
        if issued:
            raise sqlite3.OperationalError("disk I/O error")
        issued.append(kwargs["role"])
        return real(conn, **kwargs)

    monkeypatch.setattr(auth, "_issue_token", second_fails)
    with auth.open_admin_db() as conn, pytest.raises(sqlite3.OperationalError):
        auth.create_channel(conn, name="proj", roles=["a", "b"])
    monkeypatch.setattr(auth, "_issue_token", real)
    with auth.open_admin_db() as conn:
        # the name is not burned by a creation that never completed
        assert auth.create_channel(conn, name="proj", roles=["a", "b"])["name"] == "proj"
