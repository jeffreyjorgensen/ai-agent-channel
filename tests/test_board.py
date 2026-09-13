"""The read-only HTML board: what the human sees at /board."""

from __future__ import annotations

import httpx

from ai_agent_channel import board
from helpers import ADMIN_TOKEN, client, error_text, make_channel, payload


def get(url: str, **kwargs) -> httpx.Response:
    return httpx.get(url, follow_redirects=False, **kwargs)


async def board_session(base_url: str, channel: str) -> httpx.Client:
    """Open the board the way a human does: admin mints a one-time link, the
    browser exchanges it for a cookie, the cookie carries the rest."""
    async with client(base_url, ADMIN_TOKEN) as admin:
        link = payload(await admin.call_tool("board_link", {"channel": channel}))
    http = httpx.Client(base_url=base_url, follow_redirects=True)
    # Redeem with a throwaway client and carry the cookie over, so the
    # returned client is still unopened and the caller can `with` it.
    with httpx.Client(base_url=base_url) as opener:
        exchanged = opener.get(link["url"])
        assert exchanged.status_code == 303, exchanged.text
        http.cookies.update(opener.cookies)
    return http


def admin_get(base_url: str, path: str) -> httpx.Response:
    return get(base_url + path, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})


async def seed(base_url: str) -> dict:
    created = await make_channel(base_url, "board-demo")
    tokens = created["tokens"]
    async with client(base_url, tokens["frontend"]) as fe:
        payload(
            await fe.call_tool(
                "send_message",
                {
                    "to": "backend",
                    "topic": "add /users endpoint",
                    "body": "blocking the profile page",
                    "action_required": True,
                    "kind": "feat",
                },
            )
        )
    async with client(base_url, tokens["backend"]) as be:
        payload(
            await be.call_tool(
                "pin_set",
                {
                    "key": "notes",
                    "title": "Scratch notes",
                    "body": "nothing yet",
                    "version": "1",
                },
            )
        )
    return created


async def test_board_needs_a_viewing_link(http_server):
    resp = get(http_server + "/board")
    assert resp.status_code == 401
    assert "text/html" in resp.headers["content-type"]
    assert "board_link" in resp.text
    assert get(http_server + "/board?t=nope").status_code == 401


async def test_a_role_token_no_longer_opens_the_board(http_server):
    """THE point of the change: a role token is a WRITE key — it can pin,
    send and resolve. A viewing surface must not accept one, however neatly
    it is transported."""
    created = await seed(http_server)
    role_token = created["tokens"]["backend"]
    # not in a header…
    assert (
        get(
            http_server + "/board",
            headers={"Authorization": f"Bearer {role_token}"},
        ).status_code
        == 403
    )
    # …and not in a URL either
    assert get(http_server + "/board", params={"token": role_token}).status_code == 401
    assert get(http_server + "/board", params={"t": role_token}).status_code == 401


async def test_a_viewing_key_cannot_write_anything(http_server, tmp_path):
    """The red case they named: present a viewing key to pin_set and it must
    be refused. If it goes through, we renamed the old access type instead of
    creating a new one."""
    from ai_agent_channel import auth

    await seed(http_server)
    with auth.open_admin_db() as conn:
        issued = auth.create_view_token(conn, channel="board-demo")
    view_token = issued["token"]
    assert view_token.startswith("ccv_")  # a different kind, visibly

    # it does not even reach the MCP endpoint
    assert (
        httpx.post(
            http_server + "/mcp",
            json={},
            headers={"Authorization": f"Bearer {view_token}"},
        ).status_code
        == 403
    )

    # and the identity it resolves to has no role, so every mailbox tool
    # refuses it on the same path that refuses the admin token
    ident = auth.authenticate(view_token)
    assert ident is not None and ident.is_viewer and ident.role is None

    for endpoint in ("/status", "/hook-status"):
        assert (
            httpx.get(
                http_server + endpoint,
                headers={"Authorization": f"Bearer {view_token}"},
            ).status_code
            == 403
        )


async def test_the_link_is_single_use_and_leaves_a_cookie(http_server):
    await seed(http_server)
    async with client(http_server, ADMIN_TOKEN) as admin:
        link = payload(await admin.call_tool("board_link", {"channel": "board-demo"}))

    with httpx.Client(base_url=http_server) as c:
        first = c.get(link["url"], follow_redirects=False)
        assert first.status_code == 303  # exchanged, not rendered
        assert first.headers["location"] == "/board/board-demo"
        cookie = first.headers.get("set-cookie", "")
        assert "cc_board=" in cookie
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
        assert "Path=/board" in cookie
        assert first.headers.get("referrer-policy") == "no-referrer"
        assert "script-src" not in first.headers["content-security-policy"]
        assert "default-src 'none'" in first.headers["content-security-policy"]
        # the cookie carries the session from here on
        assert c.get("/board/board-demo").status_code == 200

    # …and the value left in browser history no longer opens anything
    with httpx.Client(base_url=http_server) as fresh:
        assert fresh.get(link["url"], follow_redirects=False).status_code == 401


async def test_viewer_sees_its_channel_and_no_other(http_server):
    await seed(http_server)
    other = await make_channel(http_server, "somebody-else")
    http = await board_session(http_server, "board-demo")
    with http:
        page = http.get("/board/board-demo")
        assert "add /users endpoint" in page.text  # open obligation
        assert "Scratch notes" in page.text  # pin
        assert http.get("/board/somebody-else").status_code == 403
    assert other["tokens"]


async def test_revoking_kills_open_board_sessions(http_server):
    await seed(http_server)
    http = await board_session(http_server, "board-demo")
    with http:
        assert http.get("/board/board-demo").status_code == 200
        async with client(http_server, ADMIN_TOKEN) as admin:
            revoked = payload(
                await admin.call_tool("revoke_board_access", {"channel": "board-demo"})
            )
        assert revoked["revoked"] >= 1
        # a lost phone stops working immediately, without touching the mailbox
        assert http.get("/board/board-demo").status_code == 401


async def test_admin_gets_an_index_and_can_open_any_channel(http_server):
    await seed(http_server)
    await make_channel(http_server, "second")
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    index = get(http_server + "/board", headers=admin)
    assert index.status_code == 200
    assert "/board/board-demo" in index.text
    assert "/board/second" in index.text
    assert get(http_server + "/board/second", headers=admin).status_code == 200
    assert get(http_server + "/board/nope", headers=admin).status_code == 404


async def test_board_auth_never_reaches_anything_else(http_server):
    """A board credential must not drive an MCP call or a status read."""
    await seed(http_server)
    async with client(http_server, ADMIN_TOKEN) as admin:
        link = payload(await admin.call_tool("board_link", {"channel": "board-demo"}))
    with httpx.Client(base_url=http_server) as c:
        c.get(link["url"], follow_redirects=True)
        assert c.post("/mcp", json={}).status_code == 401
        assert c.get("/hook-status").status_code == 401
        assert c.get("/status").status_code == 401


async def test_board_escapes_message_text(http_server):
    created = await make_channel(http_server, "xss")
    async with client(http_server, created["tokens"]["frontend"]) as fe:
        payload(
            await fe.call_tool(
                "send_message",
                {"to": "backend", "topic": "<script>alert(1)</script>", "body": "x"},
            )
        )
    http = await board_session(http_server, "xss")
    with http:
        resp = http.get("/board/xss")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_empty_channel_renders_without_data():
    html = board.render_board(
        "quiet",
        {
            "roles": ["a", "b"],
            "counts": {"a": {}, "b": {}},
            "open_obligations": [],
            "pins": [],
            "recent": [],
            "totals": {"messages": 0},
        },
    )
    assert "no open obligations" in html
    assert "nothing pinned" in html
    assert "no messages yet" in html


def test_the_board_explains_what_its_numbers_count():
    """A number on a page a human reads is a claim about a named role, and
    this board has already made a false one: 'to decide 33' when the role
    owed nothing. The label was fixed — the explanation belongs beside it."""
    html = board.render_board(
        "quiet",
        {
            "roles": ["a"],
            "counts": {"a": {"awaiting_ack": 3}},
            "open_obligations": [],
            "pins": [],
            "recent": [],
            "totals": {"messages": 0},
        },
    )
    assert "decisions pending" in html
    assert "what these numbers count" in html
    assert "measured incoming traffic" in html  # why the number changed


def test_the_board_says_which_build_it_is_watching():
    from ai_agent_channel import server

    html = board.render_board(
        "quiet",
        {
            "roles": ["a"],
            "counts": {"a": {}},
            "open_obligations": [],
            "pins": [],
            "recent": [],
            "totals": {"messages": 0},
        },
        build=server.BUILD,
        whats_new=server.WHATS_NEW,
    )
    assert f"build {server.BUILD}" in html
    assert "what changed in it" in html
    # the same lines the roles get, so the human and the agents read one
    # story — escaped, like every other piece of text on this page
    from html import escape

    assert escape(server.WHATS_NEW[0]["what"]) in html
    assert escape(server.WHATS_NEW[0]["do"]) in html


def test_a_board_without_build_info_still_renders():
    """The board is also rendered by tests and by any caller that has no
    server module in hand — the notice is additive, never required."""
    html = board.render_board(
        "quiet",
        {
            "roles": ["a"],
            "counts": {"a": {}},
            "open_obligations": [],
            "pins": [],
            "recent": [],
            "totals": {"messages": 0},
        },
    )
    assert "what changed in it" not in html
    assert "quiet" in html


# --- reading the conversation, not just its state ---------------------------


async def seed_conversation(base_url: str) -> tuple[dict, int]:
    created = await make_channel(base_url, "readable")
    async with client(base_url, created["tokens"]["frontend"]) as fe:
        task = payload(
            await fe.call_tool(
                "send_message",
                {
                    "to": "backend",
                    "topic": "withdrawal fails",
                    "body": "repro: guest session, step 3",
                    "action_required": True,
                    "kind": "bug",
                },
            )
        )
        payload(
            await fe.call_tool(
                "pin_set",
                # a non-reserved key: 'team-charter' would (correctly) demand the
                # whole consent round, which is not what this test is about
                {
                    "key": "release-notes",
                    "title": "Release notes",
                    "version": "v1",
                    "body": "Rule 1: we agree before we build.",
                },
            )
        )
    async with client(base_url, created["tokens"]["backend"]) as be:
        payload(
            await be.call_tool(
                "send_message",
                {
                    "to": "frontend",
                    "topic": "re: withdrawal",
                    "body": "need the exact payload",
                    "kind": "question",
                    "reply_to": task["id"],
                },
            )
        )
        payload(
            await be.call_tool(
                "set_work_status",
                {"message_id": task["id"], "work_status": "in_progress", "note": "reproduced"},
            )
        )
    return created, task["id"]


async def test_thread_page_shows_the_actual_texts(http_server):
    _created, task = await seed_conversation(http_server)
    http = await board_session(http_server, "readable")
    with http:
        resp = http.get(f"/board/readable/thread/{task}")
    assert resp.status_code == 200
    # both turns of the conversation, with their bodies
    assert "repro: guest session, step 3" in resp.text
    assert "need the exact payload" in resp.text
    # …and the transitions, so the state is readable next to the words
    assert "in_progress" in resp.text
    assert "reproduced" in resp.text


async def test_overview_links_into_the_thread(http_server):
    _created, task = await seed_conversation(http_server)
    http = await board_session(http_server, "readable")
    with http:
        overview = http.get("/board/readable")
    assert f"/board/readable/thread/{task}" in overview.text
    assert "/board/readable/pin/release-notes" in overview.text
    assert "/board/readable/feed" in overview.text


async def test_feed_reads_as_correspondence(http_server):
    await seed_conversation(http_server)
    http = await board_session(http_server, "readable")
    with http:
        resp = http.get("/board/readable/feed")
    assert resp.status_code == 200
    assert "repro: guest session, step 3" in resp.text
    assert "need the exact payload" in resp.text


async def test_pin_page_shows_body_and_digest(http_server):
    await seed_conversation(http_server)
    http = await board_session(http_server, "readable")
    with http:
        resp = http.get("/board/readable/pin/release-notes")
    assert resp.status_code == 200
    assert "Rule 1: we agree before we build." in resp.text
    assert "sha256" in resp.text
    assert "bytes" in resp.text


async def test_reading_pages_obey_the_same_channel_scope(http_server):
    """A role token must not read another channel's conversations either —
    the new pages are not a way around the boundary."""
    _created, task = await seed_conversation(http_server)
    other = await make_channel(http_server, "someone-else")
    http = await board_session(http_server, "readable")
    with http:
        for path in (
            f"/board/someone-else/thread/{task}",
            "/board/someone-else/feed",
            "/board/someone-else/pin/release-notes",
        ):
            assert http.get(path).status_code == 403, path
    assert other["tokens"]


async def test_message_bodies_are_escaped(http_server):
    """Bodies are written by agents, and an agent may be relaying text it did
    not write — the board must never render it as markup."""
    created = await make_channel(http_server, "escaping")
    async with client(http_server, created["tokens"]["frontend"]) as fe:
        sent = payload(
            await fe.call_tool(
                "send_message",
                {
                    "to": "backend",
                    "topic": "harmless topic",
                    "body": "<img src=x onerror=alert(1)>",
                },
            )
        )
    http = await board_session(http_server, "escaping")
    with http:
        for path in (f"/board/escaping/thread/{sent['id']}", "/board/escaping/feed"):
            resp = http.get(path)
            assert "<img src=x" not in resp.text
            assert "&lt;img" in resp.text


async def test_unknown_ids_answer_politely(http_server):
    await seed_conversation(http_server)
    http = await board_session(http_server, "readable")
    with http:
        missing = http.get("/board/readable/thread/99999")
        assert missing.status_code == 200 and "was not found" in missing.text
        assert "has never been pinned" in http.get("/board/readable/pin/nope").text


async def test_a_role_can_issue_a_link_for_its_own_channel(http_server):
    """So a human can ask their own agent for the dashboard instead of
    needing the admin token. Not an escalation: the role already has full
    read AND write on this channel, and a viewing key is strictly less."""
    created = await seed(http_server)
    async with client(http_server, created["tokens"]["backend"]) as be:
        link = payload(await be.call_tool("board_link", {}))
    assert link["channel"] == "board-demo"
    assert link["key_expires_at"]  # a key with a horizon

    with httpx.Client(base_url=http_server) as c:
        assert c.get(link["url"], follow_redirects=False).status_code == 303


async def test_a_role_cannot_issue_a_link_for_someone_elses_channel(http_server):
    created = await seed(http_server)
    other = await make_channel(http_server, "not-yours")
    async with client(http_server, created["tokens"]["backend"]) as be:
        text = error_text(await be.call_tool("board_link", {"channel": "not-yours"}))
    assert "its own channel" in text
    assert other["tokens"]


async def test_the_link_lives_long_enough_to_be_clicked(http_server):
    """The security property is SINGLE USE, not brevity — and 60s was short
    enough that the first real link expired before anyone opened it."""
    from ai_agent_channel import auth

    await seed(http_server)
    async with client(http_server, ADMIN_TOKEN) as admin:
        link = payload(await admin.call_tool("board_link", {"channel": "board-demo"}))
    assert link["expires_in_s"] == auth.BOARD_NONCE_TTL_S
    assert auth.BOARD_NONCE_TTL_S >= 120
