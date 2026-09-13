"""Every listing tool checks 'limit' against the same range."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_agent_channel import server

LISTINGS: dict[str, Callable[[int], Any]] = {
    "read_inbox": lambda n: server.read_inbox(limit=n),
    "list_messages": lambda n: server.list_messages(limit=n),
    "search_messages": lambda n: server.search_messages(query="hello", limit=n),
    "open_obligations": lambda n: server.open_obligations(limit=n),
    "ready_work": lambda n: server.ready_work(limit=n),
    "awaiting_ack": lambda n: server.awaiting_ack(limit=n),
}


@pytest.mark.parametrize("tool", sorted(LISTINGS))
@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_out_of_range_limit_is_refused(db_path: Path, as_role, tool, limit):
    as_role("frontend")
    with pytest.raises(ValueError, match="'limit' must be between 1 and 1000"):
        LISTINGS[tool](limit)


@pytest.mark.parametrize("tool", sorted(LISTINGS))
@pytest.mark.parametrize("limit", [1, 1000])
def test_in_range_limit_is_accepted(db_path: Path, as_role, tool, limit):
    as_role("frontend")
    assert "result" in LISTINGS[tool](limit)
