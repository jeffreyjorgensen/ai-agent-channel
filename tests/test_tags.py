from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import server
from helpers import L


def test_send_with_kind_and_work_status(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(
        to="backend",
        topic="login 500",
        body="repro: ...",
        kind="bug",
        work_status="needs_you",
    )
    as_role("backend")
    msg = L(server.read_inbox())[0]
    assert msg["kind"] == "bug"
    assert msg["work_status"] == "needs_you"


def test_kind_and_work_status_default_null(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="plain", body="x")
    as_role("backend")
    msg = L(server.read_inbox())[0]
    assert msg["kind"] is None
    assert msg["work_status"] is None


def test_answer_kind_filterable(db_path: Path, as_role):
    as_role("frontend")
    q = server.send_message(to="backend", topic="q", body="?", kind="question")
    as_role("backend")
    server.send_message(to="frontend", topic="re: q", body="!", kind="answer", reply_to=q["id"])
    answers = L(server.list_messages(kind="answer"))
    assert len(answers) == 1
    assert answers[0]["reply_to"] == q["id"]


def test_terminal_work_statuses_accepted(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="merged", body="x", work_status="done")
    server.send_message(to="backend", topic="stuck", body="x", work_status="blocked")
    done = L(server.list_messages(work_status="done"))
    assert [m["topic"] for m in done] == ["merged"]


def test_invalid_kind_rejected(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="'kind'"):
        server.send_message(to="backend", topic="t", body="b", kind="bugz")


def test_invalid_work_status_rejected(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="'work_status'"):
        server.send_message(to="backend", topic="t", body="b", work_status="wip")


def test_list_messages_filters_kind_and_work_status(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="bug-1", body="x", kind="bug", work_status="needs_you")
    server.send_message(to="backend", topic="feat-1", body="x", kind="feat", work_status="proposed")
    as_role("backend")
    server.send_message(to="frontend", topic="bug-2", body="x", kind="bug", work_status="needs_you")

    bugs = L(server.list_messages(kind="bug"))
    assert {m["topic"] for m in bugs} == {"bug-1", "bug-2"}

    # acceptance criterion: only messages waiting on backend
    needs_be = L(server.list_messages(work_status="needs_you", to_role="backend"))
    assert [m["topic"] for m in needs_be] == ["bug-1"]


def test_list_messages_invalid_filter_values_rejected(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="'status'"):
        L(server.list_messages(status="closed"))
    with pytest.raises(ValueError, match="'kind'"):
        L(server.list_messages(kind="task"))
    with pytest.raises(ValueError, match="'work_status'"):
        L(server.list_messages(work_status="wip"))
