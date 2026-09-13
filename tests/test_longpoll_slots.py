"""Held /status long polls: a client that hangs up gives its slot back at
once, and one token cannot take every slot on the server."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from urllib.parse import urlsplit

import httpx

from ai_agent_channel import http as channel_http
from helpers import make_channel


def _hold_raw(base_url: str, token: str, wait: int = 30) -> socket.socket:
    """Start a long poll on a raw socket, so the test decides exactly when
    the connection goes away."""
    parts = urlsplit(base_url)
    assert parts.hostname is not None and parts.port is not None
    sock = socket.create_connection((parts.hostname, parts.port), timeout=5)
    sock.sendall(
        (
            f"GET /status?wait={wait} HTTP/1.1\r\nHost: {parts.netloc}\r\n"
            f"Authorization: Bearer {token}\r\n\r\n"
        ).encode()
    )
    return sock


async def _first_non_429(
    http: httpx.AsyncClient, url: str, headers: dict[str, str], timeout: float
) -> int:
    deadline = time.monotonic() + timeout
    while True:
        resp = await http.get(url + "/status?wait=1", headers=headers)
        if resp.status_code != 429 or time.monotonic() > deadline:
            return resp.status_code
        await asyncio.sleep(0.05)


def _watch_reads(monkeypatch) -> threading.Event:
    """Set once a held request has read the channel, i.e. holds its slot."""
    holding = threading.Event()
    real_summary = channel_http.db.channel_summary

    def summary(*args, **kwargs):
        holding.set()
        return real_summary(*args, **kwargs)

    monkeypatch.setattr(channel_http.db, "channel_summary", summary)
    return holding


async def test_a_client_that_hangs_up_frees_its_slot_at_once(http_server, monkeypatch):
    created = await make_channel(http_server)
    monkeypatch.setattr(channel_http, "STATUS_MAX_WAITERS", 1)
    monkeypatch.setattr(channel_http, "STATUS_POLL_INTERVAL_S", 0.05)
    holding = _watch_reads(monkeypatch)
    token = created["tokens"]["backend"]
    headers = {"Authorization": f"Bearer {token}"}

    sock = await asyncio.to_thread(_hold_raw, http_server, token)
    try:
        assert await asyncio.to_thread(holding.wait, 5)
        async with httpx.AsyncClient(timeout=30) as http:
            full = await http.get(http_server + "/status?wait=1", headers=headers)
        assert full.status_code == 429  # the slot really is taken
    finally:
        sock.close()

    # The hung-up request asked for 30 seconds. Its slot must come back long
    # before that — within a poll interval or two, not at the window's end.
    began = time.monotonic()
    async with httpx.AsyncClient(timeout=30) as http:
        status = await _first_non_429(http, http_server, headers, timeout=5)
    assert status == 200, "the slot of a disconnected client was not released"
    assert time.monotonic() - began < 5


async def test_one_token_cannot_take_every_slot(http_server, monkeypatch):
    created = await make_channel(http_server)
    monkeypatch.setattr(channel_http, "STATUS_MAX_WAITERS", 10)
    monkeypatch.setattr(channel_http, "STATUS_MAX_WAITERS_PER_TOKEN", 2)
    # long enough that a held request reads the channel exactly once, when it
    # takes its slot — so each read below marks one more occupied slot
    monkeypatch.setattr(channel_http, "STATUS_POLL_INTERVAL_S", 10)
    holding = _watch_reads(monkeypatch)
    token = created["tokens"]["backend"]
    backend = {"Authorization": f"Bearer {token}"}
    frontend = {"Authorization": f"Bearer {created['tokens']['frontend']}"}

    sockets = []
    try:
        for _ in range(2):  # one at a time, so neither can lose the race to the other
            holding.clear()
            sockets.append(await asyncio.to_thread(_hold_raw, http_server, token))
            assert await asyncio.to_thread(holding.wait, 5)
        async with httpx.AsyncClient(timeout=30) as http:
            # the same token, a third time: refused although the server has room
            third = await http.get(http_server + "/status?wait=1", headers=backend)
            assert third.status_code == 429
            assert third.headers["retry-after"]
            # another token is still served
            other = await http.get(http_server + "/status?wait=1", headers=frontend)
            assert other.status_code == 200
            # and an immediate answer holds nothing, so it is never refused
            assert (await http.get(http_server + "/status", headers=backend)).status_code == 200
    finally:
        for sock in sockets:
            sock.close()


def test_the_per_token_cap_defaults_to_eight():
    assert channel_http.STATUS_MAX_WAITERS_PER_TOKEN_ENV == (
        "AI_AGENT_CHANNEL_STATUS_MAX_WAITERS_PER_TOKEN"
    )
    assert channel_http._env_int("AI_AGENT_CHANNEL_TEST_UNSET_VARIABLE", 8) == 8
