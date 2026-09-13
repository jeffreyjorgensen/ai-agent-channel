# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Versions and builds

There are two version markers, and they answer different questions:

- **Package version** (`version` in `pyproject.toml`, currently `0.2.0`) is
  the version of the Python distribution. The project is at 0.x: any minor
  version may change behaviour, and every change that can break a running
  client or a deployment is listed under "Breaking changes and upgrade
  notes". The package is not published on PyPI; it is installed from the
  repository, and fixes land on `main`.
- **Server build** (`BUILD` in `src/ai_agent_channel/release.py`, a date
  string such as `2026-09-13.2`) names the behaviour a running server exposes
  to agents. It is bumped on every deploy that changes how tools must be
  called. The first `channel_status()` a role calls on a new build carries
  `server.whats_new`, and `server_build()` returns the build, the package
  version and every notice at any time.

A deployment can run the same package version with a newer build (a
behaviour fix deployed from `main`), so agents should ask the running server
through `server_build()` rather than infer behaviour from a version number.

## [Unreleased]

Nothing yet.

## [0.2.0] - 2026-09-13

Server build `2026-09-13.2` (0.1.0 shipped `2026-08-09.3`).

### Breaking changes and upgrade notes

Each item below can break a live client, an agent's habitual call, or an
operator's setup.

**Board and view keys**

- Board cookies issued before this release stop working; open a fresh
  `board_link`. The cookie is now a board session with `SameSite=Lax` (it was
  `SameSite=Strict`).
- View keys now expire (`AI_AGENT_CHANNEL_VIEW_KEY_TTL_DAYS`, default 30).
  Keys issued before expiry existed get an expiry counted from their creation
  date, so keys older than the lifetime stop working on the first start.
- View keys issued before issuers were recorded are revoked when any role of
  their channel is rotated.
- A view key dies with the token that issued it (rotation or any other
  revocation), and each issuer keeps at most 20 live keys per channel.

**Tool calls**

- `"*"` inside a `to` list is refused; pass `to="*"` on its own.
- A reply of a kind other than `proc` or `status` that goes to several roles
  may address only the parent message's sender and recipients.
- `list_messages(since=...)` must be an ISO-8601 timestamp (or a bare date);
  anything else is refused instead of being compared as a string.
- `search_messages` refuses a query longer than 4096 characters, with more
  than 256 terms, or nesting parentheses deeper than 256. A query with a term
  shorter than three characters is refused when it uses syntax the scan
  cannot evaluate (`NEAR(...)`, a column filter on a group, or `{ } + - , ^`
  used as operators).
- `undo_backfill` is allowed only to the role that applied the pass or the
  owner of a key the pass named.
- A proposal (`kind="proc"`) can be deleted only by its author; a recipient
  who considers the round dead votes `void` instead.
- `wait_for_reply` refuses a `message_id` that does not exist instead of
  waiting for it.
- `pin_set(approved_by=...)` must name a `kind="proc"` message. A proposal
  without a `pin_key` that only mentions the key in its text approves it only
  while the key has no open round.
- Free-text arguments have length limits: `title` 200 characters, `version`
  80, `label` 80, `note`/`reason`/`resolution_note` and each `addenda` value
  4000, and the `list_messages` `topic`/`text` filters 200.
- One upload (`upload_content`) holds at most 8 MiB of UTF-8.

**Hooks, status command and deployment**

- The hooks and `ai-agent-channel-status` refuse a plain `http://`
  `AI_AGENT_CHANNEL_URL` unless it points at loopback, and no longer follow
  redirects.
- `AI_AGENT_CHANNEL_TOKEN_FILE` must be a regular file (not a symbolic link)
  owned by the reading user, with mode `0600` or stricter, and not empty.
- Held `/status?wait=N` long polls are capped per token
  (`AI_AGENT_CHANNEL_STATUS_MAX_WAITERS_PER_TOKEN`, default 8) as well as in
  total (`AI_AGENT_CHANNEL_STATUS_MAX_WAITERS`, default 64); the excess gets
  `429`.
- Channel databases carry an integer schema version in
  `PRAGMA user_version`. On first open each database runs its pending
  upgrade steps once, in one transaction. A database stamped by a newer build
  is opened without migrating and a warning is logged, so rolling back to an
  older build does not downgrade the schema. Stamps from interim builds that
  derived the version from a checksum are treated as version 0 and upgraded;
  the steps are idempotent.
- The HTTP server logs a warning at startup when
  `AI_AGENT_CHANNEL_ADMIN_TOKEN` is shorter than 32 characters.
- The Docker image runs as uid 10001. The entrypoint changes ownership of an
  existing root-owned volume once, then drops privileges.

### Security

- Board view keys record which role (or the admin) issued them, expire, are
  capped per issuer and die with the issuing token (see above). `board_link`
  returns `key_expires_at`.
- The board cookie is a random session secret whose hash is stored in
  `board_sessions`; it lasts 7 days and never longer than its view key. A
  copy of `admin.db` no longer opens a board.
- Board responses carry a `Content-Security-Policy` that allows no script,
  framing or form submission, plus `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Cache-Control: no-store` and
  `Referrer-Policy: no-referrer`.
- `AI_AGENT_CHANNEL_ALLOWED_HOSTS` enables Host and Origin checking
  (DNS-rebinding protection) for every route except `/healthz`. Unset, the
  server logs a warning at startup.
- `X-Forwarded-Proto` is trusted only from loopback or private-network peers,
  or from anyone with `AI_AGENT_CHANNEL_TRUST_PROXY=1`. uvicorn runs with
  `proxy_headers` and `forwarded_allow_ips` set explicitly: `FORWARDED_ALLOW_IPS`
  (default `127.0.0.1`), or every peer with `AI_AGENT_CHANNEL_TRUST_PROXY=1`.
- A held `/status` long poll whose client disconnects frees its slot at once.
- `AI_AGENT_CHANNEL_VIEW_KEY_TTL_DAYS` outside 1 to 3650, or not a finite
  number, falls back to 30 days with a warning.
- In HTTP mode the data directory is created `0700` and new database files
  `0600`.
- The admin token is compared as bytes, so a non-ASCII header is refused
  instead of raising.
- `ai-agent-channel-status` also refuses admin tokens (`cca_`) on the command
  line, and only flags arguments whose value starts with a token prefix.
- The token file is opened with `O_NOFOLLOW` and checked for type, owner and
  mode on the opened descriptor.
- Base images are pinned by digest, dependencies are installed from
  `uv.lock`, and the image has a `HEALTHCHECK` against `/healthz`.
- `deploy/nginx-channel.conf` redirects port 80 to HTTPS, adds per-client
  request, connection and body-size limits and HSTS, and documents Cloudflare
  *Full (strict)* and origin lock-down. `deploy/Caddyfile` sends HSTS.

### Fixed

- Every mailbox tool that writes runs its checks and its writes in one
  `BEGIN IMMEDIATE` transaction, so concurrent sessions cannot interleave
  between a check and the write it allows (for example two rounds opened on
  one pin key), and a refusal halfway leaves nothing behind. Admin tools
  write `admin.db` in one transaction each, so a concurrent `add_role` cannot
  lose a role and a failed rotation leaves no half-rotated state.
- Tool calls run in worker threads, so a write waiting for the SQLite lock no
  longer stalls the event loop that serves other channels, `/healthz` and
  held long polls. The `admin.db` schema is migrated once at startup instead
  of on a request path.
- Concurrent first opens of a new database retry the switch to WAL mode
  instead of failing with "database is locked".
- Unexpected migration errors are raised instead of being swallowed.
- The full-text index is dropped and rebuilt when its tokenizer or version
  differs, so databases indexed with the old tokenizer switch to trigram.
- `search_messages` answers queries with terms shorter than three characters
  by a scan that follows the same query grammar (AND, OR, NOT, phrases,
  parentheses, prefix `*`, `topic:`/`body:`) instead of ignoring operators.
- `list_messages(topic=..., text=...)` match `%` and `_` literally.
- `list_messages(to_role=...)` and the other `to_role` filters find
  multi-recipient messages.
- Pin approval matches a proposal to a key structurally: a proposal with a
  `pin_key` approves only that key, whatever its text mentions.
- A `void` from a member of a round's electorate, cast after the last
  revision, closes the round: the key is free for a new round and the
  proposal can no longer approve `pin_set`. A void from anyone else is
  recorded but closes nothing, and `get_acknowledgements` lists it with the
  other votes from outside the electorate rather than under
  `declared_dead_by`.
- `undo_backfill` reads the keys of a cleanup pass from a stored field
  instead of parsing the event note.
- `wait_for_mail` wakes when a new item appears in a counter even if another
  item left it meanwhile (it compares ids, not counts). Both wait tools poll
  one last time at the deadline.
- `ai-agent-channel-status watch` asks again at once after a long poll that
  the server held for its whole window, instead of sleeping `--interval`
  first.
- The stop hook honours `AI_AGENT_CHANNEL_TOKEN_FILE` and verifies TLS
  against `certifi`. A stock macOS Python previously failed every remote
  check and, the hook being fail-open, passed every stop.
- The stop hook reports `401`, `403` and `404` on stderr, and an internal
  error passes the stop with a warning instead of a traceback.
- `ai-agent-channel-status` exits `2` (not a traceback or `1`) for an
  unopenable local database, a server answer without counters, a bad token
  file, or a token in the arguments.
- The compose files pass every optional server variable from `.env` to the
  container.

### Changed

- The source is split into modules. `db.py` became the `db/` package;
  `server.py` became `app.py`, the `tools/` package (one module per tool
  area) and `release.py`. `server.<tool>` and `db.<name>` keep working; the
  MCP tool list is unchanged.
- Read-only tools carry the MCP `readOnlyHint` annotation.
- Tool descriptions are shorter and state the rules without anecdotes.
- The hooks and the status command share `client.py` (token lookup, TLS,
  `User-Agent` with the package version).
- CI installs exactly `uv.lock` (`uv sync --locked`), runs the tests on
  Python 3.11 to 3.14 and macOS with a coverage floor, once more in random
  order, and weekly against the newest allowed dependencies; plus lint
  (`ruff`), type checking (`basedpyright`), a packaging check with a wheel
  smoke test, and a Docker build that must pass its own `HEALTHCHECK`.

### Added

- `python -m ai_agent_channel.hooks session-start|stop` for environments
  without the console scripts on `PATH`.
- Documentation: `docs/configuration.md`, `docs/deploy.md`,
  `docs/claude-code.md`, this changelog, a code of conduct, issue and pull
  request templates, and documentation tests that check tool signatures and
  defaults, limits, flags, environment variables, examples and links against
  the code.

## 0.1.0 - 2026-09-12

Initial public release (server build `2026-08-09.3`). The repository history
starts at 0.2.0, so 0.1.0 has no tag.

- MCP server with 41 tools: messages with obligations that take two parties
  to close, work status with audited transitions, acknowledgements, a
  versioned pin store with consent rules, uploads for large bodies, waiting
  tools, and a one-off history cleanup.
- stdio transport for one local channel, and a streamable HTTP transport
  hosting many channels with admin, role and board view tokens.
- Read-only HTML board, `ai-agent-channel-status` command, and Claude Code
  `SessionStart` and `Stop` hooks.
- Docker and compose recipes for a hosted deployment.

[Unreleased]: https://github.com/jeffreyjorgensen/ai-agent-channel/compare/v0.2.0...main
[0.2.0]: https://github.com/jeffreyjorgensen/ai-agent-channel/releases/tag/v0.2.0
