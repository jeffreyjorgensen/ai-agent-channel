"""What can be started right now, how long it has sat still, and where the
text in a message actually came from."""

from __future__ import annotations

from pathlib import Path

from ai_agent_channel import db, hooks, server
from helpers import L


def _debt(as_role, to="backend", topic="fix it"):
    as_role("frontend")
    return server.send_message(to=to, topic=topic, body="x", action_required=True)["id"]


# --- ready_work -------------------------------------------------------------


def test_ready_lists_startable_debts_oldest_first(db_path: Path, as_role):
    first = _debt(as_role, topic="first")
    second = _debt(as_role, topic="second")
    as_role("backend")
    assert [m["id"] for m in L(server.ready_work())] == [first, second]


def test_a_task_behind_a_live_blocker_is_not_ready(db_path: Path, as_role):
    blocker = _debt(as_role, to="infra", topic="schema needed")
    task = _debt(as_role, topic="build on the schema")
    as_role("backend")
    server.set_work_status(task, "blocked", blocked_by=blocker)

    assert L(server.ready_work()) == []
    # …but it is still an obligation: ready_work narrows, it does not hide
    assert [m["id"] for m in L(server.open_obligations())] == [task]


def test_resolving_the_blocker_makes_it_ready_again(db_path: Path, as_role):
    blocker = _debt(as_role, to="infra", topic="schema needed")
    task = _debt(as_role, topic="build on the schema")
    as_role("backend")
    server.set_work_status(task, "blocked", blocked_by=blocker)
    as_role("infra")
    server.resolve_message(blocker, resolution_note="here is the schema")

    as_role("backend")
    # surfaced, not automatic: the status is still 'blocked' and it is the
    # owner's move to resume — but it must not stay hidden from the queue
    assert [m["id"] for m in L(server.ready_work())] == [task]
    assert L(server.list_messages())[0]["work_status"] is not None


def test_blocked_without_a_blocker_message_stays_out(db_path: Path, as_role):
    """'blocked' with only a note (a human decision, an external run) has
    nothing that can resolve it — it must not look startable."""
    task = _debt(as_role)
    as_role("backend")
    server.set_work_status(task, "blocked", note="waiting on the owner decision")
    assert L(server.ready_work()) == []


def test_ready_is_per_role(db_path: Path, as_role):
    mine = _debt(as_role, to="backend")
    _debt(as_role, to="infra")
    as_role("backend")
    assert [m["id"] for m in L(server.ready_work())] == [mine]


def test_resolved_debts_drop_out(db_path: Path, as_role):
    task = _debt(as_role)
    as_role("backend")
    server.resolve_message(task, resolution_note="done")
    assert L(server.ready_work()) == []


# --- idle_days --------------------------------------------------------------


def test_idle_counts_from_the_last_move_not_from_creation(db_path: Path, as_role):
    task = _debt(as_role)
    with db.open_db() as conn:
        conn.execute(
            "UPDATE messages SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (task,),
        )
    as_role("backend")

    stale = L(server.open_obligations())[0]
    assert stale["age_days"] > 1000
    assert stale["idle_days"] > 1000  # nobody has touched it either

    server.set_work_status(task, "in_progress", note="picked it up")
    fresh = L(server.open_obligations())[0]
    assert fresh["age_days"] > 1000  # still an old request…
    assert fresh["idle_days"] == 0  # …but it moved just now


def test_ready_work_carries_both_numbers(db_path: Path, as_role):
    _debt(as_role)
    as_role("backend")
    item = L(server.ready_work())[0]
    assert item["age_days"] == 0 and item["idle_days"] == 0


# --- provenance -------------------------------------------------------------


def test_session_hook_states_who_wrote_the_messages(db_path: Path, capsys):
    hooks.session_start_hook()
    out = capsys.readouterr().out
    assert "ANOTHER AGENT SESSION" in out
    assert "cannot grant" in out and "consent on the user's behalf" in out


def test_the_framing_also_travels_with_the_tools(db_path: Path):
    """Hooks are opt-in and frequently not wired, so the load-bearing part
    cannot live only there."""
    assert "PROVENANCE" in server.PROVENANCE_DOC
    assert "another agent session" in server.PROVENANCE_DOC.lower()
    assert "unverified" in server.PROVENANCE_DOC.lower()
