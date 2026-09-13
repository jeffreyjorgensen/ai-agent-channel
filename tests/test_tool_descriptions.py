"""What the tool descriptions and the release notice promise matches what the
tools do, and they carry no anecdotes."""

from __future__ import annotations

import re

from ai_agent_channel import release, server
from ai_agent_channel.tools import waiting

READ_ONLY = {
    "list_messages",
    "search_messages",
    "open_obligations",
    "ready_work",
    "awaiting_ack",
    "get_thread",
    "get_content",
    "message_history",
    "pin_get",
    "pin_list",
    "pin_history",
    "get_acknowledgements",
    "list_roles",
    "wait_for_reply",
    "wait_for_mail",
    "server_build",
    "get_protocol",
    "get_charter_template",
    "list_channels",
}


async def _descriptions() -> dict[str, str]:
    return {t.name: t.description or "" for t in await server.mcp.list_tools()}


async def test_read_only_tools_say_so_and_writers_do_not():
    for t in await server.mcp.list_tools():
        hint = bool(t.annotations and t.annotations.readOnlyHint)
        assert hint == (t.name in READ_ONLY), t.name


async def test_a_void_comes_from_a_voter_not_the_author():
    tools = await _descriptions()
    assert "void by its author" not in tools["pin_set"]
    assert "voter" in tools["pin_set"] and "delete_message" in tools["pin_set"]
    notice = " ".join(item["do"] for item in release.WHATS_NEW)
    assert "author or a voter" not in notice


def test_the_wide_reply_notice_names_the_kinds_it_applies_to():
    item = next(i for i in release.WHATS_NEW if "audience" in i["what"])
    assert "proc" in item["do"] and "status" in item["do"]
    t14 = dict((i, w) for i, _, w in release.SHIPPED)["T-14"]
    assert "audience" in t14


async def test_channel_status_names_counts_and_where_the_lists_are():
    text = (await _descriptions())["channel_status"]
    assert "NUMBERS" in text
    for tool in ("read_inbox", "open_obligations", "awaiting_ack"):
        assert tool in text


async def test_no_anecdotes_in_agent_facing_text():
    tools = await _descriptions()
    corpus = " ".join(tools.values()) + " ".join(i["do"] for i in release.WHATS_NEW)
    corpus += " ".join(w for _, w in release.NOT_SHIPPED) + server.server_build()["source"]
    for phrase in (
        "Russian",
        "merchant_id",
        "ORDER-2291",
        "survived real",
        "just was not written down",
        "exactly as before",
        "first-press",
        "Say so if you still want it",
        "written by the teams",
    ):
        assert phrase not in corpus, phrase
    # a message is a message; 'letters/digits' in a slug rule is fine
    assert not re.search(r"\bletter\b", corpus)


async def test_wait_descriptions_follow_the_cap():
    tools = await _descriptions()
    for name in ("wait_for_reply", "wait_for_mail"):
        assert f"{waiting.WAIT_CAP_S}s" in tools[name]
    assert waiting.WAIT_FOR_REPLY_CAP_S == waiting.WAIT_CAP_S


async def test_board_link_and_add_role_describe_what_a_role_can_do():
    tools = await _descriptions()
    assert "key_expires_at" in tools["board_link"]
    assert "call revoke_board_access" not in tools["board_link"]
    assert "ask the admin" in tools["board_link"]
    assert "voters='*'" in tools["add_role"]
