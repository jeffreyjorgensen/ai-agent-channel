from __future__ import annotations

import io
import json
import os
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from ai_agent_channel import client, hooks, server

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    # a developer's exported hosted-channel settings must not leak in
    for name in (hooks.URL_ENV, hooks.TOKEN_ENV, hooks.TOKEN_FILE_ENV):
        monkeypatch.delenv(name, raising=False)


def _stdin(monkeypatch, payload) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))


def test_session_start_hook_prints_bootstrap(capsys):
    hooks.session_start_hook()
    out = capsys.readouterr().out
    assert "channel_status" in out
    assert "unblocked" in out
    assert "resolved_for_you" in out


def test_stop_hook_blocks_first_attempt_without_role(capsys, monkeypatch):
    # role unknown → the channel cannot be checked → blanket reminder
    monkeypatch.delenv("AI_AGENT_CHANNEL_ROLE", raising=False)
    _stdin(monkeypatch, {"stop_hook_active": False})
    hooks.stop_hook()
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "channel_status" in out["reason"]


def test_stop_hook_allows_second_attempt(capsys, monkeypatch):
    monkeypatch.delenv("AI_AGENT_CHANNEL_ROLE", raising=False)
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""  # no block — stop proceeds


def test_stop_hook_survives_garbage_stdin(capsys, monkeypatch):
    monkeypatch.delenv("AI_AGENT_CHANNEL_ROLE", raising=False)
    _stdin(monkeypatch, "not json")
    hooks.stop_hook()
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"  # fail-safe: remind rather than crash


@pytest.mark.parametrize("payload", [[1, 2], None, '"a string"'])
def test_stop_hook_survives_non_object_stdin(payload, capsys, monkeypatch):
    monkeypatch.delenv("AI_AGENT_CHANNEL_ROLE", raising=False)
    _stdin(monkeypatch, payload)
    hooks.stop_hook()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["decision"] == "block"
    assert captured.err == ""


def test_stop_hook_silent_when_channel_empty(db_path: Path, as_role, capsys, monkeypatch):
    # role known + nothing actionable → no block, no tokens wasted
    as_role("backend")
    _stdin(monkeypatch, {"stop_hook_active": False})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""


def test_stop_hook_blocks_with_pending_and_names_counts(
    db_path: Path, as_role, capsys, monkeypatch
):
    as_role("frontend")
    server.send_message(to="backend", topic="t", body="x", action_required=True)
    as_role("backend")
    _stdin(monkeypatch, {"stop_hook_active": False})
    hooks.stop_hook()
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "unread=1" in out["reason"]
    assert "open_obligations_untaken=1" in out["reason"]


def _assert_stop(monkeypatch, capsys, *, blocks: bool, mentions: str | None = None):
    _stdin(monkeypatch, {"stop_hook_active": False})
    hooks.stop_hook()
    out = capsys.readouterr().out
    if not blocks:
        assert out == ""
    else:
        decision = json.loads(out)
        assert decision["decision"] == "block"
        if mentions:
            assert mentions in decision["reason"]


def test_stop_hook_obligation_nags_whoever_holds_the_ball(
    db_path: Path, as_role, capsys, monkeypatch
):
    # the ball, not the status whitelist, decides who gets nagged
    as_role("frontend")
    sent = server.send_message(to="backend", topic="big task", body="x", action_required=True)
    as_role("backend")
    server.mark_read(message_id=sent["id"])
    # untouched debt → nags the addressee
    _assert_stop(monkeypatch, capsys, blocks=True, mentions="open_obligations_untaken=1")
    # taken into work → silent for the executor while the work runs
    server.set_work_status(message_id=sent["id"], work_status="in_progress")
    _assert_stop(monkeypatch, capsys, blocks=False)
    # executor throws a question back (needs_you) → ball at the author:
    # the executor is NOT nagged...
    server.set_work_status(message_id=sent["id"], work_status="needs_you")
    _assert_stop(monkeypatch, capsys, blocks=False)
    # ...the author is (via their needs_you list)
    as_role("frontend")
    _assert_stop(monkeypatch, capsys, blocks=True, mentions="needs_you=1")
    # author throws the ball back → the executor is nagged again
    server.set_work_status(message_id=sent["id"], work_status="needs_you")
    _assert_stop(monkeypatch, capsys, blocks=False)
    as_role("backend")
    _assert_stop(monkeypatch, capsys, blocks=True, mentions="open_obligations_untaken=1")
    # declaring done_local also hands the ball over → executor silent
    server.set_work_status(message_id=sent["id"], work_status="done_local")
    _assert_stop(monkeypatch, capsys, blocks=False)


def test_stop_hook_author_can_poke_a_taken_debt(db_path: Path, as_role, capsys, monkeypatch):
    # the actor rule beats the status whitelist: the author re-setting
    # in_progress with their own hand is a transition (other role), the
    # last move is now the author's → the debt nags the executor again —
    # a deliberate, audited "poke" for a stalled task
    as_role("frontend")
    sent = server.send_message(to="backend", topic="stalled", body="x", action_required=True)
    as_role("backend")
    server.mark_read(message_id=sent["id"])
    server.set_work_status(message_id=sent["id"], work_status="in_progress")
    _assert_stop(monkeypatch, capsys, blocks=False)  # taken, silent

    as_role("frontend")
    poke = server.set_work_status(message_id=sent["id"], work_status="in_progress")
    assert poke["already_set"] is False  # other role → real audited transition

    as_role("backend")
    _assert_stop(monkeypatch, capsys, blocks=True, mentions="open_obligations_untaken=1")
    # the executor re-takes it (same value, same role, after the poke —
    # last same-value setter is now the author, so this records again)
    retake = server.set_work_status(message_id=sent["id"], work_status="in_progress")
    assert retake["already_set"] is False
    _assert_stop(monkeypatch, capsys, blocks=False)


def test_stop_hook_reopen_resets_taken(db_path: Path, as_role, capsys, monkeypatch):
    # axes are independent: after a reopen the debt still carries
    # work_status=in_progress — but "taken" must reset, or the executor's
    # stop-hook stays silent about work that came back
    as_role("frontend")
    sent = server.send_message(to="backend", topic="fix", body="x", action_required=True)
    as_role("backend")
    server.mark_read(message_id=sent["id"])
    server.set_work_status(message_id=sent["id"], work_status="in_progress")
    server.resolve_message(message_id=sent["id"], resolution_note="done")
    as_role("frontend")
    server.reopen_message(message_id=sent["id"], reason="null case fails")
    as_role("backend")
    _assert_stop(monkeypatch, capsys, blocks=True, mentions="open_obligations_untaken=1")
    # explicit re-take: same value, but AFTER a reopen it is a real audited
    # transition, not an idempotent no-op — and it silences the nag again
    res = server.set_work_status(message_id=sent["id"], work_status="in_progress")
    assert res["already_set"] is False
    _assert_stop(monkeypatch, capsys, blocks=False)
    # plain retry without an intervening reopen stays a no-op
    again = server.set_work_status(message_id=sent["id"], work_status="in_progress")
    assert again["already_set"] is True


def test_stop_hook_retry_passes_when_counts_unchanged(db_path: Path, as_role, capsys, monkeypatch):
    as_role("frontend")
    server.send_message(to="backend", topic="t", body="x")
    as_role("backend")
    # first attempt: blocks and snapshots the counts
    _stdin(monkeypatch, {"stop_hook_active": False})
    hooks.stop_hook()
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    # retry with the SAME counts: the agent looked and decided — let it stop
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""


def test_stop_hook_retry_reblocks_only_on_growth(db_path: Path, as_role, capsys, monkeypatch):
    as_role("frontend")
    server.send_message(to="backend", topic="t1", body="x")
    as_role("backend")
    _stdin(monkeypatch, {"stop_hook_active": False})
    hooks.stop_hook()
    capsys.readouterr()  # first block, snapshot unread=1

    # mail arrives in the race window between reminder and retry
    as_role("frontend")
    server.send_message(to="backend", topic="t2", body="x")
    as_role("backend")
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "new items arrived" in out["reason"]
    assert "unread=2" in out["reason"]

    # no further growth → the next retry passes
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""


def test_stop_hook_block_cap_prevents_livelock(db_path: Path, as_role, capsys, monkeypatch):
    def send_one(i: int) -> None:
        as_role("frontend")
        server.send_message(to="backend", topic=f"spam{i}", body="x")
        as_role("backend")

    send_one(0)
    _stdin(monkeypatch, {"stop_hook_active": False})
    hooks.stop_hook()  # block 1
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    for i in (1, 2):
        send_one(i)
        _stdin(monkeypatch, {"stop_hook_active": True})
        hooks.stop_hook()  # blocks 2 and 3 — counts keep growing
        assert json.loads(capsys.readouterr().out)["decision"] == "block"
    # growth continues, but the cap is reached — stop must pass
    send_one(3)
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""


def test_stop_hook_ignores_non_actionable_states(db_path: Path, as_role, capsys, monkeypatch):
    # in_progress and blocked-without-resolvable-blocker are legitimate
    # multi-turn states — they must not nag on every stop
    as_role("frontend")
    sent = server.send_message(to="backend", topic="t", body="x", action_required=True)
    as_role("backend")
    server.mark_read(message_id=sent["id"])
    server.resolve_message(message_id=sent["id"], resolution_note="done")
    as_role("frontend")
    server.confirm_resolution(message_id=sent["id"])
    server.set_work_status(message_id=sent["id"], work_status="in_progress")
    _stdin(monkeypatch, {"stop_hook_active": False})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""


# --- local mode: the edges that must not break a stop -----------------------


def test_stop_hook_retry_with_nothing_pending_clears_the_snapshot(
    db_path: Path, as_role, capsys, monkeypatch
):
    as_role("backend")
    snapshot = db_path.parent / ".stop-hook-backend.json"
    snapshot.write_text(json.dumps({"counts": {"unread": 1}, "blocks": 1}))
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""
    assert not snapshot.exists()


def test_stop_hook_falls_back_to_the_blanket_reminder_when_the_db_fails(
    as_role, capsys, monkeypatch
):
    monkeypatch.setenv("AI_AGENT_CHANNEL_DB", "/dev/null/x.db")
    as_role("backend")
    _stdin(monkeypatch, {"stop_hook_active": False})
    hooks.stop_hook()
    assert json.loads(capsys.readouterr().out)["reason"] == hooks.STOP_MESSAGE
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""  # cannot check → never loop


def test_stop_hook_survives_an_unusable_snapshot_file(db_path: Path, as_role, capsys, monkeypatch):
    as_role("frontend")
    sent = server.send_message(to="backend", topic="t", body="x")
    as_role("backend")
    # a directory where the snapshot file should be: unreadable,
    # unwritable and unremovable as a file
    (db_path.parent / ".stop-hook-backend.json").mkdir()
    _assert_stop(monkeypatch, capsys, blocks=True, mentions="unread=1")
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""
    server.mark_read(message_id=sent["id"])
    _assert_stop(monkeypatch, capsys, blocks=False)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("content", ["[1]", '{"counts": [1], "blocks": 1}', '{"blocks": "x"}'])
def test_stop_hook_ignores_a_corrupt_snapshot(content, db_path: Path, as_role, capsys, monkeypatch):
    as_role("frontend")
    server.send_message(to="backend", topic="t", body="x")
    as_role("backend")
    (db_path.parent / ".stop-hook-backend.json").write_text(content)
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_stop_hook_never_tracebacks(monkeypatch, capsys):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "remote_config", broken)
    _stdin(monkeypatch, {})
    hooks.stop_hook()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" in captured.err


# --- remote mode ------------------------------------------------------------


class _ChannelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = self.server.reply  # type: ignore[attr-defined]
        self.server.seen.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def channel_server(monkeypatch, tmp_path):
    httpd = HTTPServer(("127.0.0.1", 0), _ChannelHandler)
    httpd.reply = (200, {"role": "backend", "counts": {}})  # type: ignore[attr-defined]
    httpd.seen = []  # type: ignore[attr-defined]
    # shutdown() waits out one poll interval (0.5s by default) per test
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(hooks.ROLE_ENV, raising=False)
    monkeypatch.setenv(hooks.URL_ENV, f"http://127.0.0.1:{httpd.server_port}/mcp")
    monkeypatch.setenv(hooks.TOKEN_ENV, "cct_env")
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _token_file(tmp_path: Path, token: str, mode: int) -> Path:
    path = tmp_path / "role.token"
    path.write_text(token + "\n")
    path.chmod(mode)
    return path


def test_stop_hook_honours_the_token_file(channel_server, tmp_path, capsys, monkeypatch):
    channel_server.reply = (200, {"role": "backend", "counts": {"unread": 2}})
    monkeypatch.delenv(hooks.TOKEN_ENV)
    monkeypatch.setenv(hooks.TOKEN_FILE_ENV, str(_token_file(tmp_path, "cct_file", 0o600)))
    _assert_stop(monkeypatch, capsys, blocks=True, mentions="'backend': unread=2")
    assert channel_server.seen == [("/hook-status", "Bearer cct_file")]


def test_stop_hook_warns_about_an_unusable_token_file(
    channel_server, tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv(hooks.TOKEN_FILE_ENV, str(_token_file(tmp_path, "cct_file", 0o644)))
    _stdin(monkeypatch, {})
    hooks.stop_hook()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "readable by others" in captured.err
    assert channel_server.seen == []


@pytest.mark.parametrize("status", [401, 403, 404])
def test_stop_hook_warns_when_the_server_rejects_it(status, channel_server, capsys, monkeypatch):
    """A wrong token or URL must not look like a clean channel forever."""
    channel_server.reply = (status, {"error": "no"})
    _stdin(monkeypatch, {})
    hooks.stop_hook()
    captured = capsys.readouterr()
    assert captured.out == ""  # still fail-open: no block
    assert str(status) in captured.err
    assert hooks.URL_ENV in captured.err


def test_stop_hook_is_quiet_about_server_errors(channel_server, capsys, monkeypatch):
    channel_server.reply = (503, {"error": "later"})
    _stdin(monkeypatch, {})
    hooks.stop_hook()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


@pytest.mark.parametrize("body", [None, [], {"counts": None}, {"counts": [1]}, b"nope"])
def test_stop_hook_treats_a_malformed_answer_as_unavailable(
    body, channel_server, capsys, monkeypatch
):
    channel_server.reply = (200, body)
    _stdin(monkeypatch, {})
    hooks.stop_hook()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_stop_hook_reblocks_on_growth_in_remote_mode(channel_server, capsys, monkeypatch):
    channel_server.reply = (200, {"role": "backend", "counts": {"unread": 1}})
    _assert_stop(monkeypatch, capsys, blocks=True)
    channel_server.reply = (200, {"role": "backend", "counts": {"unread": 3}})
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.stop_hook()
    assert "new items arrived" in json.loads(capsys.readouterr().out)["reason"]


def test_stop_hook_verifies_certificates(monkeypatch, tmp_path, capsys):
    """A python.org macOS build has no usable CA path: without the certifi
    context every HTTPS check fails and the fail-open hook passes every stop."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(hooks.URL_ENV, "https://host")
    monkeypatch.setenv(hooks.TOKEN_ENV, "cct_x")
    seen = {}

    def fake_urlopen(request, timeout=None, context=None):
        seen["context"] = context
        raise OSError("stop here")

    monkeypatch.setattr("ai_agent_channel.client._urlopen", fake_urlopen)
    _stdin(monkeypatch, {})
    hooks.stop_hook()
    assert capsys.readouterr().out == ""
    assert seen["context"] is not None
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("message", [hooks.SESSION_MESSAGE, hooks.STOP_MESSAGE])
def test_triage_text_names_the_tools_behind_the_counts(message):
    """channel_status only COUNTS unread mail, open obligations and pending
    acks; telling the agent to triage "the list" sends it looking for
    something that is not in the answer."""
    for tool in ("read_inbox()", "open_obligations()", "awaiting_ack()"):
        assert tool in message
    for name in ("channel_status", "read_inbox", "open_obligations", "awaiting_ack"):
        assert callable(getattr(server, name)), name


def test_session_message_says_those_are_counts():
    assert "COUNTS" in hooks.SESSION_MESSAGE


def test_stop_hook_warns_when_the_url_is_refused(monkeypatch, capsys, tmp_path):
    """A URL the client refuses (plain http:// to another host) never heals
    on retry; failing open SILENTLY would disable the hook for good."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(hooks.URL_ENV, "http://channel.example.com")
    monkeypatch.setenv(hooks.TOKEN_ENV, "cct_x")

    def must_not_connect(*args, **kwargs):
        raise AssertionError("a request was made")

    monkeypatch.setattr("ai_agent_channel.client._urlopen", must_not_connect, raising=False)
    _stdin(monkeypatch, {})
    hooks.stop_hook()
    captured = capsys.readouterr()
    assert captured.out == ""  # still fail-open
    assert "NOT checked" in captured.err
    assert "https://" in captured.err


# --- python -m ai_agent_channel.hooks ---------------------------------------


def test_module_entrypoint_dispatches(capsys, monkeypatch):
    hooks.main(["session-start"])
    assert "channel_status" in capsys.readouterr().out
    monkeypatch.delenv(hooks.ROLE_ENV, raising=False)
    _stdin(monkeypatch, {"stop_hook_active": True})
    hooks.main(["stop"])
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("argv", [[], ["bogus"], ["stop", "extra"]])
def test_module_entrypoint_usage_error_does_not_break_the_session(argv, capsys):
    hooks.main(argv)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage" in captured.err


def test_python_dash_m_hooks_runs():
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-m", "ai_agent_channel.hooks", "session-start"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0
    assert "channel_status" in result.stdout
