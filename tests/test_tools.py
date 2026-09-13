from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai_agent_channel import server
from ai_agent_channel.tools import waiting
from helpers import L


def test_send_and_read_roundtrip(db_path: Path, as_role):
    as_role("frontend")
    sent = server.send_message(to="backend", topic="hello", body="hi from frontend")
    assert "id" in sent
    assert "created_at" in sent

    as_role("backend")
    inbox = L(server.read_inbox())
    assert len(inbox) == 1
    msg = inbox[0]
    assert msg["from"] == "frontend"
    assert msg["to"] == "backend"
    assert msg["topic"] == "hello"
    assert msg["body"] == "hi from frontend"
    assert msg["action_required"] is False
    assert msg["read_at"] is None


def test_send_message_action_required_flag(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="urgent", body="please ack", action_required=True)
    as_role("backend")
    inbox = L(server.read_inbox())
    assert inbox[0]["action_required"] is True


def test_send_to_self_rejected(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="cannot send to self"):
        server.send_message(to="frontend", topic="x", body="y")


def test_send_without_role_rejected(db_path: Path, no_role):
    with pytest.raises(ValueError, match="AI_AGENT_CHANNEL_ROLE"):
        server.send_message(to="backend", topic="x", body="y")


def test_read_inbox_without_role_rejected(db_path: Path, no_role):
    with pytest.raises(ValueError, match="AI_AGENT_CHANNEL_ROLE"):
        L(server.read_inbox())


def test_read_inbox_only_returns_my_messages(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="for-backend", body="b")
    as_role("backend")
    server.send_message(to="frontend", topic="for-frontend", body="f")

    as_role("frontend")
    inbox = L(server.read_inbox())
    assert len(inbox) == 1
    assert inbox[0]["topic"] == "for-frontend"

    as_role("backend")
    inbox = L(server.read_inbox())
    assert len(inbox) == 1
    assert inbox[0]["topic"] == "for-backend"


def test_read_inbox_unread_only_default(db_path: Path, as_role):
    as_role("frontend")
    s1 = server.send_message(to="backend", topic="t1", body="b1")
    server.send_message(to="backend", topic="t2", body="b2")

    as_role("backend")
    server.mark_read(message_id=s1["id"])

    inbox_unread = L(server.read_inbox())
    assert len(inbox_unread) == 1
    assert inbox_unread[0]["topic"] == "t2"

    inbox_all = L(server.read_inbox(unread_only=False))
    assert len(inbox_all) == 2


def test_read_inbox_limit_keeps_newest(db_path: Path, as_role):
    as_role("frontend")
    for i in range(5):
        server.send_message(to="backend", topic=f"t{i}", body="x")

    as_role("backend")
    inbox = L(server.read_inbox(limit=3))
    # newest 3 messages, still in chronological order for reading
    assert [m["topic"] for m in inbox] == ["t2", "t3", "t4"]


def test_mark_read_happy(db_path: Path, as_role):
    as_role("frontend")
    sent = server.send_message(to="backend", topic="t", body="b")

    as_role("backend")
    res = server.mark_read(message_id=sent["id"])
    assert isinstance(res, dict)
    assert res["id"] == sent["id"]
    assert res["already_read"] is False
    assert res["read_at"]

    again = server.mark_read(message_id=sent["id"])
    assert isinstance(again, dict)
    assert again["already_read"] is True


def test_mark_read_foreign_rejected(db_path: Path, as_role):
    as_role("frontend")
    sent = server.send_message(to="backend", topic="t", body="b")

    as_role("frontend")
    with pytest.raises(PermissionError):
        server.mark_read(message_id=sent["id"])


def test_mark_read_missing_message(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="not found"):
        server.mark_read(message_id=9999)


def test_delete_message_by_recipient(db_path: Path, as_role):
    as_role("frontend")
    sent = server.send_message(to="backend", topic="t", body="b")

    as_role("backend")
    res = server.delete_message(message_id=sent["id"])
    assert res["id"] == sent["id"] and res["deleted"] is True

    assert L(server.read_inbox(unread_only=False)) == []
    assert L(server.list_messages()) == []


def test_delete_message_by_sender(db_path: Path, as_role):
    as_role("frontend")
    sent = server.send_message(to="backend", topic="t", body="b")

    res = server.delete_message(message_id=sent["id"])
    assert res["deleted"] is True

    as_role("backend")
    assert L(server.read_inbox()) == []


def test_delete_message_foreign_rejected(db_path: Path, as_role):
    as_role("frontend")
    sent = server.send_message(to="backend", topic="t", body="b")

    as_role("ops")
    with pytest.raises(PermissionError):
        server.delete_message(message_id=sent["id"])


def test_delete_message_missing(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="not found"):
        server.delete_message(message_id=9999)


def test_delete_message_is_tombstone(db_path: Path, as_role):
    as_role("frontend")
    parent = server.send_message(to="backend", topic="q", body="?")

    as_role("backend")
    reply = server.send_message(to="frontend", topic="re: q", body="!", reply_to=parent["id"])

    as_role("frontend")
    server.delete_message(message_id=parent["id"])

    # invisible to search/filters, reply chain untouched
    all_msgs = L(server.list_messages())
    assert len(all_msgs) == 1
    assert all_msgs[0]["id"] == reply["id"]
    assert all_msgs[0]["reply_to"] == parent["id"]

    # invisible to operations
    with pytest.raises(ValueError, match="not found"):
        server.delete_message(message_id=parent["id"])

    # but still present in the thread as a tombstone
    thread = L(server.get_thread(message_id=reply["id"]))
    assert [m["id"] for m in thread] == [parent["id"], reply["id"]]
    assert thread[0]["deleted_at"] is not None
    assert thread[1]["deleted_at"] is None


def test_mark_read_batch(db_path: Path, as_role):
    as_role("frontend")
    ids = [server.send_message(to="backend", topic=f"t{i}", body="b")["id"] for i in range(3)]

    as_role("backend")
    res = server.mark_read(message_ids=ids)
    assert isinstance(res, list)
    assert [r["id"] for r in res] == ids
    assert all(r["already_read"] is False for r in res)
    assert L(server.read_inbox()) == []


def test_mark_read_batch_is_atomic(db_path: Path, as_role):
    as_role("frontend")
    mine = server.send_message(to="backend", topic="ok", body="b")["id"]
    foreign = server.send_message(to="ops", topic="not-mine", body="b")["id"]

    as_role("backend")
    with pytest.raises(PermissionError, match="nothing was marked"):
        server.mark_read(message_ids=[mine, foreign])
    # the valid id was NOT marked — all-or-nothing
    assert len(L(server.read_inbox())) == 1


def test_mark_read_batch_with_an_unknown_id_marks_nothing(db_path: Path, as_role):
    as_role("frontend")
    mine = server.send_message(to="backend", topic="ok", body="b")["id"]

    as_role("backend")
    with pytest.raises(ValueError, match=f"message {mine + 999} not found; nothing was marked"):
        server.mark_read(message_ids=[mine, mine + 999])
    assert [m["id"] for m in L(server.read_inbox())] == [mine]  # still unread


def test_revise_message_checks_a_new_topic(db_path: Path, as_role):
    as_role("frontend")
    mid = server.send_message(to="backend", topic="t", body="b")["id"]
    with pytest.raises(ValueError, match="'topic' cannot be empty"):
        server.revise_message(message_id=mid, body="b2", topic="   ")
    with pytest.raises(ValueError, match="'topic' must be <= 80 characters"):
        server.revise_message(message_id=mid, body="b2", topic="x" * 81)
    server.revise_message(message_id=mid, body="b2", topic="  new topic  ")
    [row] = L(server.list_messages())
    assert (row["topic"], row["body"]) == ("new topic", "b2")


def test_revise_message_refusals_and_a_no_op(db_path: Path, as_role):
    as_role("frontend")
    mid = server.send_message(to="backend", topic="t", body="b")["id"]
    with pytest.raises(ValueError, match=f"message {mid + 999} not found"):
        server.revise_message(message_id=mid + 999, body="b2")
    with pytest.raises(ValueError, match="'body' is required"):
        server.revise_message(message_id=mid, body="")
    same = server.revise_message(message_id=mid, body="b")
    assert same["unchanged"] is True
    history = [e["event"] for e in L(server.message_history(message_id=mid))]
    assert "revision" not in history


def test_pin_key_length_is_capped(db_path: Path, as_role):
    from ai_agent_channel.tools.messaging import PIN_KEY_MAX

    as_role("frontend")
    long_key = "k" * (PIN_KEY_MAX + 1)
    with pytest.raises(ValueError, match=f"'pin_key' must be <= {PIN_KEY_MAX} characters"):
        server.send_message(
            to="backend",
            topic="t",
            body="b",
            kind="proc",
            pin_key=long_key,
            about_message_id=None,
        )


def test_body_and_body_ref_together_are_refused(db_path: Path, as_role):
    as_role("frontend")
    up = server.upload_content(text="document")["upload_id"]
    server.seal_content(upload_id=up)
    with pytest.raises(ValueError, match="either 'body' or 'body_ref', not both"):
        server.send_message(to="backend", topic="t", body="inline", body_ref=up)
    assert L(server.list_messages()) == []


def test_mark_read_requires_exactly_one_arg(db_path: Path, as_role):
    as_role("backend")
    with pytest.raises(ValueError, match="exactly one"):
        server.mark_read()
    with pytest.raises(ValueError, match="exactly one"):
        server.mark_read(message_id=1, message_ids=[1])


def test_topic_too_long(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="80"):
        server.send_message(to="backend", topic="x" * 81, body="b")


def test_empty_required_fields(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError):
        server.send_message(to="", topic="t", body="b")
    with pytest.raises(ValueError):
        server.send_message(to="backend", topic="", body="b")
    with pytest.raises(ValueError):
        server.send_message(to="backend", topic="t", body="")


def test_reply_to_threading(db_path: Path, as_role):
    as_role("frontend")
    parent = server.send_message(to="backend", topic="q", body="?")

    as_role("backend")
    reply = server.send_message(to="frontend", topic="re: q", body="!", reply_to=parent["id"])

    as_role("frontend")
    inbox = L(server.read_inbox())
    assert len(inbox) == 1
    assert inbox[0]["reply_to"] == parent["id"]
    assert inbox[0]["id"] == reply["id"]


def test_reply_to_unknown_message(db_path: Path, as_role):
    as_role("frontend")
    with pytest.raises(ValueError, match="reply_to"):
        server.send_message(to="backend", topic="x", body="y", reply_to=12345)


def test_list_messages_filters(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="api-bug", body="x")
    server.send_message(to="backend", topic="ui-bug", body="x")
    as_role("backend")
    server.send_message(to="frontend", topic="api-fix", body="x")

    all_msgs = L(server.list_messages())
    assert len(all_msgs) == 3

    api_msgs = L(server.list_messages(topic="api"))
    assert len(api_msgs) == 2
    assert {m["topic"] for m in api_msgs} == {"api-bug", "api-fix"}

    from_fe = L(server.list_messages(from_role="frontend"))
    assert len(from_fe) == 2

    to_fe = L(server.list_messages(to_role="frontend"))
    assert len(to_fe) == 1


def test_list_messages_text_searches_topic_and_body(db_path: Path, as_role):
    as_role("frontend")
    server.send_message(to="backend", topic="api contract", body="about merchant_id and the fields")
    server.send_message(to="backend", topic="merchant_id nullability", body="question")
    server.send_message(to="backend", topic="other", body="nothing in common")

    hits = L(server.list_messages(text="merchant_id"))
    assert {m["topic"] for m in hits} == {"api contract", "merchant_id nullability"}
    assert L(server.list_messages(text="no such thing")) == []


def test_list_messages_unread_only(db_path: Path, as_role):
    as_role("frontend")
    s1 = server.send_message(to="backend", topic="t1", body="b1")
    server.send_message(to="backend", topic="t2", body="b2")
    as_role("backend")
    server.mark_read(message_id=s1["id"])

    unread = L(server.list_messages(unread_only=True))
    assert len(unread) == 1
    assert unread[0]["topic"] == "t2"


def test_wait_for_reply_returns_when_available(db_path: Path, as_role):
    as_role("frontend")
    parent = server.send_message(to="backend", topic="q", body="?")

    as_role("backend")
    reply = server.send_message(to="frontend", topic="re", body="!", reply_to=parent["id"])

    as_role("frontend")
    result = asyncio.run(
        server.wait_for_reply(message_id=parent["id"], timeout_s=2, poll_interval_s=0.1)
    )
    assert result["id"] == reply["id"]
    assert result["reply_to"] == parent["id"]
    assert result["body"] == "!"


def test_wait_for_reply_timeout(db_path: Path, as_role, fast_wait):
    as_role("frontend")
    parent = server.send_message(to="backend", topic="q", body="?")
    result = asyncio.run(
        server.wait_for_reply(message_id=parent["id"], timeout_s=1, poll_interval_s=0.2)
    )
    # timeout is a clean retryable result, not a null and not a client-side
    # tool-timeout error — nothing is lost, a late reply waits in the DB
    assert result["timed_out"] is True
    assert result["retry"] is True
    assert result["waited_s"] == 1


def test_wait_for_reply_caps_per_call_wait(db_path: Path, as_role, monkeypatch, fast_wait):
    # long waits are assembled from sub-MCP-timeout chunks: the server caps
    # a single call below the client tool timeout instead of dying on it
    assert waiting.WAIT_CAP_S < 60  # must stay under MCP_TOOL_TIMEOUT
    monkeypatch.setattr(waiting, "WAIT_CAP_S", 1)
    as_role("frontend")
    parent = server.send_message(to="backend", topic="q", body="?")
    result = asyncio.run(
        server.wait_for_reply(message_id=parent["id"], timeout_s=3600, poll_interval_s=0.2)
    )
    assert result["timed_out"] is True
    assert result["waited_s"] == 1  # clamped from 3600 to the (patched) cap


def test_wait_for_reply_picks_up_async_reply(db_path: Path, as_role, monkeypatch, fast_wait):
    as_role("frontend")
    parent = server.send_message(to="backend", topic="q", body="?")
    sent: dict[str, int] = {}

    async def driver():
        async def replier():
            await fast_wait.until_polled()  # the waiter is already waiting
            monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", "backend")
            sent["id"] = server.send_message(
                to="frontend", topic="re", body="late!", reply_to=parent["id"]
            )["id"]
            monkeypatch.setenv("AI_AGENT_CHANNEL_ROLE", "frontend")

        replier_task = asyncio.create_task(replier())
        result = await server.wait_for_reply(
            message_id=parent["id"], timeout_s=30, poll_interval_s=0.1
        )
        await replier_task
        return result

    result = asyncio.run(driver())
    assert result["id"] == sent["id"]
    assert result["body"] == "late!"


def test_get_protocol_returns_contract(db_path, as_role):
    as_role("frontend")
    from ai_agent_channel import server

    text = server.get_protocol()
    assert "The permission matrix" in text  # the permission matrix section
    assert "work_status" in text


def test_get_charter_template(db_path, as_role):
    as_role("frontend")
    from ai_agent_channel import server

    text = server.get_charter_template()
    assert "Team charter" in text
    assert "team-charter" in text
