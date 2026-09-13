"""Registry and mailbox reads stay off the event loop, and the registry
schema is migrated once — not on every request."""

from __future__ import annotations

import asyncio
import threading
import time

import httpx

from ai_agent_channel import auth


async def test_slow_registry_lookups_do_not_stall_healthz(http_server, monkeypatch):
    """A burst of bogus tokens used to run its SQLite lookups on the event
    loop, one after another, while /healthz and every held long poll waited
    behind them. The lookup is made artificially slow so the difference is
    seconds, not milliseconds — no timing luck involved."""
    entered = threading.Event()
    real = auth.authenticate

    def slow(token):
        entered.set()
        time.sleep(0.5)
        return real(token)

    monkeypatch.setattr(auth, "authenticate", slow)
    bogus = {"Authorization": "Bearer cct_bogus"}
    async with httpx.AsyncClient(timeout=60) as http:
        flood = [
            asyncio.create_task(http.get(http_server + "/status", headers=bogus)) for _ in range(20)
        ]
        assert await asyncio.to_thread(entered.wait, 10)
        began = time.monotonic()
        health = await http.get(http_server + "/healthz")
        latency = time.monotonic() - began
        answers = await asyncio.gather(*flood)
    assert health.status_code == 200
    assert [a.status_code for a in answers] == [401] * len(answers), [a.text for a in answers]
    # blocked, the loop answers after the queued lookups: about 10 seconds
    assert latency < 2.5, f"/healthz took {latency:.2f}s behind blocking lookups"


async def test_the_registry_schema_is_not_run_per_request(http_server, monkeypatch):
    bogus = {"Authorization": "Bearer cct_bogus"}
    # whatever migration there is happens before or on the first request
    assert httpx.get(http_server + "/status", headers=bogus).status_code == 401

    statements: list[str] = []
    real_connect = auth._connect

    def traced(path):
        conn = real_connect(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(auth, "_connect", traced)
    async with httpx.AsyncClient(timeout=30) as http:
        answers = await asyncio.gather(
            *(http.get(http_server + "/status", headers=bogus) for _ in range(30))
        )
    assert [a.status_code for a in answers] == [401] * len(answers), [a.text for a in answers]
    assert any("FROM tokens" in s for s in statements), "the lookups were not traced"
    ddl = [s for s in statements if "CREATE " in s.upper() or "ALTER " in s.upper()]
    assert ddl == [], f"schema statements ran on the request path: {ddl[:3]}"
