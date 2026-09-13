"""Shared test helpers (plain functions; fixtures live in conftest.py).

Importable as ``helpers`` because pyproject puts tests/ on pytest's
``pythonpath``. Test modules must import from here, never from each other:
under ``--import-mode=importlib`` a test module is not importable by name.

`L` unwraps the listing envelope. Listings return {"result": [...]} plus a
'truncated' key when the answer is a window rather than the whole set, so
tests that care about the rows say so explicitly and the ones that care about
truncation read the other key.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, TypeVar, cast

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.types import CallToolResult, TextContent

from ai_agent_channel import auth

ADMIN_TOKEN = "test-admin-token"

_T = TypeVar("_T")


def L(response: object) -> list[dict[str, Any]]:
    """The rows of a listing response."""
    if isinstance(response, dict) and "result" in response:
        return cast("list[dict[str, Any]]", response["result"])
    assert isinstance(response, list), f"not a listing: {response!r}"
    return cast("list[dict[str, Any]]", response)


def present(value: _T | None) -> _T:
    """`value`, asserted to exist: for lookups (pin_get, fetch_message...)
    that return None for "not found" where the test knows it is there."""
    assert value is not None
    return value


def authenticated(token: str) -> auth.Identity:
    """The identity a token resolves to; fails the test if it resolves to none."""
    ident = auth.authenticate(token)
    assert ident is not None, "the token did not authenticate"
    return ident


# --- the few places tests reach into mcp's private state ----------------------
# Each access is made here and nowhere else, and each asserts the attribute it
# relies on still exists: an mcp upgrade that renames it fails these helpers
# loudly instead of silently turning the reset or the lookup into a no-op.


def reset_session_manager(mcp: FastMCP) -> None:
    """Forget the app's streamable-HTTP session manager.

    The manager is single-run and FastMCP creates it once, lazily, in
    streamable_http_app(); mcp 1.x has no public way to reset it, so a test
    that builds a fresh app per server clears the private attribute."""
    assert hasattr(mcp, "_session_manager"), "mcp no longer has FastMCP._session_manager"
    mcp._session_manager = None  # pyright: ignore[reportPrivateUsage]


def registered_tool(mcp: FastMCP, name: str) -> Tool:
    """The server-side Tool object (is_async, fn...). ``await mcp.list_tools()``
    is the public listing but carries only the wire schema."""
    manager = getattr(mcp, "_tool_manager", None)
    assert manager is not None, "mcp no longer has FastMCP._tool_manager"
    tool = manager.get_tool(name)
    assert tool is not None, f"no tool named {name!r}"
    return tool


# --- a real uvicorn and MCP clients over streamable HTTP ----------------------


@contextmanager
def running_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[str]:
    """A real uvicorn on a random port; ``env`` is applied before the app is
    built, since build_app reads its settings then."""
    monkeypatch.setenv(auth.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(auth.ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    monkeypatch.delenv("AI_AGENT_CHANNEL_ROLE", raising=False)
    monkeypatch.delenv("AI_AGENT_CHANNEL_DB", raising=False)
    monkeypatch.delenv("AI_AGENT_CHANNEL_ALLOWED_HOSTS", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    from ai_agent_channel.http import build_app
    from ai_agent_channel.server import mcp

    reset_session_manager(mcp)
    config = uvicorn.Config(build_app(), host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@asynccontextmanager
async def client(base_url: str, token: str) -> AsyncIterator[ClientSession]:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx.AsyncClient(headers=headers, timeout=30) as http_client,
        streamable_http_client(base_url + "/mcp", http_client=http_client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent), f"expected text content, got {block!r}"
    return block.text


def payload(result: CallToolResult) -> Any:
    """A tool call's answer, decoded. Typed Any on purpose: it is the JSON a
    tool returned, and each test reads the shape it asked for."""
    assert not result.isError, _text(result)
    if result.structuredContent is not None:
        sc = result.structuredContent
        return sc["result"] if set(sc) == {"result"} else sc
    return json.loads(_text(result))


def payload_dict(result: CallToolResult) -> dict[str, Any]:
    """`payload` for a tool that answers with an object."""
    value = payload(result)
    assert isinstance(value, dict), f"expected an object, got {value!r}"
    return cast("dict[str, Any]", value)


def error_text(result: CallToolResult) -> str:
    assert result.isError
    return _text(result)


async def make_channel(base_url: str, name: str = "proj") -> dict[str, Any]:
    async with client(base_url, ADMIN_TOKEN) as admin:
        result = await admin.call_tool(
            "create_channel", {"name": name, "roles": ["frontend", "backend"]}
        )
        return payload_dict(result)


# --- a second process -----------------------------------------------------------


def concurrent_writer(db_file: str, role: str, target: str, count: int) -> None:
    """Child-process body for test_concurrent. It lives here, not in the test
    module, because a spawned child re-imports it by module name. The
    environment it sets is the child's own."""
    os.environ["AI_AGENT_CHANNEL_DB"] = db_file
    os.environ["AI_AGENT_CHANNEL_ROLE"] = role
    from ai_agent_channel import server

    for i in range(count):
        server.send_message(to=target, topic=f"t{i:03d}-from-{role}", body=f"body {i} from {role}")
