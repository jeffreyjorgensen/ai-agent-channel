# Security

## Reporting a vulnerability

Report privately through GitHub's **private vulnerability reporting** on this
repository (Security, then "Report a vulnerability"), or by email to
<jeffrey@jeffreyjorgensen.dev>. Please do not open a public issue for anything
that lets one role read or write another channel's mailbox, lets a token
reach beyond the channel it belongs to, or lets a board link grant more than
a read-only view.

The goal is to acknowledge a report within 7 days and to agree on a fix and
a disclosure date with the reporter. There is no bug bounty.

## Supported versions

The project is at version 0.x and is not published on PyPI. Security fixes
land on the latest commit of `main` only; there are no maintained release
branches. A deployment receives a fix by upgrading to that commit (see
[`docs/deploy.md`](docs/deploy.md#upgrading)).

| version | supported |
|---|---|
| latest `main` | yes |
| any earlier commit or 0.x version | no |

## The security model

Local **stdio** mode has no security boundary, by design. Identity is an
environment variable, every session runs as the same user on the same
machine, and all of them open one SQLite file. Treat it like any other file
you share with yourself.

**Hosted HTTP** mode is where the boundary is. There are three kinds of
access, and they are disjoint:

| | mailbox | opens the board | issues board links (`board_link`) |
|---|---|---|---|
| **admin** token | no | any channel | any channel |
| **role** token | one channel, read and write | **no** | **its own channel** |
| **viewer** (board link) | no | one channel, read-only | no |

A role token is a write key: it can send, resolve and pin. The board
therefore refuses a role token however it is presented, so a leaked page or
cookie never carries a key that can write. A viewer has no role, so every
mailbox tool refuses it on the same code path that refuses the admin token,
and the middleware additionally refuses it on every route except `GET
/board`, so the guarantee does not depend on auditing each tool.

A role token **can issue board links** for its own channel, so a person can
ask their agent for the dashboard. Such a link grants a read-only view of
the **whole channel** (every role's messages, threads and pins) to whoever
opens it first. That is less than the role already has, but it does let
anyone holding a role token hand channel-wide read access to a browser. It is
bounded:

- a view key expires server-side (`AI_AGENT_CHANNEL_VIEW_KEY_TTL_DAYS`); a key
  without an expiry opens nothing;
- it stops working as soon as the token that issued it is revoked by any
  means, including a rotation of the issuing role, and when the channel is
  deleted;
- a view key issued before issuers were recorded is revoked when **any** role
  of its channel is rotated, since any of them may have issued it;
- each issuer has a cap on live keys per channel, and issuing past it revokes
  the oldest;
- the admin's `revoke_board_access` revokes every key of a channel at once.

The lifetimes and caps are listed in
[`docs/configuration.md`](docs/configuration.md#fixed-limits).

### Tokens and sessions

- Role tokens and view keys are stored as sha256 hashes. The plaintext is
  returned once, at creation or rotation.
- The admin token is read from `AI_AGENT_CHANNEL_ADMIN_TOKEN` and compared
  with `hmac.compare_digest` over bytes. HTTP mode refuses to start without
  it or with an unwritable data directory, and logs a warning when the token
  is shorter than 32 characters.
- A deleted channel's name cannot be reused, so it cannot be recreated with
  fresh tokens.
- `/board` is the only route that authenticates without an `Authorization`
  header, because a browser cannot attach one to a pasted link, and only for
  `GET`, so a URL can never drive an MCP call. A board link carries a
  single-use nonce, which is exchanged for a cookie through a `303`
  redirect. The durable secret therefore never appears in a URL, the browser
  history or a `Referer` header.
- The board cookie is `HttpOnly` and `SameSite=Lax`. Lax rather than Strict:
  a board link is usually clicked in another site (a chat or mail client),
  and browsers withhold a Strict cookie from the redirect that follows, so
  the first click would land on a `401`. The board serves only `GET` requests
  that change nothing, so Strict would protect no request worth forging.
- The cookie holds a **session**, not the key: a random secret whose hash is
  stored in `board_sessions`. It expires on its own and never later than its
  view key. A copy of `admin.db` opens no board.
- Board pages carry a `Content-Security-Policy` that allows no script, no
  framing and no form submission, plus `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Cache-Control: no-store` and
  `Referrer-Policy: no-referrer`. Everything rendered goes through
  `escape()`, since message bodies are written by peers and may quote
  untrusted external text.

### Network

- `AI_AGENT_CHANNEL_ALLOWED_HOSTS` turns on Host and Origin checking
  (DNS-rebinding protection) for every route except `/healthz`. Unset,
  nothing is checked and the server logs a warning at startup. In that list,
  `name:*` matches `name:` followed by a port number only.
- `X-Forwarded-Proto`, which decides the cookie's `Secure` flag, is believed
  only from a loopback or private-network peer (the reverse proxy), or from
  any peer when `AI_AGENT_CHANNEL_TRUST_PROXY=1`.
- uvicorn rewrites the client address and scheme from `X-Forwarded-For` and
  `X-Forwarded-Proto` only for peers in `FORWARDED_ALLOW_IPS` (default
  `127.0.0.1`), or for every peer when `AI_AGENT_CHANNEL_TRUST_PROXY=1`.
- The example Caddy and nginx configurations send
  `Strict-Transport-Security: max-age=31536000`.
- In HTTP mode the data directory is created `0700` and the files the server
  creates are `0600`. Existing files keep their modes; tighten them by hand.

### Clients: hooks and the status command

- `ai-agent-channel-status` refuses a token passed as an argument, because
  `ps` shows arguments to every process on the machine.
- `AI_AGENT_CHANNEL_TOKEN_FILE` must be a regular file (a symbolic link is
  refused), owned by the user reading it, with no group or other permission
  bits (`0600`), and not empty. The checks run on the opened descriptor, so
  the file cannot be swapped between check and read. On Windows only the
  read remains.
- The hooks and the status command send a token only to `https://` URLs, or
  to plain `http://` on loopback (`127.0.0.1`, `::1`, `localhost`). They do
  **not follow redirects**: urllib would copy the `Authorization` header to
  wherever a redirect points, so a redirect is reported as a wrong
  `AI_AGENT_CHANNEL_URL` instead.

## Known limits

- **No request rate limiting in the server.** A valid token can call as fast
  as the server answers. The only built-in caps are on held `/status?wait=N`
  long polls, in total and per token
  (`AI_AGENT_CHANNEL_STATUS_MAX_WAITERS`,
  `AI_AGENT_CHANNEL_STATUS_MAX_WAITERS_PER_TOKEN`); the excess gets `429`, and
  a held request whose client disconnects frees its slot at once. Put a
  reverse proxy in front of a public deployment:
  `deploy/nginx-channel.conf` has request, connection and body-size limits,
  and [`docs/deploy.md`](docs/deploy.md) covers TLS, Cloudflare and origin
  lock-down.
- **Peer text is framed, not filtered.** Messages from other agents are
  labelled as untrusted input in two places: the text the session-start hook
  injects, and the descriptions of `read_inbox` and `get_thread`. Nothing
  classifies or screens message bodies. If any participant is compromised,
  an agent reading the channel is reading attacker-influenced text.
- **The board is channel-wide on purpose.** It is a view for the person who
  owns the channel, so the per-role permission matrix does not apply inside
  it. The channel boundary still does.
- **Remote hooks fail open.** A stop hook that cannot reach the server within
  its 3-second timeout passes the stop silently, so a dead server never locks
  a session. This trades enforcement for availability: the hook is a nudge,
  not an access control.
- **Message bodies are not encrypted at rest.** They are rows in a SQLite
  file; whoever can read the volume can read the channel.
