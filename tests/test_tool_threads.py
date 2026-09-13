"""Tool bodies run off the event loop, with the caller's identity, and the
registry keeps the schema FastMCP builds from them."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import threading
import time

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool

from ai_agent_channel import auth, server
from ai_agent_channel.tools import CATALOGUE, registry
from helpers import client, make_channel, payload, registered_tool


async def test_a_write_waiting_on_one_channel_does_not_freeze_another(http_server):
    a = await make_channel(http_server, "chan-a")
    b = await make_channel(http_server, "chan-b")
    async with (
        client(http_server, a["tokens"]["frontend"]) as fe_a,
        client(http_server, b["tokens"]["frontend"]) as fe_b,
    ):
        payload(await fe_a.call_tool("list_roles", {}))  # channel A's DB exists
        payload(await fe_b.call_tool("list_roles", {}))
        locked, release = threading.Event(), threading.Event()

        def hold_write_lock() -> None:
            conn = sqlite3.connect(str(auth.channel_db_path("chan-a")), isolation_level=None)
            conn.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(timeout=10)
            conn.execute("COMMIT")
            conn.close()

        holder = threading.Thread(target=hold_write_lock, daemon=True)
        holder.start()
        assert locked.wait(timeout=5)
        try:
            send = asyncio.create_task(
                fe_a.call_tool("send_message", {"to": "backend", "topic": "t", "body": "b"})
            )
            await asyncio.sleep(0.3)  # the send is now waiting on the lock
            started = time.monotonic()
            roles = payload(await fe_b.call_tool("list_roles", {}))
            elapsed = time.monotonic() - started
            assert roles["you"] == "frontend"
            assert elapsed < 0.5, f"another channel waited {elapsed:.2f}s behind a lock"
            assert not send.done()
        finally:
            release.set()
        sent = await send
        assert not sent.isError
        holder.join(timeout=5)


async def test_concurrent_callers_keep_their_own_identity(http_server):
    tokens = (await make_channel(http_server))["tokens"]
    async with (
        client(http_server, tokens["frontend"]) as fe,
        client(http_server, tokens["backend"]) as be,
    ):
        calls = [(role, s) for _ in range(20) for role, s in (("frontend", fe), ("backend", be))]
        answers = await asyncio.gather(*(s.call_tool("list_roles", {}) for _, s in calls))
        assert [payload(r)["you"] for r in answers] == [role for role, _ in calls]


async def test_run_in_thread_carries_the_callers_context():
    ident = auth.Identity(is_admin=False, channel="c", role="alpha", peers=("beta",))
    token = auth.CURRENT_IDENTITY.set(ident)
    try:
        seen = await registry.run_in_thread(
            lambda: (auth.CURRENT_IDENTITY.get(), threading.current_thread())
        )
    finally:
        auth.CURRENT_IDENTITY.reset(token)
    assert seen[0] is ident
    assert seen[1] is not threading.current_thread()


async def test_registered_tools_are_async_and_keep_their_schema():
    listed = {t.name: t for t in await server.mcp.list_tools()}
    for fn in CATALOGUE:
        # is_async is not on the wire listing, only on the server-side Tool
        assert registered_tool(server.mcp, fn.__name__).is_async, fn.__name__
        plain = Tool.from_function(fn)
        assert listed[fn.__name__].inputSchema == plain.parameters, fn.__name__
        assert listed[fn.__name__].outputSchema == plain.output_schema, fn.__name__
    # the module attribute is still the plain function tests call directly
    assert not inspect.iscoroutinefunction(server.send_message)
    assert isinstance(server.channel_status.__call__, object)


def test_registry_names_what_is_out_of_step(monkeypatch):
    monkeypatch.setattr(registry, "_declared", {})

    @registry.tool(description="x")
    def declared_only() -> None: ...

    def never_declared() -> None: ...

    with pytest.raises(RuntimeError) as err:
        registry.register(FastMCP("t"), [never_declared, never_declared])
    text = str(err.value)
    assert "declared_only" in text and "never_declared" in text
    assert "listed more than once" in text

    with pytest.raises(RuntimeError, match="declared_only"):
        registry.tool(description="again")(declared_only)
