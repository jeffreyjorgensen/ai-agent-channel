"""HTTP transport: bearer auth + per-request identity around FastMCP.

Layout of the served app (all under one port, TLS terminates at the reverse
proxy — see deploy/):

- ``/healthz``      unauthenticated liveness probe;
- ``/hook-status``  role token → the stop-hook counts for that token's
  (channel, role), so remote sessions' hooks work without DB access;
- ``/status``       role token → the same role's FULL channel_summary, for
  the ``ai-agent-channel-status`` console command; kept separate from
  /hook-status, which runs on every stop and must stay thin. With
  ``?wait=N`` it long-polls: the request is held until something actionable
  exists for that role, which is what lets an external watcher wake an idle
  session promptly without a second protocol;
- ``/board``        read-only HTML view of a channel for the HUMAN (see
  board.py). Browsers cannot send an Authorization header when you paste a
  URL, so this is the one place that authenticates from a URL — and only
  with ``?t=``, a single-use nonce from ``board_link``, never a token. It is
  restricted to GET on /board so a URL can never drive an MCP call, and is
  answered with a ``Set-Cookie`` carrying a board session;
- everything else   (i.e. ``/mcp``) → FastMCP's streamable-http app.

The auth middleware is a pure ASGI wrapper (NOT BaseHTTPMiddleware): it must
run the downstream app in the same task so the ``CURRENT_IDENTITY``
contextvar set here is visible inside tool calls. For the same reason the
MCP app runs with ``stateless_http=True`` — stateful sessions dispatch tool
calls into a long-lived session task that would not inherit a per-request
contextvar. Stateless is also what lets the server sit behind a plain
reverse proxy with no sticky sessions.

DNS-rebinding protection (Host/Origin allowlist) is on when
``AI_AGENT_CHANNEL_ALLOWED_HOSTS`` names the public hostname(s), and off —
with a startup warning — when it is unset, because the hostname is not known
at build time and bearer auth does not depend on it.

``X-Forwarded-Proto`` is believed only from a loopback/private peer (the
reverse proxy on the same host or docker network) or when
``AI_AGENT_CHANNEL_TRUST_PROXY=1``; it decides the cookie's Secure flag.

Every SQLite access on a request path runs in a worker thread
(``asyncio.to_thread``, which carries the contextvars along): a slow disk or
a burst of bogus tokens must not stall the event loop that also answers
/healthz and every held long poll. The registry schema is migrated once at
startup, never per request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import sqlite3
import time
from typing import Any
from urllib.parse import parse_qs

from mcp.server.transport_security import TransportSecuritySettings

from . import auth, board, db, server

log = logging.getLogger(__name__)

HOST_DEFAULT = "127.0.0.1"
PORT_DEFAULT = 8765
BOARD_COOKIE = "cc_board"
BOARD_COOKIE_MAX_AGE = auth.BOARD_SESSION_TTL_S
ALLOWED_HOSTS_ENV = "AI_AGENT_CHANNEL_ALLOWED_HOSTS"
TRUST_PROXY_ENV = "AI_AGENT_CHANNEL_TRUST_PROXY"
STATUS_MAX_WAITERS_ENV = "AI_AGENT_CHANNEL_STATUS_MAX_WAITERS"
STATUS_MAX_WAITERS_PER_TOKEN_ENV = "AI_AGENT_CHANNEL_STATUS_MAX_WAITERS_PER_TOKEN"
# uvicorn's own variable for which peers may set X-Forwarded-For/-Proto
FORWARDED_ALLOW_IPS_ENV = "FORWARDED_ALLOW_IPS"
# An admin token shorter than this gets a startup warning (not a refusal: the
# live deployment's token predates the check and must keep starting).
ADMIN_TOKEN_MIN_LENGTH = 32
# Long-poll ceiling for /status?wait=N. Well under the idle timeouts of the
# things in front of us (nginx 60s by default, Cloudflare 100s), so a held
# request always ends as our own empty answer rather than someone else's
# gateway error.
STATUS_WAIT_CAP_S = 50
STATUS_POLL_INTERVAL_S = 1.0
# The board is static HTML with one inline <style> and no script; the policy
# says so, so injected markup that slips past escape() still cannot run.
BOARD_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


# Each held long poll is a DB read per second; a token holder opening
# thousands of them is a cheap way to exhaust the server. The global cap
# protects the server, the per-token cap protects the other channels: without
# it one token (or one looping watcher) takes every slot on the host.
STATUS_MAX_WAITERS = _env_int(STATUS_MAX_WAITERS_ENV, 64)
STATUS_MAX_WAITERS_PER_TOKEN = _env_int(STATUS_MAX_WAITERS_PER_TOKEN_ENV, 8)


async def _respond(
    send,
    status: int,
    body: bytes,
    content_type: bytes,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    headers = [
        (b"content-type", content_type),
        (b"content-length", str(len(body)).encode()),
    ]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _respond_json(
    send,
    status: int,
    payload: dict[str, Any],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    await _respond(send, status, json.dumps(payload).encode(), b"application/json", extra_headers)


async def _respond_html(
    send, status: int, html: str, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    await _respond(send, status, html.encode(), b"text/html; charset=utf-8", extra_headers)


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1").strip()
    return ""


def _bearer(scope) -> str:
    text = _header(scope, b"authorization")
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    return ""


def _is_board(scope) -> bool:
    """Board routes are the only ones that may authenticate from a URL or a
    cookie, and only for reads — keep that gate in one place."""
    path = scope["path"].rstrip("/") or "/"
    return scope.get("method") == "GET" and (path == "/board" or path.startswith("/board/"))


def _query(scope, name: str) -> str:
    qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    return (qs.get(name) or [""])[0].strip()


def _cookie(scope, name: str) -> str:
    for part in _header(scope, b"cookie").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""


def _trusted_proxy(scope) -> bool:
    """Forwarded headers are only as honest as the peer that sent them: the
    proxy in front of us (loopback or a private/docker network), or whatever
    the operator vouches for with AI_AGENT_CHANNEL_TRUST_PROXY=1.

    uvicorn applies its own proxy-header rule before this runs (see
    serve_http): from a peer in FORWARDED_ALLOW_IPS it has already replaced
    ``client`` and ``scheme`` with the forwarded values, so a loopback proxy
    shows up here as the real client with scheme https."""
    if os.environ.get(TRUST_PROXY_ENV, "").strip() == "1":
        return True
    client = scope.get("client")
    if not client:
        return False
    try:
        addr = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def _is_secure(scope) -> bool:
    """Behind the proxy the connection to us is plain HTTP; what matters is
    how the browser reached the proxy."""
    if scope.get("scheme") == "https":
        return True
    return _trusted_proxy(scope) and _header(scope, b"x-forwarded-proto").lower() == "https"


def allowed_hosts() -> list[str]:
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _allowed_origins(hosts: list[str]) -> list[str]:
    return [f"{scheme}://{h}" for h in hosts for scheme in ("https", "http")]


def _matches(value: str, allowed: list[str]) -> bool:
    """Exact match, or ``name:*`` for any port — the same rule the MCP
    transport applies, so /board and /mcp cannot disagree. The wildcard
    stands for a port, so only digits may follow the colon: ``127.0.0.1:*``
    must not admit ``127.0.0.1:evil.example``."""
    value = value.lower()
    if value in allowed:
        return True
    for a in allowed:
        if a.endswith(":*") and value.startswith(a[:-1]):
            port = value[len(a) - 1 :]
            if port.isascii() and port.isdigit():
                return True
    return False


def _board_headers(scope, cookie_value: str | None = None) -> list[tuple[bytes, bytes]]:
    # no-referrer because the board has links, and a Referer is the third
    # way a URL leaks after history and logs.
    headers = [
        (b"referrer-policy", b"no-referrer"),
        (b"content-security-policy", BOARD_CSP.encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"cache-control", b"no-store"),
    ]
    if cookie_value is not None:
        # Lax, not Strict. A board link is clicked in a chat or mail client,
        # i.e. the navigation starts on another site. Browsers treat the
        # whole redirect chain as cross-site, so a Strict cookie set by the
        # redeem answer is withheld from the 303's target and the first
        # click lands on a 401. Lax is sent on top-level GET navigations,
        # which is the only thing the board serves: it is GET-only and
        # read-only (_is_board), with no state-changing request for a
        # cross-site POST to forge, so Strict bought no CSRF protection.
        # Lax still keeps the cookie off cross-site subresource and iframe
        # requests (and X-Frame-Options/frame-ancestors forbid framing).
        flags = f"HttpOnly; SameSite=Lax; Path=/board; Max-Age={BOARD_COOKIE_MAX_AGE}"
        if _is_secure(scope):
            flags += "; Secure"
        headers.append((b"set-cookie", f"{BOARD_COOKIE}={cookie_value}; {flags}".encode()))
    return headers


def _int_param(scope, name: str, default: int) -> int:
    raw = parse_qs(scope.get("query_string", b"").decode("latin-1")).get(name)
    try:
        return max(0, int(raw[0])) if raw else default
    except (TypeError, ValueError):
        return default


def _duration(seconds: int) -> str:
    if seconds >= 120 and seconds % 60 == 0:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


async def _wait_for_disconnect(receive) -> None:
    """Return once the client has gone away. For a GET the first message is
    the (empty) request body; after that uvicorn's receive() blocks until
    the connection closes and then answers http.disconnect."""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


def _read_status(ident: auth.Identity) -> dict[str, Any]:
    assert ident.db_path is not None and ident.role is not None  # noqa: S101 - checked by caller
    with db.open_db(ident.db_path) as conn:
        summary = db.channel_summary(conn, role=ident.role)
        summary["debts"] = _debts(conn, ident.role)
    return summary


def _debts(conn, role: str) -> list[dict[str, Any]]:
    """Open obligations as headers — channel_summary only counts them, and a
    checker that reports "you owe 1 thing" without saying which is barely
    better than silence."""
    return db.project(
        db.list_messages(
            conn,
            topic=None,
            from_role=None,
            to_role=role,
            unread_only=False,
            since=None,
            limit=100,
            status="open",
        ),
        db.MESSAGE_HEADERS,
    )


class AuthMiddleware:
    """Authenticates every HTTP request and pins the resolved Identity to the
    current task's context for the duration of the request."""

    def __init__(self, app, allowed_hosts: list[str] | None = None) -> None:
        self.app = app
        self.allowed_hosts = list(allowed_hosts or [])
        self.allowed_origins = _allowed_origins(self.allowed_hosts)
        # held long polls, in total and per bearer token (by hash)
        self._waiters = 0
        self._waiters_by_token: dict[str, int] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":  # lifespan etc. pass through untouched
            await self.app(scope, receive, send)
            return
        path = scope["path"].rstrip("/") or "/"
        if path == "/healthz":
            await _respond_json(send, 200, {"ok": True})
            return
        if self.allowed_hosts:
            if not _matches(_header(scope, b"host"), self.allowed_hosts):
                await _respond_json(send, 421, {"error": "invalid Host header"})
                return
            origin = _header(scope, b"origin")
            if origin and not _matches(origin, self.allowed_origins):
                await _respond_json(send, 403, {"error": "invalid Origin header"})
                return
        board_request = _is_board(scope)

        # A one-time value in the URL is exchanged for a cookie and burned,
        # so the durable secret never enters browser history, proxy logs or
        # a Referer. The redirect is what removes it from the address bar.
        if board_request and _query(scope, "t"):
            await self._redeem(scope, send)
            return

        # a registry lookup per request, off the event loop
        ident = await asyncio.to_thread(self._identify, scope, board_request)
        if ident is None:
            if board_request:
                await _respond_html(
                    send,
                    401,
                    board.render_message(
                        "Cannot open this",
                        "You need a viewer link: ask the channel's "
                        "administrator to run board_link(<channel>). A role "
                        "token is not accepted here — it can write, and this "
                        "page only shows.",
                    ),
                    _board_headers(scope),
                )
            else:
                await _respond_json(send, 401, {"error": "missing or invalid bearer token"})
            return

        # A viewer is read-only BY CONSTRUCTION (no role, so every mailbox
        # tool refuses it) — but it must also never reach /mcp at all, so
        # that the guarantee does not rest on auditing every tool.
        if ident.is_viewer and not board_request:
            await _respond_json(send, 403, {"error": "view tokens are only valid for GET /board"})
            return
        # …and the converse, which is the whole point of the separate kind:
        # a ROLE token is a write key. It must not open a viewing surface
        # even when correctly presented in a header — otherwise the board is
        # still one leaked page away from someone else's mailbox.
        if board_request and not (ident.is_admin or ident.is_viewer):
            await _respond_html(
                send,
                403,
                board.render_message(
                    "A role token is not accepted here",
                    "It can write to the channel, and this page only shows. "
                    "You need a viewer link: board_link(<channel>).",
                ),
                _board_headers(scope),
            )
            return

        token = auth.CURRENT_IDENTITY.set(ident)
        try:
            if path == "/hook-status":
                await self._hook_status(ident, send)
            elif path == "/status":
                await self._status(scope, receive, ident, send)
            elif board_request:
                await self._board(scope, path, ident, send)
            else:
                await self.app(scope, receive, send)
        finally:
            auth.CURRENT_IDENTITY.reset(token)

    @staticmethod
    def _identify(scope, board_request: bool) -> auth.Identity | None:
        """Who is calling. The board additionally accepts its own cookie —
        and ONLY its own cookie, never a role token pasted into a URL."""
        bearer = _bearer(scope)
        if bearer:
            return auth.authenticate(bearer)
        if board_request:
            held = _cookie(scope, BOARD_COOKIE)
            if held:
                return auth.viewer_from_session(held)
        return None

    @staticmethod
    async def _redeem(scope, send) -> None:
        """Exchange a one-time link value for a board session cookie, then
        redirect so the address bar no longer holds it."""

        def redeem() -> dict[str, str] | None:
            with auth.open_admin_db() as conn:
                return auth.redeem_board_nonce(conn, nonce=_query(scope, "t"))

        opened = await asyncio.to_thread(redeem)
        if opened is None:
            await _respond_html(
                send,
                401,
                board.render_message(
                    "This link is no longer valid",
                    "Viewer links are single-use and live for "
                    f"{_duration(auth.BOARD_NONCE_TTL_S)}. "
                    "Ask for a new one: board_link(<channel>).",
                ),
                _board_headers(scope),
            )
            return
        # the key's own channel (a registry slug), never the request path
        target = f"/board/{opened['channel']}"
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [
                    (b"location", target.encode()),
                    (b"content-length", b"0"),
                    *_board_headers(scope, opened["session"]),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def _board(self, scope, path: str, ident: auth.Identity, send) -> None:
        # reading the registry and the mailbox and rendering the page are all
        # blocking work: one thread hop for the whole page
        status, page = await asyncio.to_thread(self._board_page, path, ident)
        await _respond_html(send, status, page, _board_headers(scope))

    @classmethod
    def _board_page(cls, path: str, ident: auth.Identity) -> tuple[int, str]:
        # /board | /board/<channel> | /board/<channel>/{feed,thread/<id>,pin/<key>}
        tail = path[len("/board/") :] if path.startswith("/board/") else ""
        parts = [p for p in tail.split("/") if p]
        wanted = parts[0] if parts else ""
        view = parts[1:] if len(parts) > 1 else []

        if not wanted:
            if ident.is_admin:
                with auth.open_admin_db() as conn:
                    channels = auth.list_channels(conn)
                return 200, board.render_index(channels)
            wanted = ident.channel or ""
        if not ident.is_admin and wanted != ident.channel:
            # A viewer is scoped to its channel — no peeking at others.
            return 403, board.render_message(
                "Not your channel",
                f"This token belongs to '{ident.channel}'.",
            )
        # for a viewer `wanted` is its own channel (checked above), so this is
        # the same path its identity carries
        db_path = auth.channel_db_path(wanted)
        with auth.open_admin_db() as conn:
            match = [c for c in auth.list_channels(conn) if c["name"] == wanted]
        if not match:
            return 404, board.render_message("No such channel", f"'{wanted}' is not active.")
        with db.open_db(db_path) as conn:
            return 200, cls._board_view(conn, wanted, view, roles=match[0]["roles"])

    @staticmethod
    def _board_view(conn, channel: str, view: list[str], *, roles: list[str]) -> str:
        """Which page of a channel to render. Read-only throughout: the board
        shows the human what the agents are doing, it never acts for them."""
        if view and view[0] == "feed":
            return board.render_feed(channel, db.board_feed(conn))
        if len(view) == 2 and view[0] == "thread" and view[1].isdigit():
            return board.render_thread(channel, db.board_thread(conn, message_id=int(view[1])))
        if len(view) == 2 and view[0] == "pin":
            return board.render_pin(channel, view[1], db.pin_history(conn, key=view[1]))
        # The human watching four agents has to learn that the server moved
        # from the same page they are reading, or a counter changes meaning
        # under them with no notice anywhere.
        return board.render_board(
            channel,
            db.board_snapshot(conn, roles=roles),
            build=server.BUILD,
            whats_new=server.WHATS_NEW,
        )

    async def _status(self, scope, receive, ident: auth.Identity, send) -> None:
        """The full role state, for the console command (cli.py).

        /hook-status stays a deliberately thin subset — it runs on every
        stop and must not grow — so the richer view gets its own route
        instead of being bolted onto it.
        """
        if ident.role is None or ident.db_path is None:
            await _respond_json(send, 403, {"error": "status needs a channel role token"})
            return
        # ?wait=N turns this into a long poll: hold the request until
        # something actionable exists for this role, or N seconds elapse.
        # That is what makes the watcher near-instant without a second
        # protocol — one held HTTP request beats both a tight poll loop and
        # a WebSocket, and it traverses a reverse proxy and Cloudflare as
        # ordinary traffic. The cap stays below their idle timeouts.
        wait_s = min(_int_param(scope, "wait", 0), STATUS_WAIT_CAP_S)
        if wait_s == 0:
            summary = await asyncio.to_thread(_read_status, ident)
            await self._status_answer(send, ident, summary, wait_s)
            return
        key = ident.token_hash or ""
        # no await between the checks and the increments: one event loop,
        # so the counts cannot race
        if (
            self._waiters >= STATUS_MAX_WAITERS
            or self._waiters_by_token.get(key, 0) >= STATUS_MAX_WAITERS_PER_TOKEN
        ):
            await _respond_json(
                send,
                429,
                {"error": "too many held /status requests; retry without wait"},
                [(b"retry-after", b"5")],
            )
            return
        self._waiters += 1
        self._waiters_by_token[key] = self._waiters_by_token.get(key, 0) + 1
        # A client that hangs up must give its slot back NOW, not when its
        # window would have ended: otherwise a reconnecting watcher (or
        # anyone opening and dropping connections) holds the cap full with
        # requests nobody is waiting for.
        gone = asyncio.ensure_future(_wait_for_disconnect(receive))
        try:
            deadline = time.monotonic() + wait_s
            while True:
                summary = await asyncio.to_thread(_read_status, ident)
                counts = summary["counts"]
                if any(counts.get(k, 0) for k in db.ACTIONABLE_COUNTS):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.wait({gone}, timeout=min(STATUS_POLL_INTERVAL_S, remaining))
                if gone.done():
                    return  # nobody left to answer
        finally:
            gone.cancel()
            self._waiters -= 1
            left = self._waiters_by_token.get(key, 1) - 1
            if left > 0:
                self._waiters_by_token[key] = left
            else:
                self._waiters_by_token.pop(key, None)
        await self._status_answer(send, ident, summary, wait_s)

    @staticmethod
    async def _status_answer(send, ident: auth.Identity, summary: dict[str, Any], wait_s: int):
        actionable = {
            k: summary["counts"][k] for k in db.ACTIONABLE_COUNTS if summary["counts"].get(k, 0)
        }
        await _respond_json(
            send,
            200,
            {
                "channel": ident.channel,
                "role": ident.role,
                "waited": wait_s > 0,
                "wait_s": wait_s,
                "pending": actionable,
                **summary,
            },
        )

    async def _hook_status(self, ident: auth.Identity, send) -> None:
        if ident.role is None or ident.db_path is None:
            await _respond_json(send, 403, {"error": "hook-status needs a channel role token"})
            return
        db_path, role = ident.db_path, ident.role

        def read_counts() -> dict[str, Any]:
            with db.open_db(db_path) as conn:
                return db.channel_summary(conn, role=role)["counts"]

        counts = await asyncio.to_thread(read_counts)
        await _respond_json(
            send,
            200,
            {"channel": ident.channel, "role": ident.role, "counts": counts},
        )


def build_app():
    from .server import mcp

    hosts = allowed_hosts()
    mcp.settings.stateless_http = True
    if hosts:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=_allowed_origins(hosts),
        )
    else:
        log.warning(
            "%s is not set: Host/Origin headers are not checked (DNS-rebinding protection off)",
            ALLOWED_HOSTS_ENV,
        )
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    return AuthMiddleware(mcp.streamable_http_app(), allowed_hosts=hosts)


_HOW_TO_GENERATE_ONE = "python3 -c \"import secrets; print('cca_'+secrets.token_urlsafe(32))\""


def forwarded_allow_ips() -> str:
    """Which peers uvicorn lets rewrite the client address and scheme from
    X-Forwarded-For / X-Forwarded-Proto.

    Kept consistent with _trusted_proxy: AI_AGENT_CHANNEL_TRUST_PROXY=1
    vouches for every peer, so uvicorn trusts every peer too ("*").
    Otherwise uvicorn's own default applies — FORWARDED_ALLOW_IPS when the
    operator set it, else loopback only — so a proxy on the same host is
    honoured and nobody else can spoof a client address. (A proxy on a
    docker network is not loopback: uvicorn leaves its requests alone, and
    _trusted_proxy still believes its X-Forwarded-Proto for the cookie's
    Secure flag, because the peer is private.)
    """
    if os.environ.get(TRUST_PROXY_ENV, "").strip() == "1":
        return "*"
    return os.environ.get(FORWARDED_ALLOW_IPS_ENV, "").strip() or "127.0.0.1"


def _startup_warnings(admin_token: str) -> None:
    """Settings that are weak but not unsafe enough to refuse a live server."""
    if len(admin_token) < ADMIN_TOKEN_MIN_LENGTH:
        log.warning(
            "%s is only %d characters long; use at least %d random characters "
            "(generate one: %s) and restart",
            auth.ADMIN_TOKEN_ENV,
            len(admin_token),
            ADMIN_TOKEN_MIN_LENGTH,
            _HOW_TO_GENERATE_ONE,
        )
    auth.view_key_ttl_days()  # logs once when the configured value is invalid


def serve_http(host: str = HOST_DEFAULT, port: int = PORT_DEFAULT) -> None:
    admin_token = os.environ.get(auth.ADMIN_TOKEN_ENV, "").strip()
    if not admin_token:
        raise SystemExit(
            f"{auth.ADMIN_TOKEN_ENV} is not set — the HTTP transport refuses "
            f"to start without an admin token (generate one: {_HOW_TO_GENERATE_ONE})"
        )
    _startup_warnings(admin_token)
    # fail fast on an unwritable data dir, not on the first tool call; and
    # migrate the registry here, once, instead of on a request path
    try:
        auth.data_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        auth.migrate_admin_db()
    except (OSError, sqlite3.Error) as exc:
        raise SystemExit(
            f"data dir {auth.data_dir()} is not usable ({exc}) — set "
            f"{auth.DATA_DIR_ENV} to a writable directory"
        ) from exc
    import uvicorn

    # channel mailboxes are created later, by db/: owner-only from here on
    os.umask(0o077)
    uvicorn.run(
        build_app(),
        host=host,
        port=port,
        log_level="info",
        # explicit rather than inherited from uvicorn's defaults, so the
        # proxy trust model is visible in one place (see forwarded_allow_ips)
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips(),
    )
