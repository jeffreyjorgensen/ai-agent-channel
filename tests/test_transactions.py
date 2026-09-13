"""A check and the write it justifies happen in one transaction.

Each race test pauses the first writer right after its check and lets a
second writer run on its own connection. With transactions the second one
waits for the first to commit and then sees its result; without them both
checks pass and both writes land.

No step of the proof is a timeout. The first writer resumes only once the
second has provably reached the contested point: it has STARTED its
``BEGIN IMMEDIATE`` (seen through the connection's trace callback, which
fires before SQLite tries the lock the paused first writer holds), or — in
code that checks outside a transaction — it has already completed its own
check. Either way the second check cannot have seen the first write unless
the transaction made it wait, so a missing transaction shows up as two
successes however slow the runner is. Every wait below is a hang guard.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ai_agent_channel import db, server
from ai_agent_channel.db import connection
from ai_agent_channel.tools.common import open_channel_db
from helpers import present

HANG_GUARD_S = 10


@dataclass
class _Race:
    checked: threading.Event = field(default_factory=threading.Event)
    second_arrived: threading.Event = field(default_factory=threading.Event)
    second_statements: list[str] = field(default_factory=list)


def _is_begin_immediate(sql: str) -> bool:
    return sql.lstrip().upper().startswith("BEGIN IMMEDIATE")


def _pause_first_caller(
    monkeypatch: pytest.MonkeyPatch, name: str, owner: ModuleType = db
) -> _Race:
    """Make the thread named 'first' stop after calling owner.<name> until the
    'second' thread has started its write transaction or finished its own
    call of <name>. `owner` is the module whose lookup of <name> the checked
    code goes through."""
    race = _Race()
    real = getattr(owner, name)

    def paused(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        me = threading.current_thread().name
        if me == "second":
            race.second_arrived.set()
        elif me == "first" and not race.checked.is_set():
            race.checked.set()
            assert race.second_arrived.wait(HANG_GUARD_S), "the second writer never arrived"
        return result

    real_connect = connection.connect

    def traced(path: Path) -> sqlite3.Connection:
        conn = real_connect(path)
        if threading.current_thread().name == "second":

            def on_statement(sql: str) -> None:
                race.second_statements.append(sql)
                if _is_begin_immediate(sql):
                    race.second_arrived.set()

            conn.set_trace_callback(on_statement)
        return conn

    monkeypatch.setattr(owner, name, paused)
    monkeypatch.setattr(connection, "connect", traced)
    return race


def _race(first: Callable[[], Any], second: Callable[[], Any], race: _Race) -> dict[str, Any]:
    results: dict[str, Any] = {}

    def run(label: str, fn: Callable[[], Any]) -> None:
        try:
            results[label] = fn()
        except Exception as exc:  # the loser's refusal is part of the result
            results[label] = exc

    t1 = threading.Thread(target=run, args=("first", first), name="first")
    t1.start()
    assert race.checked.wait(HANG_GUARD_S)
    t2 = threading.Thread(target=run, args=("second", second), name="second")
    t2.start()
    t1.join(HANG_GUARD_S)
    t2.join(HANG_GUARD_S)
    assert not t1.is_alive() and not t2.is_alive()
    # the contention was real: the second writer asked for the write lock
    assert any(_is_begin_immediate(s) for s in race.second_statements)
    return results


def test_two_resolves_of_one_debt_write_once(db_path: Path, monkeypatch):
    with db.open_db() as conn:
        debt = db.insert_message(
            conn,
            from_role="frontend",
            to_role="backend",
            topic="t",
            body="x",
            action_required=True,
            reply_to=None,
        )["id"]
    race = _pause_first_caller(monkeypatch, "fetch_message", db.obligations)

    def resolve(role: str, note: str) -> Callable[[], Any]:
        def go() -> Any:
            with db.open_db() as conn:
                return db.resolve_message(conn, message_id=debt, role=role, resolution_note=note)

        return go

    results = _race(resolve("backend", "fixed"), resolve("frontend", "withdrawn"), race)

    assert results["first"]["already_resolved"] is False
    assert results["second"]["already_resolved"] is True
    with db.open_db() as conn:
        events = [e for e in db.fetch_events(conn, message_id=debt) if e["event"] == "resolve"]
        assert len(events) == 1
        assert present(db.fetch_message(conn, debt))["resolution_note"] == "fixed"


def test_one_approval_cannot_write_two_pin_versions(db_path: Path, as_role, monkeypatch):
    as_role("frontend")
    proposal = server.send_message(
        to="backend",
        topic="notes",
        body="x",
        kind="proc",
        pin_key="notes",
        about_message_id=None,
    )["id"]
    as_role("backend")
    server.acknowledge(message_id=proposal, decision="agree")
    as_role("frontend")
    race = _pause_first_caller(monkeypatch, "approval_used")

    def write(version: str) -> Callable[[], Any]:
        return lambda: server.pin_set(
            key="notes",
            title="n",
            body=version,
            version=version,
            approved_by=proposal,
        )

    results = _race(write("v1"), write("v2"), race)

    assert isinstance(results["second"], ValueError)
    assert "already approved" in str(results["second"])
    assert [p["version"] for p in server.pin_history(key="notes")["result"]] == ["v1"]


def test_two_rounds_cannot_open_on_one_key(db_path: Path, as_role, monkeypatch):
    as_role("frontend")
    race = _pause_first_caller(monkeypatch, "open_rounds_for_pin")

    def propose(body: str) -> Callable[[], Any]:
        return lambda: server.send_message(
            to="backend",
            topic="glossary",
            body=body,
            kind="proc",
            pin_key="glossary",
            about_message_id=None,
        )

    results = _race(propose("a"), propose("b"), race)

    assert isinstance(results["second"], ValueError)
    assert "already has an open round" in str(results["second"])
    with db.open_db() as conn:
        assert len(db.open_rounds_for_pin(conn, key="glossary")) == 1


def test_a_failed_insert_leaves_no_partial_message(db_path: Path):
    with db.open_db() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_message(
                conn,
                from_role="frontend",
                to_role=db.BROADCAST,
                topic="t",
                body="x",
                action_required=False,
                reply_to=None,
                kind="status",
                recipients=["backend", "backend"],
            )
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_a_tool_that_refuses_half_way_writes_nothing(db_path: Path, as_role):
    """mark_read over a batch validates before writing; the transaction makes
    that hold even if a later step fails."""
    as_role("frontend")
    sent = server.send_message(to="backend", topic="t", body="x")["id"]
    as_role("backend")
    with pytest.raises(ValueError), open_channel_db(write=True) as conn:
        db.mark_read(conn, message_id=sent, role="backend")
        raise ValueError("refused after the first write")
    with db.open_db() as conn:
        assert db.delivery_read_at(conn, message_id=sent, role="backend") is None


def test_nested_transactions_roll_back_only_their_own_part(db_path: Path):
    with db.open_db() as conn:
        with db.transaction(conn):
            db.meta_set(conn, key="outer", value="1")
            with pytest.raises(RuntimeError), db.transaction(conn):
                db.meta_set(conn, key="inner", value="1")
                raise RuntimeError
        assert db.meta_get(conn, key="outer") == "1"
        assert db.meta_get(conn, key="inner") is None
        assert not conn.in_transaction
