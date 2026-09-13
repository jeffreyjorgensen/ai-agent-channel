"""Read the channel from a plain shell, with no agent session running.

Everything else here assumes an MCP client: the channel is only visible to
something that remembered to look at it. That makes the regimen depend on an
agent's memory, and it puts the channel out of reach of the places where
checks actually belong — a pre-commit hook cannot open an MCP session, so a
"is my copy of the contract still the agreed one" check could not run where
it would be useful.

Three properties this must keep:

* **It is part of this package.** Five roles pointing their own scripts at
  the SQLite schema would all break together on the first migration, and
  silently. The command is the compatibility boundary.
* **It queries live state.** An exported snapshot on disk goes stale while
  continuing to look authoritative — which is the failure mode the channel
  exists to remove, reintroduced one layer down.
* **It speaks whichever transport the mailbox uses.** Local DB today, hosted
  server tomorrow; a caller's script should not care.

Exit code 1 when the role owes something, 2 when the channel cannot be read.
A checker that always exits 0 is decoration, and a hook that wraps it would
pass forever.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from . import client, db

URL_ENV = client.URL_ENV
TOKEN_ENV = client.TOKEN_ENV
TOKEN_FILE_ENV = client.TOKEN_FILE_ENV
ROLE_ENV = client.ROLE_ENV
USER_AGENT = client.USER_AGENT
TIMEOUT_S = 10.0

# The token NEVER comes from the command line. argv is world-readable: any
# process on the machine can read it out of `ps`, and it lands in shell
# history and in whatever supervises the watcher. This is enforced, not
# merely documented — a rule that depends on remembering it is not a rule.
TOKEN_PREFIXES = client.TOKEN_PREFIXES


def _token_in_argv(argv: list[str]) -> bool:
    # a value that IS a token, bare or as --flag=value; a path or role name
    # that merely contains "cct_" is not one
    return any(
        value.startswith(TOKEN_PREFIXES) for arg in argv for value in (arg, arg.partition("=")[2])
    )


class ChannelUnavailable(Exception):
    """The channel could not be read. Loud on purpose: a checker whose own
    failure is indistinguishable from "nothing pending" is worse than none."""


def _remote() -> tuple[str, str] | None:
    try:
        return client.remote_config()
    except client.TokenFileError as exc:
        raise ChannelUnavailable(str(exc)) from exc


def _fetch_remote(
    remote: tuple[str, str], path: str, *, timeout: float = TIMEOUT_S
) -> dict[str, Any]:
    url, token = remote
    try:
        data = client.get_json(url, path, token, timeout=timeout)
    except client.ClientError as exc:
        raise ChannelUnavailable(str(exc)) from exc
    if not isinstance(data, dict):
        raise ChannelUnavailable(f"{url}{path}: expected a JSON object")
    return data


def _counts(state: dict[str, Any]) -> dict[str, int]:
    counts = state.get("counts")
    if not isinstance(counts, dict):
        raise ChannelUnavailable("the channel answered without counts")
    return counts


@contextmanager
def _local_db() -> Iterator[sqlite3.Connection]:
    # an unopenable DB is "cannot read the channel" (2), not a traceback —
    # whose exit 1 would read as "you owe something"
    try:
        with db.open_db() as conn:
            yield conn
    except (OSError, sqlite3.Error) as exc:
        raise ChannelUnavailable(f"{db.get_db_path()}: {exc}") from exc


def _local_role(explicit: str | None) -> str:
    role = (explicit or os.environ.get(ROLE_ENV, "")).strip()
    if not role:
        raise ChannelUnavailable(
            f"no role: pass --role, or set {ROLE_ENV} (locally) or "
            f"{URL_ENV} plus {TOKEN_ENV} or {TOKEN_FILE_ENV} (hosted, where the "
            "token implies the role)"
        )
    return role


def gather_status(role: str | None, *, wait_s: int = 0) -> dict[str, Any]:
    remote = _remote()
    if remote is not None:
        # wait_s > 0 asks the server to hold the request until something
        # actionable appears; the read timeout must outlast that hold or we
        # would time out on our own long poll.
        state = _fetch_remote(
            remote,
            f"/status?wait={wait_s}" if wait_s else "/status",
            timeout=TIMEOUT_S + wait_s,
        )
        _counts(state)
        return state
    resolved = _local_role(role)
    with _local_db() as conn:
        summary = db.channel_summary(conn, role=resolved)
        summary["debts"] = db.project(
            db.list_messages(
                conn,
                topic=None,
                from_role=None,
                to_role=resolved,
                unread_only=False,
                since=None,
                limit=100,
                status="open",
            ),
            db.MESSAGE_HEADERS,
        )
    return {"channel": "local", "role": resolved, **summary}


def gather_pins(role: str | None) -> list[dict[str, Any]]:
    remote = _remote()
    if remote is not None:
        pins = _fetch_remote(remote, "/status").get("pins")
        if not isinstance(pins, list):
            raise ChannelUnavailable("the channel answered without pins")
        return pins
    _local_role(role)
    with _local_db() as conn:
        return db.pin_list(conn)


def pending(counts: dict[str, int]) -> dict[str, int]:
    return {k: counts[k] for k in db.ACTIONABLE_COUNTS if counts.get(k, 0)}


# --- watch ------------------------------------------------------------------
#
# The channel is pull-based because MCP is: a server cannot hand work to a
# client that is not currently inside a tool call, and the spec is closing
# that door further rather than opening it. But the POLLING does not have to
# happen inside the agent's turn. Run it in a background process whose stdout
# the harness watches (Claude Code's Monitor tool), and an arriving line
# wakes the session — verified: an event delivered while the session sat idle
# at its prompt did wake it.
#
# What that does and does not buy:
#   * an OPEN session idling at the prompt gets woken;
#   * a session that has EXITED does not — there is no process to wake, and
#     nothing here pretends otherwise. That case is still answered by the
#     durable ledger plus the session-start triage.
#
# Emission is edge-triggered, never level-triggered. Monitors that produce
# too many events get stopped by the harness, and a line on every poll would
# say "you still owe three things" forever. Only growth is news.

WATCH_INTERVAL_S = 30
# Held-request window when the server supports it. The server caps this at
# its own ceiling; both stay below the idle timeouts of whatever proxy sits
# in front, so a quiet channel ends the request as our own empty answer.
WATCH_LONGPOLL_S = 50
BACKOFF_MAX_S = 300


def _journal(path: str | None, record: dict[str, Any]) -> None:
    """Append one line per wake, so "does it work" can be answered with
    numbers instead of an impression. What the watcher cannot know is
    whether a wake was USEFUL — that is a human judgement, and the log is
    what makes it annotatable after the fact."""
    if not path:
        return
    try:
        with open(pathlib.Path(path).expanduser(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # the journal is bookkeeping; it must never kill the watcher


def watch(
    role: str | None,
    *,
    interval_s: int,
    emit=print,
    sleep=time.sleep,
    forever: bool = True,
    journal: str | None = None,
    now=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"),
) -> None:
    seen: dict[str, int] = {}
    first_pass = True
    failures = 0
    stats = {"polls": 0, "wakes": 0, "failures": 0}
    while True:
        try:
            # Long-poll where we can: the server holds the request until
            # something actionable exists, so a new item reaches the session
            # in about a second instead of on the next tick. Local mode has
            # no server to hold anything, so it falls through to the sleep.
            wait_s = WATCH_LONGPOLL_S if _remote() else 0
            state = gather_status(role, wait_s=wait_s)
            counts = _counts(state)
            stats["polls"] += 1
            if failures:
                emit(f"ai-agent-channel: channel readable again after {failures} failures")
                _journal(journal, {"at": now(), "event": "recovered", "after_failures": failures})
                failures = 0
        except ChannelUnavailable as exc:
            # Report the OUTAGE, once, then back off. Emitting every tick
            # would get the watcher rate-limited and killed by the harness,
            # which turns a temporary network problem into permanently no
            # wake-ups — and a dead watcher is indistinguishable from a
            # quiet channel.
            failures += 1
            stats["failures"] += 1
            _journal(journal, {"at": now(), "event": "unreachable", "error": str(exc)})
            if failures == 1:
                emit(f"ai-agent-channel: cannot read the channel ({exc}) — retrying")
            if not forever:
                return
            sleep(min(interval_s * 2 ** min(failures, 8), BACKOFF_MAX_S))
            continue
        current = pending(counts)
        grown = {k: v for k, v in current.items() if v > seen.get(k, 0)}
        # The first pass reports whatever is already outstanding — a session
        # that arms this mid-flight should not have to wait for the next new
        # item to learn it owes something.
        if grown or (first_pass and current):
            reported = grown or current
            summary = ", ".join(f"{k}={v}" for k, v in reported.items())
            # A statement of fact, not an order. The point of knowing at the
            # moment mail lands is to be able to factor it in — not to drop
            # whatever is in hand. What must actually be triaged before the
            # turn ends is the stop hook's job, and it already does it.
            emit(
                f"ai-agent-channel: mail arrived — {summary}. "
                f"Nothing is required this second; read it when it fits, or "
                f"at the latest before you finish."
            )
            stats["wakes"] += 1
            _journal(
                journal,
                {
                    "at": now(),
                    "event": "wake",
                    "pending": reported,
                    "first_pass": first_pass,
                    "wakes_so_far": stats["wakes"],
                    "polls_so_far": stats["polls"],
                },
            )
        seen = current
        first_pass = False
        if not forever:
            return
        # A long poll that came back empty was HELD by the server for its
        # whole window: ask again at once, or every quiet window is followed
        # by a blind --interval in which an arrival waits for the next tick.
        # Sleep otherwise: a server that did not hold the request (local
        # mode, an older server without long polling) would be polled in a
        # tight loop, and so would a channel with something outstanding —
        # the server answers that immediately, and what is already pending
        # is not news.
        if wait_s and state.get("waited") is True and not current:
            continue
        sleep(interval_s)


def _print_status_text(state: dict[str, Any]) -> None:
    counts = state.get("counts", {})
    owed = pending(counts)
    print(f"channel={state.get('channel')} role={state.get('role')}")
    print("pending: " + (", ".join(f"{k}={v}" for k, v in owed.items()) or "nothing"))
    for label in ("debts", "needs_you", "resolved_for_you", "unblocked"):
        items = state.get(label) or []
        if isinstance(items, list) and items:
            print(f"{label}:")
            for item in items:
                print(f"  #{item.get('id')} {item.get('topic', '')}")


def _print_pins_text(pins: list[dict[str, Any]]) -> None:
    for pin in pins:
        print(
            f"{pin['key']}\t{pin.get('version')}\t{pin.get('updated_at')}\t"
            f"{pin.get('updated_by')}\t{pin.get('approved_by')}\t"
            f"{pin.get('body_sha256')}\t{pin.get('body_length_bytes')}"
        )


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ai-agent-channel-status",
        description=(
            "Print one role's channel state without an MCP session. Reads "
            "the local SQLite mailbox, or the hosted server when "
            f"{URL_ENV} and {TOKEN_ENV} (or {TOKEN_FILE_ENV}) are set. "
            "Exits 1 when the role has "
            "anything outstanding, so it can be wired into a git hook. "
            f"THE TOKEN IS NEVER AN ARGUMENT: pass it in {TOKEN_ENV}, or put "
            f"it in a file with mode 0600 and point {TOKEN_FILE_ENV} at it. "
            "A token on the command line is visible in `ps` to every process "
            "on the machine, so this command refuses to accept one there."
        ),
    )
    parser.add_argument(
        "what",
        nargs="?",
        default="status",
        choices=("status", "pins", "watch"),
        help="status: counters and debts; pins: one line per pinned document "
        "with its sha256 and length, no bodies; watch: run forever and "
        "print a line whenever something NEW needs you (feed it to a "
        "harness that watches stdout and it will wake an idle session)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=WATCH_INTERVAL_S,
        help=f"watch only: seconds between polls (default {WATCH_INTERVAL_S})",
    )
    parser.add_argument(
        "--journal",
        metavar="PATH",
        help="watch only: append one JSON line per wake/outage, so a trial "
        "run can be reported with numbers rather than impressions",
    )
    parser.add_argument("--role", help=f"overrides {ROLE_ENV} (local mode only)")
    parser.add_argument(
        "--text",
        action="store_true",
        help="human/TSV output instead of the default JSON",
    )
    if _token_in_argv(argv if argv is not None else sys.argv[1:]):
        print(
            "ai-agent-channel: refusing to take a token from the command "
            "line — it is visible in `ps` to every process on this "
            f"machine. Use {TOKEN_ENV}, or {TOKEN_FILE_ENV} pointing at a "
            "file with mode 0600.",
            file=sys.stderr,
        )
        return 2
    args = parser.parse_args(argv)

    if args.what == "watch":
        if args.interval < 1:
            print("ai-agent-channel: --interval must be >= 1", file=sys.stderr)
            return 2
        with suppress(KeyboardInterrupt):
            watch(args.role, interval_s=args.interval, journal=args.journal)
        return 0

    try:
        if args.what == "pins":
            pins = gather_pins(args.role)
            _print_pins_text(pins) if args.text else print(
                json.dumps(pins, ensure_ascii=False, indent=2)
            )
            return 0
        state = gather_status(args.role)
    except ChannelUnavailable as exc:
        # Exit 2, distinct from "you owe something": a caller must be able to
        # tell "the channel says you are clear" from "the channel is
        # unreachable", or a broken setup reads as a clean bill of health.
        print(f"ai-agent-channel: {exc}", file=sys.stderr)
        return 2

    if args.text:
        _print_status_text(state)
    else:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    return 1 if pending(state.get("counts", {})) else 0


def main() -> None:
    sys.exit(run())
