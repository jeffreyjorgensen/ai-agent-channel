from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_channel import server
from helpers import L


def _task(as_role) -> int:
    as_role("frontend")
    sent = server.send_message(
        to="backend",
        topic="fix login",
        body="...",
        action_required=True,
        kind="bug",
        work_status="needs_you",
    )
    return sent["id"]


def test_work_status_moves_on_same_message(db_path: Path, as_role):
    mid = _task(as_role)

    as_role("backend")
    res = server.set_work_status(message_id=mid, work_status="in_progress")
    assert res["previous"] == "needs_you"
    assert res["already_set"] is False

    # filter reflects the CURRENT status, not the one at send time
    assert L(server.list_messages(work_status="needs_you")) == []
    assert L(server.list_messages(work_status="in_progress"))[0]["id"] == mid

    server.set_work_status(message_id=mid, work_status="done_local", note="branch fix/login")
    events = L(server.message_history(message_id=mid))
    # The status passed to send_message is a transition by the SENDER and is
    # audited as one. It always counted as one — ownership and the stop hook
    # both read "the sender set it at send time" — it just was not written
    # down, so the axis that decides whose ball it is and the axis that
    # records why disagreed in silence.
    assert [(e["event"], e["role"]) for e in events] == [
        ("work_status:needs_you", "frontend"),
        ("work_status:in_progress", "backend"),
        ("work_status:done_local", "backend"),
    ]
    assert events[0]["note"] == "set at send time"
    assert events[2]["note"] == "branch fix/login"


def test_work_status_idempotent(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    server.set_work_status(message_id=mid, work_status="in_progress")
    again = server.set_work_status(message_id=mid, work_status="in_progress")
    assert again["already_set"] is True
    # send-time needs_you + the one in_progress; the repeat adds nothing
    assert [e["event"] for e in L(server.message_history(message_id=mid))] == [
        "work_status:needs_you",
        "work_status:in_progress",
    ]


def test_work_status_third_party_rejected(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("ops")
    with pytest.raises(PermissionError):
        server.set_work_status(message_id=mid, work_status="in_progress")


def test_work_status_invalid_value(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    with pytest.raises(ValueError, match="'work_status'"):
        server.set_work_status(message_id=mid, work_status="wip")


def test_done_requires_done_local_first(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    with pytest.raises(ValueError, match="done_local"):
        server.set_work_status(message_id=mid, work_status="done")


def test_done_must_be_confirmed_by_other_role(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    server.set_work_status(message_id=mid, work_status="done_local")
    # the executor cannot also confirm the merge
    with pytest.raises(PermissionError, match="other side"):
        server.set_work_status(message_id=mid, work_status="done")

    as_role("frontend")
    res = server.set_work_status(message_id=mid, work_status="done", note="merged into main")
    assert res["work_status"] == "done"


def test_done_local_at_send_time_counts_as_executor(db_path: Path, as_role):
    as_role("backend")
    sent = server.send_message(
        to="frontend", topic="report", body="done locally", work_status="done_local"
    )
    # sender declared done_local at send time → sender cannot confirm done
    with pytest.raises(PermissionError, match="other side"):
        server.set_work_status(message_id=sent["id"], work_status="done")
    as_role("frontend")
    server.set_work_status(message_id=sent["id"], work_status="done")


def test_blocked_with_blocked_by(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    blocker = server.send_message(
        to="frontend", topic="need schema", body="waiting for the schema", action_required=True
    )
    server.set_work_status(message_id=mid, work_status="blocked", blocked_by=blocker["id"])
    msg = L(server.list_messages(work_status="blocked"))[0]
    assert msg["blocked_by"] == blocker["id"]

    # leaving blocked clears the pointer
    server.set_work_status(message_id=mid, work_status="in_progress")
    assert L(server.list_messages(work_status="in_progress"))[0]["blocked_by"] is None


def test_blocked_by_validation(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    with pytest.raises(ValueError, match="only valid with"):
        server.set_work_status(message_id=mid, work_status="in_progress", blocked_by=mid)
    with pytest.raises(ValueError, match="not found"):
        server.set_work_status(message_id=mid, work_status="blocked", blocked_by=9999)


def test_blocked_by_must_be_resolvable(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    # non-action_required blocker: nothing can ever resolve it → rejected
    fyi = server.send_message(to="frontend", topic="fyi", body="x")
    with pytest.raises(ValueError, match="not action_required"):
        server.set_work_status(message_id=mid, work_status="blocked", blocked_by=fyi["id"])
    # already-resolved blocker is not a blocker
    done = server.send_message(to="frontend", topic="old", body="x", action_required=True)
    as_role("frontend")
    server.resolve_message(message_id=done["id"])
    as_role("backend")
    with pytest.raises(ValueError, match="already resolved"):
        server.set_work_status(message_id=mid, work_status="blocked", blocked_by=done["id"])


def test_delete_open_obligation_rejected(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    # deleting an open debt would silently close it → rejected for both parties
    with pytest.raises(ValueError, match="open obligation"):
        server.delete_message(message_id=mid)
    server.resolve_message(message_id=mid, resolution_note="done")
    # the verification window is protected too: resolved-but-unconfirmed
    # still sits in the author's resolved_for_you
    with pytest.raises(ValueError, match="not yet confirmed"):
        server.delete_message(message_id=mid)
    as_role("frontend")
    server.confirm_resolution(message_id=mid)
    as_role("backend")
    assert server.delete_message(message_id=mid)["deleted"] is True


def test_channel_status_in_progress(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    server.set_work_status(message_id=mid, work_status="in_progress")
    status = server.channel_status()
    assert [t["id"] for t in status["in_progress"]] == [mid]
    assert status["counts"]["in_progress"] == 1
    assert status["counts"]["open_obligations"] == 1


def test_needs_you_surfaces_to_the_other_side(db_path: Path, as_role):
    # frontend sent the task with work_status="needs_you" → the ball is at
    # backend, regardless of message direction
    mid = _task(as_role)
    as_role("backend")
    status = server.channel_status()
    assert [t["id"] for t in status["needs_you"]] == [mid]
    assert status["counts"]["needs_you"] == 1
    # ...and never at the side that set it
    as_role("frontend")
    assert server.channel_status()["needs_you"] == []


def test_needs_you_ball_moves_back_on_same_status(db_path: Path, as_role):
    mid = _task(as_role)
    # backend throws the ball back by re-setting needs_you on the SAME message:
    # same value from the OTHER role is a real transition, not an idempotent no-op
    as_role("backend")
    res = server.set_work_status(message_id=mid, work_status="needs_you", note="question")
    assert res["already_set"] is False
    assert server.channel_status()["needs_you"] == []
    as_role("frontend")
    assert [t["id"] for t in server.channel_status()["needs_you"]] == [mid]
    # but a retry by the same role is still a no-op
    again = server.set_work_status(message_id=mid, work_status="needs_you")
    assert again["already_set"] is False  # frontend != backend (last setter)
    final = server.set_work_status(message_id=mid, work_status="needs_you")
    assert final["already_set"] is True


def test_awaiting_done_surfaces_to_confirmer(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    server.set_work_status(message_id=mid, work_status="done_local")
    # the executor does not wait on themselves
    assert server.channel_status()["awaiting_done"] == []
    # the other side is asked to confirm
    as_role("frontend")
    status = server.channel_status()
    assert [t["id"] for t in status["awaiting_done"]] == [mid]
    assert status["counts"]["awaiting_done"] == 1
    # confirming clears the list
    server.set_work_status(message_id=mid, work_status="done")
    assert server.channel_status()["awaiting_done"] == []


def test_in_progress_belongs_to_whoever_set_it(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    server.set_work_status(message_id=mid, work_status="in_progress")
    # "what you left unfinished" follows the last transition's author,
    # not the addressee
    assert [t["id"] for t in server.channel_status()["in_progress"]] == [mid]
    as_role("frontend")
    assert server.channel_status()["in_progress"] == []


def test_obligations_and_acks_carry_age(db_path: Path, as_role):
    _task(as_role)
    as_role("frontend")
    server.send_message(
        to="backend", topic="p", body="...", kind="proc", pin_key=None, about_message_id=None
    )
    as_role("backend")
    assert L(server.open_obligations())[0]["age_days"] == 0
    assert L(server.awaiting_ack())[0]["age_days"] == 0


def test_resolve_blocker_surfaces_unblocked(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    blocker = server.send_message(
        to="frontend", topic="need schema", body="waiting for the schema", action_required=True
    )
    server.set_work_status(message_id=mid, work_status="blocked", blocked_by=blocker["id"])

    as_role("frontend")
    res = server.resolve_message(message_id=blocker["id"], resolution_note="schema in #N")
    assert res["unblocked"] == [mid]

    # the blocked task also surfaces in its owner's channel_status
    as_role("backend")
    status = server.channel_status()
    assert [u["id"] for u in status["unblocked"]] == [mid]
    assert status["unblocked"][0]["blocked_by"] == blocker["id"]

    # resuming the task clears the hint
    server.set_work_status(message_id=mid, work_status="in_progress")
    assert server.channel_status()["unblocked"] == []


def test_reopen_blocker_moves_task_back_to_blocked(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    blocker = server.send_message(
        to="frontend", topic="need schema", body="...", action_required=True
    )
    server.set_work_status(message_id=mid, work_status="blocked", blocked_by=blocker["id"])

    as_role("frontend")
    server.resolve_message(message_id=blocker["id"], resolution_note="schema in #N")
    as_role("backend")
    assert [u["id"] for u in server.channel_status()["unblocked"]] == [mid]

    # blocker reopened BEFORE the task was resumed: unblocked is computed,
    # not materialised — the task drops back to blocked, no stale hint
    as_role("frontend")
    server.reopen_message(message_id=blocker["id"], reason="the fix is incomplete")
    as_role("backend")
    status = server.channel_status()
    assert status["unblocked"] == []
    assert [b["id"] for b in status["blocked"]] == [mid]


def test_still_blocked_not_surfaced(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    blocker = server.send_message(
        to="frontend", topic="need schema", body="...", action_required=True
    )
    server.set_work_status(message_id=mid, work_status="blocked", blocked_by=blocker["id"])
    # blocker still open → listed as blocked, not unblocked
    status = server.channel_status()
    assert status["unblocked"] == []
    assert [b["id"] for b in status["blocked"]] == [mid]


def test_blocked_without_message_blocker_stays_blocked(db_path: Path, as_role):
    mid = _task(as_role)
    as_role("backend")
    # blocked on something without a message id (human decision, external run)
    server.set_work_status(
        message_id=mid, work_status="blocked", note="waiting on the user decision"
    )
    status = server.channel_status()
    assert [b["id"] for b in status["blocked"]] == [mid]
    assert status["unblocked"] == []  # never auto-unblocks — lifted manually
    server.set_work_status(message_id=mid, work_status="in_progress")
    assert server.channel_status()["blocked"] == []


def test_get_thread_from_any_message(db_path: Path, as_role):
    as_role("frontend")
    root = server.send_message(to="backend", topic="q", body="question")
    as_role("backend")
    r1 = server.send_message(to="frontend", topic="re: q", body="reply", reply_to=root["id"])
    as_role("frontend")
    r2 = server.send_message(
        to="backend", topic="re: re: q", body="clarification", reply_to=r1["id"]
    )
    # unrelated message must not leak into the thread
    server.send_message(to="backend", topic="other", body="x")

    expected = [root["id"], r1["id"], r2["id"]]
    for entry in expected:
        thread = L(server.get_thread(message_id=entry))
        assert [m["id"] for m in thread] == expected


def test_get_thread_missing_message(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="not found"):
        L(server.get_thread(message_id=9999))
