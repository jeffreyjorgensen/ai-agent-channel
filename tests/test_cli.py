"""The console command: channel state without an MCP session, with an exit
code a git hook can act on."""

from __future__ import annotations

import io
import json
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from ai_agent_channel import cli, hooks, server


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    # a developer's exported hosted-channel settings must not leak in
    for name in (cli.URL_ENV, cli.TOKEN_ENV, cli.TOKEN_FILE_ENV):
        monkeypatch.delenv(name, raising=False)


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, Any, str]:
    """(exit code, stdout decoded as JSON — or the raw text when it is not
    JSON, e.g. --text — and stderr). The middle value is whatever the command
    printed, so it is typed Any and each test reads the shape it asked for."""
    code = cli.run(argv)
    out = capsys.readouterr()
    try:
        parsed = json.loads(out.out)
    except json.JSONDecodeError:
        parsed = out.out
    return code, parsed, out.err


def test_clean_channel_exits_zero(db_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", "backend")
    code, state, _ = _run(capsys, ["status"])
    assert code == 0
    assert state["role"] == "backend"
    assert state["counts"]["unread"] == 0


def test_pending_debt_exits_nonzero(db_path: Path, capsys, as_role, monkeypatch):
    as_role("frontend")
    server.send_message(to="backend", topic="fix it", body="x", action_required=True)
    monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", "backend")
    code, state, _ = _run(capsys, ["status"])
    # a checker that always exits 0 is decoration — this is the whole point
    assert code == 1
    assert state["counts"]["open_obligations"] == 1


def test_role_flag_overrides_the_env(db_path: Path, capsys, as_role, monkeypatch):
    as_role("frontend")
    server.send_message(to="backend", topic="t", body="x")
    monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", "frontend")
    code, state, _ = _run(capsys, ["status", "--role", "backend"])
    assert (code, state["role"]) == (1, "backend")


def test_missing_role_is_an_error_not_a_clean_bill(db_path: Path, capsys, no_role):
    code, _, err = _run(capsys, ["status"])
    # exit 2, distinct from both 0 and 1: "cannot tell" must never read as
    # "nothing pending"
    assert code == 2
    assert "no role" in err


@pytest.mark.parametrize("what", ["status", "pins"])
def test_an_unopenable_local_db_exits_two(what, capsys, monkeypatch):
    """A traceback exits 1, which reads as "you owe something"."""
    monkeypatch.setenv("AI_AGENT_CHANNEL_DB", "/dev/null/x.db")
    code, _, err = _run(capsys, [what, "--role", "backend"])
    assert code == 2
    assert err.startswith("ai-agent-channel: /dev/null/x.db")


def test_unreachable_server_is_loud(capsys, monkeypatch):
    monkeypatch.setenv("AI_AGENT_CHANNEL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AI_AGENT_CHANNEL_TOKEN", "cct_whatever")
    code, _, err = _run(capsys, ["status"])
    assert code == 2
    assert "/status" in err


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a: object) -> None:
        return None


def _serve_body(monkeypatch, body: bytes) -> None:
    monkeypatch.setenv(cli.URL_ENV, "https://host")
    monkeypatch.setenv(cli.TOKEN_ENV, "cct_x")
    monkeypatch.setattr(
        "ai_agent_channel.client._urlopen",
        lambda request, timeout=None, context=None: FakeResponse(body),
    )


@pytest.mark.parametrize(
    ("what", "body"),
    [
        ("status", b"null"),
        ("status", b"[]"),
        ("status", b'{"role": "x"}'),
        ("pins", b"null"),
        ("pins", b'{"counts": {}}'),
    ],
)
def test_a_malformed_server_answer_exits_two(what, body, capsys, monkeypatch):
    _serve_body(monkeypatch, body)
    code, _, err = _run(capsys, [what])
    assert code == 2
    assert err.startswith("ai-agent-channel: ")


def test_pins_listing_has_digests_and_no_bodies(db_path: Path, capsys, as_role):
    as_role("frontend")
    server.pin_set(key="notes", title="N", body="document body", version="v3")
    code, pins, _ = _run(capsys, ["pins", "--role", "frontend"])
    assert code == 0
    assert pins[0]["key"] == "notes"
    assert len(pins[0]["body_sha256"]) == 64
    assert pins[0]["body_length_bytes"] > 0
    assert "body" not in pins[0]


def test_pins_text_is_one_tsv_line_per_pin(db_path: Path, capsys, as_role):
    as_role("frontend")
    server.pin_set(key="notes", title="N", body="caf\u00e9 document body", version="v3")
    code, text, _ = _run(capsys, ["pins", "--text", "--role", "frontend"])
    assert code == 0
    [line] = text.splitlines()
    fields = line.split("\t")
    assert (fields[0], fields[1], len(fields[5])) == ("notes", "v3", 64)
    assert "caf\u00e9" not in line


def test_text_output_is_greppable(db_path: Path, capsys, as_role, monkeypatch):
    as_role("frontend")
    server.send_message(to="backend", topic="fix login", body="x", action_required=True)
    monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", "backend")
    code, text, _ = _run(capsys, ["status", "--text"])
    assert code == 1
    assert "role=backend" in text
    assert "open_obligations_untaken=1" in text
    assert "fix login" in text


def test_pending_uses_the_same_set_as_the_stop_hook():
    counts: dict[str, int] = dict.fromkeys(cli.db.ACTIONABLE_COUNTS, 1)
    counts["blocked"] = 5  # not actionable, must not leak in
    assert set(cli.pending(counts)) == set(cli.db.ACTIONABLE_COUNTS)


# --- watch: the wake loop ---------------------------------------------------


class FakeChannel:
    """Serves a scripted sequence of channel states, one per poll."""

    def __init__(self, *states: dict | Exception):
        self.states = list(states)
        self.polls = 0
        self.waits: list[int] = []

    def __call__(self, role, *, wait_s: int = 0):
        self.waits.append(wait_s)
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        if isinstance(state, Exception):
            raise state
        return {"counts": state}


def run_watch(monkeypatch, channel: FakeChannel, polls: int = 1) -> list[str]:
    """Drive watch() for exactly `polls` iterations, then stop it."""
    monkeypatch.setattr(cli, "gather_status", channel)
    lines: list[str] = []

    def stop_after_enough(_seconds):
        if channel.polls >= polls:
            raise KeyboardInterrupt

    with suppress(KeyboardInterrupt):
        cli.watch("backend", interval_s=1, emit=lines.append, sleep=stop_after_enough)
    return lines


def test_watch_reports_what_is_already_outstanding_on_the_first_pass(monkeypatch):
    lines = run_watch(monkeypatch, FakeChannel({"unread": 2}))
    assert len(lines) == 1
    assert "unread=2" in lines[0]
    # a fact, not an order: knowing early is for factoring in, not for
    # dropping whatever is in hand
    assert "mail arrived" in lines[0]
    assert "Nothing is required this second" in lines[0]


def test_watch_is_silent_on_a_clean_channel(monkeypatch):
    assert run_watch(monkeypatch, FakeChannel({})) == []


def test_watch_only_speaks_when_something_grew(monkeypatch):
    """Edge-triggered, not level-triggered: a line on every poll would say
    'you still owe three things' forever and get the monitor rate-limited."""
    channel = FakeChannel({"unread": 1}, {"unread": 1}, {"unread": 3}, {"unread": 3})
    lines = run_watch(monkeypatch, channel, polls=4)
    assert len(lines) == 2  # the first pass, then the growth
    assert "unread=1" in lines[0]
    assert "unread=3" in lines[1]


def test_watch_says_nothing_when_a_debt_is_paid_off(monkeypatch):
    channel = FakeChannel({"unread": 2}, {})
    assert len(run_watch(monkeypatch, channel, polls=2)) == 1


def test_watch_survives_and_reports_a_dead_channel(monkeypatch):
    """A watcher that dies silently looks exactly like a quiet channel."""
    channel = FakeChannel(cli.ChannelUnavailable("connection refused"))
    lines = run_watch(monkeypatch, channel)
    assert len(lines) == 1
    assert "cannot read the channel" in lines[0]


def test_watch_treats_an_answer_without_counts_as_an_outage(monkeypatch):
    monkeypatch.setattr(cli, "gather_status", lambda role, *, wait_s=0: {"role": "x"})
    lines: list[str] = []
    cli.watch("backend", interval_s=1, emit=lines.append, forever=False)
    assert len(lines) == 1
    assert "cannot read the channel" in lines[0]


def test_watch_once_does_not_back_off_after_a_failure(monkeypatch):
    monkeypatch.setattr(cli, "gather_status", FakeChannel(cli.ChannelUnavailable("down")))

    def no_sleep(_seconds):
        raise AssertionError("a single pass must not sleep")

    lines: list[str] = []
    cli.watch("backend", interval_s=1, emit=lines.append, sleep=no_sleep, forever=False)
    assert len(lines) == 1


def test_watch_rejects_a_nonsense_interval(capsys, monkeypatch):
    monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", "backend")
    assert cli.run(["watch", "--interval", "0"]) == 2
    assert "interval" in capsys.readouterr().err


def test_ctrl_c_ends_watch_cleanly(capsys, monkeypatch):
    def interrupted(role, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "watch", interrupted)
    assert cli.run(["watch"]) == 0
    assert capsys.readouterr().err == ""


def test_watch_reports_an_outage_once_and_backs_off(monkeypatch):
    """Emitting on every failed tick would get the watcher rate-limited and
    killed by the harness — turning a passing network problem into
    permanently no wake-ups."""
    channel = FakeChannel(*[cli.ChannelUnavailable("refused")] * 6)
    monkeypatch.setattr(cli, "gather_status", channel)
    lines: list[str] = []
    naps: list[float] = []

    def record_sleep(seconds):
        naps.append(seconds)
        if channel.polls >= 6:
            raise KeyboardInterrupt

    with suppress(KeyboardInterrupt):
        cli.watch("backend", interval_s=10, emit=lines.append, sleep=record_sleep)

    assert len(lines) == 1  # one line, not six
    assert "retrying" in lines[0]
    assert naps == sorted(naps) and naps[0] < naps[-1]  # it backs off
    assert max(naps) <= cli.BACKOFF_MAX_S  # and stays bounded


def test_watch_announces_recovery(monkeypatch):
    channel = FakeChannel(cli.ChannelUnavailable("refused"), {}, {})
    monkeypatch.setattr(cli, "gather_status", channel)
    lines: list[str] = []

    def stop(_):
        if channel.polls >= 2:
            raise KeyboardInterrupt

    with suppress(KeyboardInterrupt):
        cli.watch("backend", interval_s=1, emit=lines.append, sleep=stop)

    assert len(lines) == 2
    assert "cannot read" in lines[0]
    assert "readable again" in lines[1]


def test_watch_long_polls_only_against_a_server(monkeypatch):
    """Local mode has nobody to hold the request; asking for a wait there
    would just be a parameter nothing reads."""
    channel = FakeChannel({}, {})
    monkeypatch.setattr(cli, "gather_status", channel)
    cli.watch("backend", interval_s=1, emit=lambda _: None, forever=False)
    assert channel.waits == [0]

    channel = FakeChannel({}, {})
    monkeypatch.setattr(cli, "gather_status", channel)
    monkeypatch.setenv(cli.URL_ENV, "https://host")
    monkeypatch.setenv(cli.TOKEN_ENV, "cct_x")
    cli.watch("backend", interval_s=1, emit=lambda _: None, forever=False)
    assert channel.waits == [cli.WATCH_LONGPOLL_S]


# --- the token never travels in argv ----------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--role", "cct_leaked"],
        ["watch", "--journal", "ccv_oops"],
        ["status", "--role=cct_leaked"],
        ["pins", "cca_admin"],
    ],
)
def test_token_is_never_taken_from_the_command_line(argv, capsys):
    """argv is world-readable: `ps` shows it to every process on the box.
    The rule is enforced, not documented — a rule you have to remember is
    not a rule."""
    code, out, err = _run(capsys, argv)
    assert code == 2
    assert out == ""
    assert "visible in `ps`" in err
    assert cli.TOKEN_FILE_ENV in err


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--role", "product_cct_x"],
        ["status", "--role", "backend", "--journal", "/tmp/account_cct_log.jsonl"],
        ["status", "--role", "backend", "--journal=/tmp/ccv_x.jsonl"],
    ],
)
def test_values_that_merely_contain_a_prefix_are_not_tokens(argv, db_path: Path, capsys):
    code, state, _ = _run(capsys, argv)
    assert code == 0
    assert isinstance(state, dict)


def test_token_file_must_not_be_readable_by_others(tmp_path, capsys, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("cct_x")
    secret.chmod(0o644)
    monkeypatch.setenv(cli.URL_ENV, "http://127.0.0.1:1")
    monkeypatch.setenv(cli.TOKEN_FILE_ENV, str(secret))
    code, _, err = _run(capsys, ["status"])
    assert code == 2
    assert "readable by others" in err

    secret.chmod(0o600)
    code, _, err = _run(capsys, ["status"])
    assert code == 2
    assert "/status" in err  # past the file check, on to the (dead) server


def test_a_missing_token_file_exits_two(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(cli.URL_ENV, "https://host")
    monkeypatch.setenv(cli.TOKEN_FILE_ENV, str(tmp_path / "nope"))
    code, _, err = _run(capsys, ["status"])
    assert code == 2
    assert cli.TOKEN_FILE_ENV in err


# --- the trial run has to be reportable in numbers --------------------------


def test_watch_journals_every_wake(tmp_path, monkeypatch):
    journal = tmp_path / "watch.jsonl"
    channel = FakeChannel({"unread": 1}, {"unread": 1}, {"unread": 4})
    monkeypatch.setattr(cli, "gather_status", channel)

    def stop(_):
        if channel.polls >= 3:
            raise KeyboardInterrupt

    with suppress(KeyboardInterrupt):
        cli.watch("backend", interval_s=1, emit=lambda _: None, sleep=stop, journal=str(journal))

    lines = [json.loads(x) for x in journal.read_text().splitlines()]
    assert [x["event"] for x in lines] == ["wake", "wake"]
    assert lines[0]["pending"] == {"unread": 1} and lines[0]["first_pass"] is True
    assert lines[1]["pending"] == {"unread": 4}
    assert lines[1]["wakes_so_far"] == 2 and lines[1]["polls_so_far"] == 3


def test_journal_records_outages_too(tmp_path, monkeypatch):
    journal = tmp_path / "watch.jsonl"
    channel = FakeChannel(cli.ChannelUnavailable("refused"), {})
    monkeypatch.setattr(cli, "gather_status", channel)

    def stop(_):
        if channel.polls >= 2:
            raise KeyboardInterrupt

    with suppress(KeyboardInterrupt):
        cli.watch("backend", interval_s=1, emit=lambda _: None, sleep=stop, journal=str(journal))

    events = [json.loads(x)["event"] for x in journal.read_text().splitlines()]
    assert events == ["unreachable", "recovered"]


def test_a_broken_journal_never_kills_the_watcher(tmp_path, monkeypatch):
    channel = FakeChannel({"unread": 1})
    monkeypatch.setattr(cli, "gather_status", channel)
    lines: list[str] = []
    # a directory is not writable as a file
    cli.watch("backend", interval_s=1, emit=lines.append, forever=False, journal=str(tmp_path))
    assert len(lines) == 1  # the wake still happened


def test_https_calls_carry_a_real_user_agent_and_verify_certificates(monkeypatch):
    """Two things a proxy taught us the hard way: Cloudflare answers 403 to
    'Python-urllib', and Python does not use the system trust store, so
    HTTPS fails on macOS while curl to the same URL succeeds."""
    monkeypatch.setenv(cli.URL_ENV, "https://host")
    monkeypatch.setenv(cli.TOKEN_ENV, "cct_x")
    captured = {}

    def fake_urlopen(request, timeout=None, context=None):
        captured["ua"] = request.get_header("User-agent")
        captured["auth"] = request.get_header("Authorization")
        captured["context"] = context
        return FakeResponse(b'{"counts": {}}')

    monkeypatch.setattr("ai_agent_channel.client._urlopen", fake_urlopen)
    cli.gather_status(None)

    assert "urllib" not in (captured["ua"] or "").lower()
    assert "ai-agent-channel" in captured["ua"]
    assert captured["auth"] == "Bearer cct_x"
    # verification stays on — a status tool that trusts any certificate is
    # worse than no status tool
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED


def test_the_stop_hook_sends_one_too(monkeypatch, tmp_path):
    """It is FAIL-OPEN: blocked by a WAF it would silently pass every stop,
    which is indistinguishable from a clean channel."""
    assert "urllib" not in hooks.USER_AGENT.lower()
    assert hooks.USER_AGENT == cli.USER_AGENT

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(hooks.URL_ENV, "https://host")
    monkeypatch.setenv(hooks.TOKEN_ENV, "cct_x")
    seen = {}

    def fake_urlopen(request, timeout=None, context=None):
        seen["ua"] = request.get_header("User-agent")
        raise OSError("stop here")

    monkeypatch.setattr("ai_agent_channel.client._urlopen", fake_urlopen)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    hooks.stop_hook()
    assert "ai-agent-channel" in seen["ua"]


# --- watch: back-to-back long polls ------------------------------------------


class _LongPollServer(BaseHTTPRequestHandler):
    """A channel server that answers every /status at once, as a held long
    poll that ended would: `quiet` empty windows, then one arrival."""

    def do_GET(self):
        server = self.server
        server.paths.append(self.path)  # type: ignore[attr-defined]
        arrived = len(server.paths) > server.quiet  # type: ignore[attr-defined]
        counts = {"unread": 1} if arrived else {}
        state = {"channel": "c", "role": "backend", "counts": counts, "pending": counts}
        if server.supports_wait:  # type: ignore[attr-defined]
            state["waited"] = "wait=" in self.path
        body = json.dumps(state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def _long_poll_server(monkeypatch, *, quiet: int, supports_wait: bool) -> Iterator[HTTPServer]:
    httpd = HTTPServer(("127.0.0.1", 0), _LongPollServer)
    httpd.paths = []  # type: ignore[attr-defined]
    httpd.quiet = quiet  # type: ignore[attr-defined]
    httpd.supports_wait = supports_wait  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setenv(cli.URL_ENV, f"http://127.0.0.1:{httpd.server_port}")
    monkeypatch.setenv(cli.TOKEN_ENV, "cct_x")
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _watch_until_first_sleep(interval_s: int) -> tuple[list[str], list[float]]:
    lines: list[str] = []
    naps: list[float] = []

    def sleep(seconds):
        naps.append(seconds)
        raise KeyboardInterrupt

    with suppress(KeyboardInterrupt):
        cli.watch(None, interval_s=interval_s, emit=lines.append, sleep=sleep)
    return lines, naps


def test_watch_asks_again_at_once_after_a_quiet_long_poll(monkeypatch):
    """A held request that ends empty already waited its window; sleeping
    --interval on top of it left a blind spot after every quiet window."""
    with _long_poll_server(monkeypatch, quiet=3, supports_wait=True) as server:
        lines, naps = _watch_until_first_sleep(interval_s=30)
    paths = server.paths  # type: ignore[attr-defined]
    assert len(paths) == 4  # three quiet windows back to back, then the arrival
    assert all(f"wait={cli.WATCH_LONGPOLL_S}" in p for p in paths)
    assert naps == [30]  # the only sleep came once something was outstanding
    assert "unread=1" in lines[0]


def test_watch_does_not_spin_against_a_server_without_long_polling(monkeypatch):
    with _long_poll_server(monkeypatch, quiet=100, supports_wait=False) as server:
        lines, naps = _watch_until_first_sleep(interval_s=30)
    assert len(server.paths) == 1  # type: ignore[attr-defined]
    assert naps == [30]
    assert lines == []


def test_help_names_the_token_file(capsys):
    with pytest.raises(SystemExit):
        cli.run(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert f"{cli.URL_ENV} and {cli.TOKEN_ENV} (or {cli.TOKEN_FILE_ENV}) are set" in text
