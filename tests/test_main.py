"""`ai-agent-channel`: which transport the arguments select."""

from __future__ import annotations

import pytest

import ai_agent_channel.http as http_module
from ai_agent_channel import __main__ as entry


@pytest.fixture
def served(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(entry, "serve", lambda: calls.append("stdio"))
    monkeypatch.setattr(http_module, "serve_http", lambda **kw: calls.append(kw))
    return calls


def test_default_is_stdio(served):
    entry.main([])
    assert served == ["stdio"]


def test_http_uses_loopback_defaults(served):
    entry.main(["--http"])
    assert served == [{"host": "127.0.0.1", "port": 8765}]


def test_http_host_and_port(served):
    entry.main(["--http", "--host", "0.0.0.0", "--port", "9000"])
    assert served == [{"host": "0.0.0.0", "port": 9000}]


def test_bad_port_is_a_usage_error(served, capsys):
    with pytest.raises(SystemExit) as exit_:
        entry.main(["--http", "--port", "eighty"])
    assert exit_.value.code == 2
    assert served == []
