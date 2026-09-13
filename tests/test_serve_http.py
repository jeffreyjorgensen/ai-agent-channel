"""`serve_http`: what it refuses, what it only warns about, and the proxy
settings it hands to uvicorn."""

from __future__ import annotations

import logging
import os
import sqlite3

import pytest
import uvicorn

from ai_agent_channel import auth
from ai_agent_channel import http as channel_http

LONG_TOKEN = "cca_" + "x" * 43


@pytest.fixture
def started(tmp_path, monkeypatch) -> list[dict]:
    """serve_http with uvicorn.run captured instead of serving."""
    calls: list[dict] = []
    monkeypatch.setenv(auth.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(auth.ADMIN_TOKEN_ENV, LONG_TOKEN)
    for name in (
        channel_http.TRUST_PROXY_ENV,
        channel_http.FORWARDED_ALLOW_IPS_ENV,
        channel_http.ALLOWED_HOSTS_ENV,
        auth.VIEW_KEY_TTL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_ttl_warned", set(), raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    # serve_http tightens the process umask; keep the test process's own
    monkeypatch.setattr(os, "umask", lambda mask: 0o022)
    return calls


def test_uvicorn_trusts_forwarded_headers_from_loopback_only_by_default(started):
    channel_http.serve_http()
    (kwargs,) = started
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "127.0.0.1"


def test_trust_proxy_extends_to_uvicorn(started, monkeypatch):
    monkeypatch.setenv(channel_http.TRUST_PROXY_ENV, "1")
    channel_http.serve_http()
    assert started[0]["forwarded_allow_ips"] == "*"


def test_uvicorns_own_variable_is_honoured(started, monkeypatch):
    monkeypatch.setenv(channel_http.FORWARDED_ALLOW_IPS_ENV, "10.0.0.2")
    channel_http.serve_http()
    assert started[0]["forwarded_allow_ips"] == "10.0.0.2"


def test_a_short_admin_token_is_a_warning_not_a_refusal(started, monkeypatch, caplog):
    monkeypatch.setenv(auth.ADMIN_TOKEN_ENV, "short-but-set")
    with caplog.at_level(logging.WARNING, logger="ai_agent_channel.http"):
        channel_http.serve_http()
    assert len(started) == 1  # it started
    assert any(auth.ADMIN_TOKEN_ENV in r.getMessage() for r in caplog.records)


def test_a_long_admin_token_starts_quietly(started, caplog):
    with caplog.at_level(logging.WARNING, logger="ai_agent_channel.http"):
        channel_http.serve_http()
    assert not [r for r in caplog.records if auth.ADMIN_TOKEN_ENV in r.getMessage()]


def test_an_invalid_view_key_ttl_is_reported_at_startup(started, monkeypatch, caplog):
    monkeypatch.setenv(auth.VIEW_KEY_TTL_ENV, "nan")
    with caplog.at_level(logging.WARNING):
        channel_http.serve_http()
    assert len(started) == 1
    assert any(auth.VIEW_KEY_TTL_ENV in r.getMessage() for r in caplog.records)


def test_the_registry_is_migrated_at_startup(started, tmp_path):
    channel_http.serve_http()
    conn = sqlite3.connect(tmp_path / "admin.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == auth.ADMIN_SCHEMA_VERSION
    finally:
        conn.close()


def test_a_blank_admin_token_is_refused(started, monkeypatch):
    monkeypatch.setenv(auth.ADMIN_TOKEN_ENV, "   ")
    with pytest.raises(SystemExit, match=auth.ADMIN_TOKEN_ENV):
        channel_http.serve_http()
    assert started == []


def test_an_unopenable_registry_is_refused(started, tmp_path):
    (tmp_path / "admin.db").mkdir()  # a directory where the database should be
    with pytest.raises(SystemExit, match="not usable"):
        channel_http.serve_http()
    assert started == []
