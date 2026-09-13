"""Blocking waits that stay under MCP client tool timeouts: wait_for_reply and
wait_for_mail. Tests patch WAIT_CAP_S and _poll_until on this module."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, TypeVar

from .. import db
from .common import current_role, open_channel_db
from .registry import run_in_thread, tool

# Per-call wait ceiling for both waits: below typical MCP client tool timeouts
# (60s by default in Claude Code, MCP_TOOL_TIMEOUT), so a long wait ends as a
# retryable {timed_out: true, retry: true}, never as a client-side timeout.
WAIT_CAP_S = 50
# The former name. Read-only alias: the tools read WAIT_CAP_S, so patch that.
WAIT_FOR_REPLY_CAP_S = WAIT_CAP_S

TIMEOUT_MAX_S = 3600
POLL_MIN_S = 0.1
POLL_MAX_S = 60

_T = TypeVar("_T")


def _wait_budget(timeout_s: int, poll_interval_s: float) -> int:
    if timeout_s < 0 or timeout_s > TIMEOUT_MAX_S:
        raise ValueError(f"'timeout_s' must be between 0 and {TIMEOUT_MAX_S}")
    if poll_interval_s < POLL_MIN_S or poll_interval_s > POLL_MAX_S:
        raise ValueError(f"'poll_interval_s' must be between {POLL_MIN_S} and {POLL_MAX_S}")
    return min(timeout_s, WAIT_CAP_S)


async def _poll_until(
    poll: Callable[[], _T | None], *, timeout_s: float, poll_interval_s: float
) -> _T | None:
    """Run `poll` (blocking DB work, so in a worker thread) until it returns
    something or the deadline passes. The last poll happens at the deadline,
    never a full interval after it."""
    deadline = time.monotonic() + timeout_s
    while True:
        result = await run_in_thread(poll)
        if result is not None:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(poll_interval_s, remaining))


_CAP_DOC = (
    f"The per-call wait is capped at {WAIT_CAP_S}s (below MCP client tool "
    "timeouts), so wait longer by calling again."
)


@tool(
    description=(
        "Block-poll the inbox for a reply to a given message_id that you have "
        "NOT seen yet. Returns the reply message, or {timed_out: true, retry: "
        "true} when none arrived — nothing is lost on timeout: a late reply "
        "stays in the DB and in unread, and the NEXT wait_for_reply call "
        "returns it immediately. "
        "'Not seen yet' means: not already marked read by you "
        "(include_read=true drops that condition), and — if you pass "
        "'after_id' — newer than that id; pass after_id=<the last reply you "
        "handled> when looping without marking things read. A message_id "
        "that does not exist is refused. " + _CAP_DOC
    ),
    read_only=True,
)
async def wait_for_reply(
    message_id: int,
    timeout_s: int = 50,
    poll_interval_s: float = 1.0,
    after_id: int | None = None,
    include_read: bool = False,
) -> dict[str, Any]:
    role = current_role()
    effective_s = _wait_budget(timeout_s, poll_interval_s)

    def parent_exists() -> bool:
        with open_channel_db() as conn:
            return db.fetch_message(conn, message_id) is not None

    if not await run_in_thread(parent_exists):
        raise ValueError(f"message {message_id} not found")

    def poll() -> dict[str, Any] | None:
        with open_channel_db() as conn:
            return db.find_reply(
                conn,
                parent_id=message_id,
                role=role,
                after_id=after_id,
                include_read=include_read,
            )

    reply = await _poll_until(poll, timeout_s=effective_s, poll_interval_s=poll_interval_s)
    if reply is not None:
        return reply
    return {
        "timed_out": True,
        "waited_s": effective_s,
        "retry": True,
        "note": (
            "no reply yet — nothing is lost: a late reply lands in "
            "unread and the next wait_for_reply returns it "
            "immediately; call wait_for_reply again to keep waiting"
        ),
    }


@tool(
    description=(
        "Sleep inside the channel until something NEW appears for you — new "
        "mail, a fresh obligation, a proposal to decide, a ball thrown back "
        "at you, a resolution to verify. Use it when you have finished your "
        "own work and want to stay available to the partner instead of "
        "ending the turn (wait_for_reply waits for a reply to ONE message; "
        "this waits for any event). "
        "It wakes when an item appears in one of the actionable counters "
        "that was not there when you called — even if another item left the "
        "same counter meanwhile — and returns those counters as 'pending'. "
        "The backlog you already carried is returned as 'pending_at_entry' "
        "and does not wake you; ignore_backlog=false returns immediately if "
        "anything at all is pending. "
        "On an empty wait it returns {timed_out: true, retry: true}. "
        + _CAP_DOC
        + " Nothing is lost between calls."
    ),
    read_only=True,
)
async def wait_for_mail(
    timeout_s: int = 50,
    poll_interval_s: float = 2.0,
    ignore_backlog: bool = True,
) -> dict[str, Any]:
    role = current_role()
    effective_s = _wait_budget(timeout_s, poll_interval_s)

    def snapshot() -> tuple[dict[str, int], dict[str, set[int]]]:
        with open_channel_db() as conn:
            # one summary per poll: the ids reuse it instead of recomputing
            summary = db.channel_summary(conn, role=role)
            return summary["counts"], db.actionable_ids(conn, role=role, summary=summary)

    entry, entry_ids = await run_in_thread(snapshot)
    at_entry = {k: entry[k] for k in db.ACTIONABLE_COUNTS if entry.get(k, 0)}
    # What the caller already has, by id rather than by count: a count stays
    # level when one item leaves and another arrives. An item that leaves
    # and later comes back counts as new.
    known = {k: (set(entry_ids[k]) if ignore_backlog else set()) for k in db.ACTIONABLE_COUNTS}

    def poll() -> dict[str, int] | None:
        counts, ids = snapshot()
        pending = {k: counts[k] for k in db.ACTIONABLE_COUNTS if ids[k] - known[k]}
        for k in db.ACTIONABLE_COUNTS:
            known[k] &= ids[k]
        return pending or None

    pending = await _poll_until(poll, timeout_s=effective_s, poll_interval_s=poll_interval_s)
    if pending is not None:
        return {
            "pending": pending,
            "pending_at_entry": at_entry,
            "timed_out": False,
        }
    return {
        "timed_out": True,
        "waited_s": effective_s,
        "retry": True,
        "pending_at_entry": at_entry,
        "note": (
            "nothing new — call wait_for_mail again to keep waiting, "
            "or finish; anything that arrives later is durable and "
            "surfaces on the next call, the stop hook, or session "
            "start. 'pending_at_entry' is what you were already "
            "carrying when this wait began; it did not wake you"
        ),
    }
