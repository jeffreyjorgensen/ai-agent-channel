"""Regression tests for the hosted surface's hardening: board sessions,
view-key lifetime, response headers, the Host allowlist, proxy trust and
resource caps. Each test here failed against the code before its fix."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import stat
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ai_agent_channel import auth, server
from ai_agent_channel import http as channel_http
from helpers import (
    ADMIN_TOKEN,
    authenticated,
    client,
    make_channel,
    payload,
    present,
    running_server,
)


async def link_for(base_url: str, token: str, channel: str | None = None) -> dict:
    args = {"channel": channel} if channel else {}
    async with client(base_url, token) as c:
        return payload(await c.call_tool("board_link", args))


def open_board(base_url: str, link: dict, **headers: str) -> tuple[httpx.Client, httpx.Response]:
    """Redeem a link and return an unopened client holding the cookie, so the
    caller can `with` it, plus the redeeming response."""
    browser = httpx.Client(base_url=base_url)
    with httpx.Client(base_url=base_url) as opener:
        first = opener.get(link["url"], headers=headers, follow_redirects=False)
        assert first.status_code == 303, first.text
        browser.cookies.update(opener.cookies)
    return browser, first


def cookie_value(resp: httpx.Response) -> str:
    return resp.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


# --- L2: a non-ASCII bearer is a 401, not a 500 ------------------------------


async def test_non_ascii_bearer_is_rejected_not_a_server_error(http_server):
    garbage = {b"authorization": b"Bearer cct_\xe9\xff"}
    assert httpx.get(http_server + "/status", headers=garbage).status_code == 401
    assert httpx.post(http_server + "/mcp", json={}, headers=garbage).status_code == 401
    assert httpx.get(http_server + "/board", headers=garbage).status_code == 401


# --- L1: the cookie is a session, not the stored key -------------------------


async def test_board_cookie_is_not_the_value_stored_in_the_registry(http_server):
    await make_channel(http_server)
    browser, first = open_board(http_server, await link_for(http_server, ADMIN_TOKEN, "proj"))
    held = cookie_value(first)
    with auth.open_admin_db() as conn:
        token_hashes = {r[0] for r in conn.execute("SELECT token_hash FROM tokens")}
        sessions = {r[0] for r in conn.execute("SELECT session_hash FROM board_sessions")}
        view_hash = conn.execute("SELECT token_hash FROM tokens WHERE kind = 'view'").fetchone()[0]
    assert held not in token_hashes and held not in sessions
    assert hashlib.sha256(held.encode()).hexdigest() in sessions
    with browser:
        assert browser.get("/board/proj").status_code == 200
    # what a leaked admin.db holds opens nothing
    forged = httpx.get(http_server + "/board/proj", cookies={channel_http.BOARD_COOKIE: view_hash})
    assert forged.status_code == 401


# --- M2: view keys die with their issuer, expire, and are capped -------------


async def test_rotating_a_role_revokes_the_board_links_it_issued(http_server):
    created = await make_channel(http_server)
    by_role, _ = open_board(http_server, await link_for(http_server, created["tokens"]["frontend"]))
    by_admin, _ = open_board(http_server, await link_for(http_server, ADMIN_TOKEN, "proj"))
    with by_role, by_admin:
        assert by_role.get("/board/proj").status_code == 200
        async with client(http_server, ADMIN_TOKEN) as admin:
            payload(await admin.call_tool("rotate_token", {"channel": "proj", "role": "frontend"}))
        assert by_role.get("/board/proj").status_code == 401
        assert by_admin.get("/board/proj").status_code == 200
    with auth.open_admin_db() as conn:
        keys = auth.list_view_tokens(conn, channel="proj")
    assert [k["issued_by_role"] for k in keys if k["revoked_at"]] == ["frontend"]


async def test_deleting_a_channel_revokes_its_board_links(http_server):
    created = await make_channel(http_server)
    browser, _ = open_board(http_server, await link_for(http_server, created["tokens"]["backend"]))
    with browser:
        async with client(http_server, ADMIN_TOKEN) as admin:
            payload(await admin.call_tool("delete_channel", {"name": "proj"}))
        assert browser.get("/board/proj").status_code == 401


async def test_view_key_expiry_is_enforced_on_cookie_use(http_server):
    await make_channel(http_server)
    browser, _ = open_board(http_server, await link_for(http_server, ADMIN_TOKEN, "proj"))
    with browser:
        assert browser.get("/board/proj").status_code == 200
        with auth.open_admin_db() as conn:
            conn.execute(
                "UPDATE tokens SET expires_at = '2000-01-01T00:00:00.000Z' WHERE kind = 'view'"
            )
        assert browser.get("/board/proj").status_code == 401


async def test_board_session_expiry_is_enforced(http_server):
    await make_channel(http_server)
    browser, _ = open_board(http_server, await link_for(http_server, ADMIN_TOKEN, "proj"))
    with browser:
        with auth.open_admin_db() as conn:
            conn.execute("UPDATE board_sessions SET expires_at = '2000-01-01T00:00:00.000Z'")
        assert browser.get("/board/proj").status_code == 401


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(auth.ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    monkeypatch.delenv(auth.VIEW_KEY_TTL_ENV, raising=False)
    return tmp_path


def test_view_key_lifetime_defaults_to_thirty_days_and_is_configurable(registry, monkeypatch):
    with auth.open_admin_db() as conn:
        auth.create_channel(conn, name="proj", roles=["a", "b"])
        default = auth.create_view_token(conn, channel="proj")
        monkeypatch.setenv(auth.VIEW_KEY_TTL_ENV, "2")
        short = auth.create_view_token(conn, channel="proj")
    now = datetime.now(UTC)
    assert abs(_parse(default["expires_at"]) - (now + timedelta(days=30))) < timedelta(minutes=1)
    assert abs(_parse(short["expires_at"]) - (now + timedelta(days=2))) < timedelta(minutes=1)


def test_live_view_keys_are_capped_per_issuer(registry):
    cap = auth.MAX_VIEW_KEYS_PER_ISSUER
    with auth.open_admin_db() as conn:
        auth.create_channel(conn, name="proj", roles=["a", "b"])
        admin_key = auth.create_view_token(conn, channel="proj")
        keys = [
            auth.create_view_token(conn, channel="proj", issued_by_role="a") for _ in range(cap + 5)
        ]
    assert all(auth.authenticate(k["token"]) is None for k in keys[:5])
    assert all(auth.authenticate(k["token"]) is not None for k in keys[5:])
    assert auth.authenticate(admin_key["token"]) is not None


def test_existing_registry_migrates_in_place(registry):
    """The live registry predates key provenance and expiry: it must gain the
    columns without losing a row, and every role token must keep working."""
    legacy = sqlite3.connect(auth.admin_db_path())
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
    plaintext: dict[str, tuple[str, str]] = {}
    for c in range(3):
        roles = [f"role{i}" for i in range(10 if c < 2 else 9)]
        legacy.execute(
            "INSERT INTO channels (name, roles) VALUES (?, ?)", (f"chan{c}", json.dumps(roles))
        )
        for role in roles:
            token = f"cct_legacy_{c}_{role}"
            plaintext[token] = (f"chan{c}", role)
            legacy.execute(
                "INSERT INTO tokens (token_hash, channel, role) VALUES (?, ?, ?)",
                (auth._hash_token(token), f"chan{c}", role),
            )
    legacy.execute(
        "INSERT INTO tokens (token_hash, channel, role, kind) VALUES (?, 'chan0', 'board', 'view')",
        (auth._hash_token("ccv_legacy"),),
    )
    legacy.commit()
    legacy.close()
    assert len(plaintext) == 29

    with auth.open_admin_db() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(tokens)")}
        assert {"issued_by_role", "issued_by_token_hash", "expires_at"} <= columns
        assert conn.execute("SELECT count(*) FROM tokens").fetchone()[0] == 30
        assert len(auth.list_channels(conn)) == 3
        view = conn.execute("SELECT expires_at FROM tokens WHERE kind = 'view'").fetchone()
    assert view["expires_at"] is not None
    for token, (channel, role) in plaintext.items():
        ident = authenticated(token)
        assert (ident.channel, ident.role) == (channel, role)
    assert authenticated("ccv_legacy").is_viewer


def test_spent_and_expired_nonces_are_purged(registry):
    with auth.open_admin_db() as conn:
        auth.create_channel(conn, name="proj", roles=["a", "b"])
        key = auth.create_view_token(conn, channel="proj")
        stale = auth.mint_board_nonce(conn, view_token=key["token"])
        fresh = auth.mint_board_nonce(conn, view_token=key["token"])
        conn.execute(
            "UPDATE board_nonces SET expires_at = '2000-01-01T00:00:00.000Z' WHERE nonce_hash = ?",
            (auth._hash_token(stale),),
        )
        assert auth.redeem_board_nonce(conn, nonce=fresh) is not None
        assert conn.execute("SELECT count(*) FROM board_nonces").fetchone()[0] == 0
        assert auth.redeem_board_nonce(conn, nonce=fresh) is None


def test_registry_files_are_owner_only(tmp_path, monkeypatch):
    data = tmp_path / "fresh" / "data"
    monkeypatch.setenv(auth.DATA_DIR_ENV, str(data))
    with auth.open_admin_db():
        pass
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(auth.admin_db_path().stat().st_mode) == 0o600


# --- L4: board responses carry security headers ------------------------------


async def test_board_responses_carry_security_headers(http_server):
    await make_channel(http_server)
    link = await link_for(http_server, ADMIN_TOKEN, "proj")
    browser, redirect = open_board(http_server, link)
    with browser:
        page = browser.get("/board/proj")
    denied = httpx.get(http_server + "/board")
    for resp in (redirect, page, denied):
        assert resp.headers["content-security-policy"] == channel_http.BOARD_CSP
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["referrer-policy"] == "no-referrer"
    # the policy allows no script and no external resource: the page has none
    assert page.status_code == 200 and "<style>" in page.text
    for needle in ("<script", "<link", " src=", "style="):
        assert needle not in page.text


# --- Info: the redirect targets the key's channel, not the request path ------


async def test_redeem_redirects_to_the_keys_channel(http_server):
    await make_channel(http_server)
    link = await link_for(http_server, ADMIN_TOKEN, "proj")
    nonce = link["url"].split("?t=", 1)[1]
    resp = httpx.get(http_server + f"/board/elsewhere/feed?t={nonce}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/board/proj"


# --- Info: X-Forwarded-Proto is believed only from a proxy -------------------


async def test_cookie_is_secure_behind_a_tls_proxy(http_server):
    await make_channel(http_server)
    _, behind_tls = open_board(
        http_server,
        await link_for(http_server, ADMIN_TOKEN, "proj"),
        **{"x-forwarded-proto": "https"},
    )
    _, plain = open_board(http_server, await link_for(http_server, ADMIN_TOKEN, "proj"))
    assert "; Secure" in behind_tls.headers["set-cookie"]
    assert "Secure" not in plain.headers["set-cookie"]


def test_forwarded_proto_is_only_trusted_from_a_proxy(monkeypatch):
    monkeypatch.delenv(channel_http.TRUST_PROXY_ENV, raising=False)
    forwarded = [(b"x-forwarded-proto", b"https")]
    # a globally routable peer (not TEST-NET: Python counts those as private)
    public = {"client": ("8.8.8.8", 4000), "headers": forwarded}
    assert channel_http._is_secure(public) is False
    assert channel_http._is_secure({"client": ("172.18.0.5", 4000), "headers": forwarded})
    assert channel_http._is_secure({"client": ("127.0.0.1", 4000), "headers": forwarded})
    assert channel_http._is_secure({"scheme": "https", "client": ("8.8.8.8", 1)})
    monkeypatch.setenv(channel_http.TRUST_PROXY_ENV, "1")
    assert channel_http._is_secure(public) is True


# --- L3: Host allowlist ------------------------------------------------------


@pytest.fixture
def hosts_server(tmp_path, monkeypatch):
    with running_server(
        tmp_path, monkeypatch, AI_AGENT_CHANNEL_ALLOWED_HOSTS="channel.test"
    ) as url:
        yield url


def test_allowed_hosts_rejects_other_hosts_everywhere(hosts_server):
    from ai_agent_channel.server import mcp

    settings = present(mcp.settings.transport_security)
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["channel.test"]
    assert "https://channel.test" in settings.allowed_origins

    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    good = {"Host": "channel.test"}
    # the default Host is 127.0.0.1:<port>, which is not on the list
    assert httpx.get(hosts_server + "/healthz").status_code == 200
    assert httpx.get(hosts_server + "/status").status_code == 421
    assert httpx.get(hosts_server + "/board").status_code == 421
    assert httpx.post(hosts_server + "/mcp", json={}, headers=admin).status_code == 421
    # the right Host gets through to authentication
    assert httpx.get(hosts_server + "/status", headers=good).status_code == 401
    assert httpx.get(hosts_server + "/board", headers=good).status_code == 401
    evil = {**good, **admin, "Origin": "https://evil.test"}
    assert httpx.post(hosts_server + "/mcp", json={}, headers=evil).status_code == 403

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    ok = httpx.post(
        hosts_server + "/mcp",
        json=initialize,
        headers={
            **good,
            **admin,
            "Origin": "https://channel.test",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert ok.status_code == 200, ok.text


def test_unset_allowed_hosts_logs_a_startup_warning(tmp_path, monkeypatch, caplog):
    from ai_agent_channel.server import mcp

    # running_server clears the allowlist and builds the app the way a real
    # start does, session-manager reset included
    with (
        caplog.at_level(logging.WARNING, logger="ai_agent_channel.http"),
        running_server(tmp_path, monkeypatch),
    ):
        pass
    assert any(channel_http.ALLOWED_HOSTS_ENV in r.getMessage() for r in caplog.records)
    assert present(mcp.settings.transport_security).enable_dns_rebinding_protection is False


# --- L5: held long polls are capped ------------------------------------------


async def test_concurrent_long_polls_are_capped(http_server, monkeypatch):
    created = await make_channel(http_server)
    monkeypatch.setattr(channel_http, "STATUS_MAX_WAITERS", 1)
    monkeypatch.setattr(channel_http, "STATUS_POLL_INTERVAL_S", 0.05)
    # the held request reads the channel only after it has taken its slot,
    # so its first read is the moment the slot is known to be occupied
    holding = threading.Event()
    real_summary = channel_http.db.channel_summary

    def summary(*args, **kwargs):
        holding.set()
        return real_summary(*args, **kwargs)

    monkeypatch.setattr(channel_http.db, "channel_summary", summary)
    backend = {"Authorization": f"Bearer {created['tokens']['backend']}"}
    async with httpx.AsyncClient(timeout=30) as http:
        held = asyncio.create_task(http.get(http_server + "/status?wait=30", headers=backend))
        assert await asyncio.to_thread(holding.wait, 5)
        refused = await http.get(http_server + "/status?wait=3", headers=backend)
        # an immediate answer holds nothing, so it is never refused
        instant = await http.get(http_server + "/status", headers=backend)
        # an arrival releases the held request instead of waiting it out
        async with client(http_server, created["tokens"]["frontend"]) as fe:
            payload(
                await fe.call_tool("send_message", {"to": "backend", "topic": "t", "body": "b"})
            )
        assert (await held).status_code == 200
        # a full house answers 429 before looking at the channel, so a 200
        # here means the slot was returned
        again = await http.get(http_server + "/status?wait=1", headers=backend)
    assert refused.status_code == 429
    assert refused.headers["retry-after"]
    assert instant.status_code == 200
    assert again.status_code == 200  # the slot was given back


# --- board_link refusals -----------------------------------------------------


def test_board_link_refusals_outside_a_channel_identity(registry):
    started = time.monotonic()
    with pytest.raises(PermissionError, match="HTTP transport"):
        server.board_link()  # stdio: no identity at all
    for ident, error, text in (
        (auth.Identity(is_admin=False), PermissionError, "no channel"),
        (auth.Identity(is_admin=True), ValueError, "'channel' is required"),
    ):
        token = auth.CURRENT_IDENTITY.set(ident)
        try:
            with pytest.raises(error, match=text):
                server.board_link()
        finally:
            auth.CURRENT_IDENTITY.reset(token)
    assert time.monotonic() - started < 5


# --- the board cookie survives a click from another site ---------------------


async def test_board_cookie_is_samesite_lax(http_server):
    """Strict made the first click on a link from a chat or mail client land
    on a 401: the redirect chain started cross-site, so the browser withheld
    the cookie it had just been given. The board is GET-only and read-only,
    so Lax gives up nothing."""
    await make_channel(http_server)
    _, redeemed = open_board(http_server, await link_for(http_server, ADMIN_TOKEN, "proj"))
    flags = [f.strip() for f in redeemed.headers["set-cookie"].split(";")]
    assert "SameSite=Lax" in flags
    assert "HttpOnly" in flags
    assert "Path=/board" in flags


# --- a port wildcard admits only a port --------------------------------------


def test_port_wildcard_admits_only_digits():
    allowed = ["127.0.0.1:*", "channel.test"]
    assert channel_http._matches("127.0.0.1:8765", allowed)
    assert channel_http._matches("channel.test", allowed)
    for bad in (
        "127.0.0.1:evil",
        "127.0.0.1:",
        "127.0.0.1:80.evil.test",
        "127.0.0.1:80@evil.test",
        "127.0.0.1:\uff18\uff10",  # fullwidth digits
        "channel.test:443",
    ):
        assert not channel_http._matches(bad, allowed), bad
    origins = channel_http._allowed_origins(["localhost:*"])
    assert channel_http._matches("http://localhost:3000", origins)
    assert not channel_http._matches("http://localhost:3000.evil.test", origins)


async def test_a_host_whose_port_is_not_a_port_is_refused(tmp_path, monkeypatch):
    with running_server(tmp_path, monkeypatch, AI_AGENT_CHANNEL_ALLOWED_HOSTS="127.0.0.1:*") as url:
        created = await make_channel(url)
        backend = {"Authorization": f"Bearer {created['tokens']['backend']}"}
        assert httpx.get(url + "/status", headers=backend).status_code == 200
        spoofed = {**backend, "Host": "127.0.0.1:evil.test"}
        assert httpx.get(url + "/status", headers=spoofed).status_code == 421


# --- Origin is checked on /board and /status, not only on /mcp ---------------


async def test_a_foreign_origin_is_refused_on_board_and_status(tmp_path, monkeypatch):
    with running_server(tmp_path, monkeypatch, AI_AGENT_CHANNEL_ALLOWED_HOSTS="127.0.0.1:*") as url:
        created = await make_channel(url)
        link = await link_for(url, ADMIN_TOKEN, "proj")
        backend = {"Authorization": f"Bearer {created['tokens']['backend']}"}
        evil = {"Origin": "https://evil.test"}
        assert httpx.get(url + "/status", headers={**backend, **evil}).status_code == 403
        assert httpx.get(url + "/status?wait=1", headers={**backend, **evil}).status_code == 403
        assert httpx.get(url + "/board/proj", headers=evil).status_code == 403
        refused = httpx.get(url + link["url"], headers=evil, follow_redirects=False)
        assert refused.status_code == 403
        # the refusal did not burn the link: the same-origin click still works
        same = {"Origin": url}
        opened = httpx.get(url + link["url"], headers=same, follow_redirects=False)
        assert opened.status_code == 303
        assert httpx.get(url + "/status", headers={**backend, **same}).status_code == 200


# --- forwarded headers with no usable peer address ---------------------------


def test_forwarded_proto_without_a_usable_peer_address_is_not_trusted(monkeypatch):
    monkeypatch.delenv(channel_http.TRUST_PROXY_ENV, raising=False)
    forwarded = [(b"x-forwarded-proto", b"https")]
    assert channel_http._is_secure({"headers": forwarded}) is False
    assert channel_http._is_secure({"client": None, "headers": forwarded}) is False
    # a unix socket or an unparseable address is not a known proxy
    assert channel_http._is_secure({"client": ("", 0), "headers": forwarded}) is False
    assert channel_http._is_secure({"client": ("proxy.local", 1), "headers": forwarded}) is False


# --- a link whose key died before the click ----------------------------------


def test_a_link_whose_key_died_before_the_click_opens_nothing(registry):
    with auth.open_admin_db() as conn:
        auth.create_channel(conn, name="proj", roles=["a", "b"])
        revoked = auth.create_view_token(conn, channel="proj", issued_by_role="a")
        revoked_link = auth.mint_board_nonce(conn, view_token=revoked["token"])
        expired = auth.create_view_token(conn, channel="proj")
        expired_link = auth.mint_board_nonce(conn, view_token=expired["token"])
        auth.revoke_view_tokens(conn, channel="proj", issued_by_role="a")
        conn.execute(
            "UPDATE tokens SET expires_at = '2000-01-01T00:00:00.000Z' WHERE token_hash = ?",
            (auth._hash_token(expired["token"]),),
        )
        assert auth.redeem_board_nonce(conn, nonce=revoked_link) is None
        assert auth.redeem_board_nonce(conn, nonce=expired_link) is None
        assert conn.execute("SELECT count(*) FROM board_sessions").fetchone()[0] == 0


def test_an_empty_board_cookie_is_nobody(registry):
    assert auth.viewer_from_session("") is None
