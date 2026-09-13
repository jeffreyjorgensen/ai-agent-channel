"""Fixtures shared across the suite.

Helpers that are plain functions (``L``, ``payload``, ``client``...) live in
helpers.py, importable because pyproject puts tests/ on ``pythonpath``; the
suite runs under ``--import-mode=importlib``, so no test module may import
another test module.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ai_agent_channel import auth
from ai_agent_channel.tools import waiting
from helpers import running_server

# Creating the FastMCP instance (imported above, through the tools) installs a
# RichHandler on the root logger. Its console holds the terminal it found at
# import time, so records from the uvicorn thread ("Terminating session: None"
# from mcp's streamable_http) were printed straight into pytest's progress
# output. Without it, records go to pytest's own log capture: shown with a
# failing test, available to caplog, and silent otherwise.
_root = logging.getLogger()
for _handler in list(_root.handlers):
    if type(_handler).__name__ == "RichHandler":
        _root.removeHandler(_handler)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "messages.db"
    monkeypatch.setenv("AI_AGENT_CHANNEL_DB", str(p))
    return p


@pytest.fixture
def as_role(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    def _set(role: str) -> None:
        monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", role)

    return _set


@pytest.fixture
def no_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_AGENT_CHANNEL_ROLE", raising=False)


# --- HTTP-mode identity without uvicorn -------------------------------------


@pytest.fixture
def channel_identity(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Callable[[Iterable[str]], Callable[[str], None]], None, None]:
    """Factory: ``channel_identity(ROLES)`` returns ``act_as(role)``, which
    injects a registered-channel identity into auth.CURRENT_IDENTITY exactly as
    the HTTP middleware does. Several rules exist only where the server KNOWS
    the roster, so these run as fast units instead of through a real server.

    Teardown restores the variable. An async test sets it inside its own task
    context, whose tokens cannot be reset from the fixture's context; those
    values never leaked out of the task, so restoring the prior value is enough.
    """
    monkeypatch.delenv("AI_AGENT_CHANNEL_ROLE", raising=False)
    before = auth.CURRENT_IDENTITY.get()
    tokens: list[contextvars.Token[auth.Identity | None]] = []

    def factory(roles: Iterable[str], channel: str = "team") -> Callable[[str], None]:
        roster = tuple(roles)

        def act_as(role: str) -> None:
            identity = auth.Identity(
                is_admin=False,
                channel=channel,
                role=role,
                peers=tuple(r for r in roster if r != role),
                db_path=db_path,
            )
            tokens.append(auth.CURRENT_IDENTITY.set(identity))

        return act_as

    yield factory
    for token in reversed(tokens):
        try:
            auth.CURRENT_IDENTITY.reset(token)
        except (ValueError, RuntimeError):
            auth.CURRENT_IDENTITY.set(before)
            break


# --- waiting tools on a compressed clock ------------------------------------


@dataclass
class FastWait:
    """Handle returned by the ``fast_wait`` fixture."""

    scale: float
    polls: int = 0

    async def until_polled(self, n: int = 2, timeout: float = 5.0) -> None:
        """Return once ``n`` more polls have COMPLETED. Two guarantee that at
        least one of them started after the caller's last change."""
        target = self.polls + n
        deadline = time.monotonic() + timeout
        while self.polls < target:
            if time.monotonic() > deadline:
                raise AssertionError(f"the waiter did not poll {n} more times")
            await asyncio.sleep(0.002)


@pytest.fixture
def fast_wait(monkeypatch: pytest.MonkeyPatch) -> FastWait:
    """Run wait_for_reply/wait_for_mail on a clock ten times faster.

    The tools validate and report their arguments unchanged (timeout_s is a
    whole number of seconds, poll_interval_s >= 0.1); only the real polling
    loop underneath sees the scaled values, so ``waited_s`` and every other
    assertion keep their meaning. Polls are counted so tests can interleave
    writes with the waiter by event instead of by sleeping.
    """
    handle = FastWait(scale=0.1)
    # The one seam into the waiting tools: every wait goes through
    # tools.waiting._poll_until, so this is the only place tests patch it.
    real = waiting._poll_until

    async def scaled(poll: Callable[[], Any], *, timeout_s: float, poll_interval_s: float) -> Any:
        def counted() -> Any:
            try:
                return poll()
            finally:
                handle.polls += 1

        return await real(
            counted,
            timeout_s=timeout_s * handle.scale,
            poll_interval_s=poll_interval_s * handle.scale,
        )

    monkeypatch.setattr(waiting, "_poll_until", scaled)
    return handle


# --- a real uvicorn ----------------------------------------------------------


@pytest.fixture
def http_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    with running_server(tmp_path, monkeypatch) as url:
        yield url
