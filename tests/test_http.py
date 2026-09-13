"""Integration tests for the HTTP transport: a real uvicorn server on a
random port, real MCP clients over streamable HTTP, bearer-token identity."""

from __future__ import annotations

import io
import json
import sys
import time

import httpx
import pytest
import uvicorn

from ai_agent_channel import auth, hooks
from helpers import ADMIN_TOKEN, client, error_text, make_channel, payload


def test_healthz_open_everything_else_locked(http_server):
    assert httpx.get(http_server + "/healthz").status_code == 200
    assert httpx.post(http_server + "/mcp", json={}).status_code == 401
    bad = {"Authorization": "Bearer cct_wrong"}
    assert httpx.post(http_server + "/mcp", json={}, headers=bad).status_code == 401
    assert httpx.get(http_server + "/hook-status").status_code == 401


async def test_admin_provisions_roles_chat(http_server):
    created = await make_channel(http_server)
    tokens = created["tokens"]

    async with client(http_server, ADMIN_TOKEN) as admin:
        channels = payload(await admin.call_tool("list_channels", {}))
        assert [c["name"] for c in channels] == ["proj"]

    async with client(http_server, tokens["frontend"]) as fe:
        sent = payload(
            await fe.call_tool(
                "send_message",
                {"to": "backend", "topic": "api", "body": "need /users endpoint"},
            )
        )
    async with client(http_server, tokens["backend"]) as be:
        inbox = payload(await be.call_tool("read_inbox", {}))
        assert [m["id"] for m in inbox] == [sent["id"]]
        assert inbox[0]["from"] == "frontend"
        status = payload(await be.call_tool("channel_status", {}))
        assert status["counts"]["unread"] == 1


async def test_channels_are_isolated(http_server):
    a = await make_channel(http_server, "proj-a")
    b = await make_channel(http_server, "proj-b")
    async with client(http_server, a["tokens"]["frontend"]) as fe_a:
        payload(
            await fe_a.call_tool(
                "send_message", {"to": "backend", "topic": "secret", "body": "of proj-a"}
            )
        )
    async with client(http_server, b["tokens"]["backend"]) as be_b:
        assert payload(await be_b.call_tool("read_inbox", {})) == []
        assert payload(await be_b.call_tool("list_messages", {})) == []


async def test_role_token_cannot_manage(http_server):
    created = await make_channel(http_server)
    async with client(http_server, created["tokens"]["frontend"]) as fe:
        text = error_text(await fe.call_tool("create_channel", {"name": "x", "roles": ["a", "b"]}))
        assert "admin token" in text
        assert "admin token" in error_text(await fe.call_tool("list_channels", {}))
        for name, args in (
            ("add_role", {"channel": "proj", "role": "intruder"}),
            ("delete_channel", {"name": "proj"}),
            ("rotate_token", {"channel": "proj", "role": "backend"}),
            ("revoke_board_access", {"channel": "proj"}),
        ):
            assert "admin token" in error_text(await fe.call_tool(name, args)), name
    # and nothing changed: the channel is intact with its two roles
    backend = {"Authorization": f"Bearer {created['tokens']['backend']}"}
    assert httpx.get(http_server + "/status", headers=backend).status_code == 200
    with auth.open_admin_db() as conn:
        assert auth.list_channels(conn)[0]["roles"] == ["frontend", "backend"]


async def test_admin_has_no_mailbox(http_server):
    await make_channel(http_server)
    async with client(http_server, ADMIN_TOKEN) as admin:
        text = error_text(
            await admin.call_tool("send_message", {"to": "backend", "topic": "t", "body": "b"})
        )
        assert "no mailbox role" in text
        assert "no mailbox role" in error_text(await admin.call_tool("channel_status", {}))


async def test_send_outside_channel_roles_rejected(http_server):
    created = await make_channel(http_server)
    async with client(http_server, created["tokens"]["frontend"]) as fe:
        text = error_text(
            await fe.call_tool("send_message", {"to": "designer", "topic": "t", "body": "b"})
        )
        assert "cannot address 'designer'" in text


async def test_rotate_token_locks_out_old_client(http_server):
    created = await make_channel(http_server)
    async with client(http_server, ADMIN_TOKEN) as admin:
        rotated = payload(
            await admin.call_tool("rotate_token", {"channel": "proj", "role": "frontend"})
        )
    old = {"Authorization": f"Bearer {created['tokens']['frontend']}"}
    assert httpx.post(http_server + "/mcp", json={}, headers=old).status_code == 401
    async with client(http_server, rotated["token"]) as fe:
        payload(await fe.call_tool("channel_status", {}))


async def test_hook_status_endpoint(http_server):
    created = await make_channel(http_server)
    async with client(http_server, created["tokens"]["frontend"]) as fe:
        payload(
            await fe.call_tool(
                "send_message",
                {"to": "backend", "topic": "do it", "body": "x", "action_required": True},
            )
        )
    resp = httpx.get(
        http_server + "/hook-status",
        headers={"Authorization": f"Bearer {created['tokens']['backend']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert (data["channel"], data["role"]) == ("proj", "backend")
    assert data["counts"]["unread"] == 1
    assert data["counts"]["open_obligations_untaken"] == 1
    admin = httpx.get(
        http_server + "/hook-status", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    )
    assert admin.status_code == 403


def _run_stop_hook(monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    hooks.stop_hook()
    return capsys.readouterr().out


async def test_remote_stop_hook_blocks_on_pending(http_server, monkeypatch, capsys, tmp_path):
    created = await make_channel(http_server)
    async with client(http_server, created["tokens"]["frontend"]) as fe:
        payload(
            await fe.call_tool(
                "send_message",
                {"to": "backend", "topic": "debt", "body": "x", "action_required": True},
            )
        )
    monkeypatch.setenv("HOME", str(tmp_path))  # keep the snapshot hermetic
    monkeypatch.setenv(hooks.URL_ENV, http_server + "/mcp")  # /mcp suffix accepted
    monkeypatch.setenv(hooks.TOKEN_ENV, created["tokens"]["backend"])
    out = _run_stop_hook(monkeypatch, capsys)
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "'backend'" in decision["reason"]
    assert "unread=1" in decision["reason"]


async def test_remote_stop_hook_silent_when_clean(http_server, monkeypatch, capsys, tmp_path):
    created = await make_channel(http_server)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(hooks.URL_ENV, http_server)
    monkeypatch.setenv(hooks.TOKEN_ENV, created["tokens"]["backend"])
    assert _run_stop_hook(monkeypatch, capsys) == ""


def test_remote_stop_hook_fails_open_when_unreachable(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(hooks.URL_ENV, "http://127.0.0.1:1")  # nothing listens
    monkeypatch.setenv(hooks.TOKEN_ENV, "cct_whatever")
    assert _run_stop_hook(monkeypatch, capsys) == ""


async def test_three_role_channel_end_to_end(http_server):
    async with client(http_server, ADMIN_TOKEN) as admin:
        created = payload(
            await admin.call_tool("create_channel", {"name": "trio", "roles": ["dev", "qa", "ops"]})
        )
    assert set(created["tokens"]) == {"dev", "qa", "ops"}
    async with client(http_server, created["tokens"]["dev"]) as dev:
        payload(
            await dev.call_tool("send_message", {"to": "qa", "topic": "build", "body": "ready"})
        )
        payload(
            await dev.call_tool("send_message", {"to": "ops", "topic": "deploy", "body": "please"})
        )
        text = error_text(
            await dev.call_tool("send_message", {"to": "designer", "topic": "t", "body": "b"})
        )
        assert "cannot address 'designer'" in text
    async with client(http_server, created["tokens"]["qa"]) as qa:
        inbox = payload(await qa.call_tool("read_inbox", {}))
        assert [m["topic"] for m in inbox] == ["build"]
        # transparency: qa sees the dev→ops message too
        all_msgs = payload(await qa.call_tool("list_messages", {}))
        assert {m["to"] for m in all_msgs} == {"qa", "ops"}


async def test_status_endpoint_and_console_command(http_server, monkeypatch, capsys):
    """Condition 3 from the request: the console command must speak the same
    transport as the mailbox, so a hosted channel is readable from a plain
    shell with no MCP session."""
    from ai_agent_channel import cli

    created = await make_channel(http_server, "cli-demo")
    async with client(http_server, created["tokens"]["frontend"]) as fe:
        payload(
            await fe.call_tool(
                "send_message",
                {"to": "backend", "topic": "fix login", "body": "x", "action_required": True},
            )
        )
        payload(
            await fe.call_tool(
                "pin_set",
                {"key": "notes", "title": "N", "body": "body", "version": "v1"},
            )
        )

    monkeypatch.setenv(cli.URL_ENV, http_server + "/mcp")  # /mcp suffix accepted
    monkeypatch.setenv(cli.TOKEN_ENV, created["tokens"]["backend"])
    monkeypatch.delenv(cli.ROLE_ENV, raising=False)

    code = cli.run(["status"])
    state = json.loads(capsys.readouterr().out)
    assert code == 1  # backend owes something
    assert (state["channel"], state["role"]) == ("cli-demo", "backend")
    assert [d["topic"] for d in state["debts"]] == ["fix login"]

    assert cli.run(["pins"]) == 0
    pins = json.loads(capsys.readouterr().out)
    assert len(pins[0]["body_sha256"]) == 64
    assert "body" not in pins[0]

    # the admin token has no mailbox, so it cannot answer "what do I owe"
    resp = httpx.get(http_server + "/status", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    assert resp.status_code == 403


async def test_status_long_poll_holds_until_something_arrives(http_server, monkeypatch):
    """The held request is what makes an external watcher near-instant: it
    must actually block, and it must be released by an arrival."""
    import asyncio

    from ai_agent_channel import http as channel_http

    # the hold is measured in whole seconds, the re-check need not be
    monkeypatch.setattr(channel_http, "STATUS_POLL_INTERVAL_S", 0.05)
    created = await make_channel(http_server, "longpoll")
    backend = {"Authorization": f"Bearer {created['tokens']['backend']}"}

    async with httpx.AsyncClient(timeout=30) as http:
        # nothing pending: the request is held for the full window
        began = time.monotonic()
        quiet = await http.get(http_server + "/status?wait=1", headers=backend)
        held = time.monotonic() - began
        assert quiet.status_code == 200
        assert quiet.json()["pending"] == {}
        assert held >= 0.9, f"returned after {held:.2f}s — it did not wait"

        # …and an arrival releases it early. An answer given before the
        # arrival would carry pending == {}, so the assertion below cannot
        # pass on a request that was never held.
        async def send_after_delay():
            await asyncio.sleep(0.2)
            async with client(http_server, created["tokens"]["frontend"]) as fe:
                payload(
                    await fe.call_tool(
                        "send_message",
                        {"to": "backend", "topic": "urgent", "body": "x"},
                    )
                )

        began = time.monotonic()
        sender = asyncio.create_task(send_after_delay())
        woken = await http.get(http_server + "/status?wait=30", headers=backend)
        elapsed = time.monotonic() - began
        await sender

        assert woken.json()["pending"] == {"unread": 1}
        assert elapsed < 10, f"took {elapsed:.2f}s — it did not return promptly"


async def test_status_without_wait_answers_immediately(http_server):
    created = await make_channel(http_server, "nowait")
    began = time.monotonic()
    resp = httpx.get(
        http_server + "/status",
        headers={"Authorization": f"Bearer {created['tokens']['backend']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["waited"] is False
    assert time.monotonic() - began < 2


async def test_status_wait_is_capped_below_proxy_timeouts(http_server, monkeypatch):
    """A caller asking for an hour must not get one: the hold has to end
    before the idle timeout of whatever proxy sits in front (nginx 60s by
    default, Cloudflare 100s), or the client gets someone else's gateway
    error instead of our empty answer.

    Exercised through the real handler with the cap shrunk to one second —
    a test that burns the full window to prove the window exists is a test
    nobody runs.
    """
    from ai_agent_channel import http as channel_http

    assert channel_http.STATUS_WAIT_CAP_S <= 50
    created = await make_channel(http_server, "capped")
    monkeypatch.setattr(channel_http, "STATUS_WAIT_CAP_S", 1)
    backend = {"Authorization": f"Bearer {created['tokens']['backend']}"}
    async with httpx.AsyncClient(timeout=30) as http:
        began = time.monotonic()
        resp = await http.get(http_server + "/status?wait=99999", headers=backend)
        elapsed = time.monotonic() - began
    assert resp.status_code == 200
    assert resp.json()["waited"] is True
    assert resp.json()["wait_s"] == 1
    assert 0.9 <= elapsed < 5, f"held {elapsed:.2f}s for a 1s cap"

    # a garbage value falls back to "do not wait" rather than to the cap
    assert channel_http._int_param({"query_string": b"wait=soon"}, "wait", 0) == 0
    assert channel_http._int_param({"query_string": b"wait=-5"}, "wait", 0) == 0


def test_serve_http_refuses_to_start_without_admin_token(tmp_path, monkeypatch):
    from ai_agent_channel import http as channel_http

    monkeypatch.setenv(auth.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(auth.ADMIN_TOKEN_ENV, raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("server started"))
    with pytest.raises(SystemExit, match=auth.ADMIN_TOKEN_ENV):
        channel_http.serve_http()


def test_serve_http_refuses_an_unusable_data_dir(tmp_path, monkeypatch):
    from ai_agent_channel import http as channel_http

    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv(auth.DATA_DIR_ENV, str(blocker / "data"))
    monkeypatch.setenv(auth.ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("server started"))
    with pytest.raises(SystemExit, match="not usable"):
        channel_http.serve_http()
