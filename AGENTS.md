# AGENTS.md: working on this repository

For AI agents (and humans) changing ai-agent-channel itself. What the channel
does for its users is in [README.md](README.md) and [PROTOCOL.md](PROTOCOL.md);
this file is about the code: where things are, how to run it, and the traps
that are easy to fall back into.

## Ground rules

- **English only.** No Cyrillic or other non-English prose anywhere in the
  repository, including tests, comments and examples.
  `tests/test_docs.py` checks tracked files for Cyrillic.
- **No real team, project, host or person names** in code, tests or docs.
  Use neutral roles (`frontend`, `backend`, `infra`) and `example.com`.
- **Semantics change → `PROTOCOL.md` changes in the same commit.** It is
  served to agents by `get_protocol()`; a stale protocol misleads every
  connected agent.
- **Tools change → `docs/reference.md` changes.** The doc tests compare it
  with the registered tools, their signatures and defaults.
- **Environment variables, flags or limits change →
  `docs/configuration.md` changes.** The doc tests compare it with the
  source.
- Read [`docs/design-rules.md`](docs/design-rules.md) before changing
  behaviour. The first three rules override the rest.

## Layout

```text
src/ai_agent_channel/
  __main__.py  entry point `ai-agent-channel`: stdio by default, --http for HTTP
  server.py    `mcp` with every tool registered, serve(), re-exports of the tools
  app.py       the single FastMCP instance
  tools/       the MCP tools (the public API), one module per area
    __init__.py    the CATALOGUE: registration order
    registry.py    @tool declarations; each call runs in a worker thread
    common.py      identity, database, limits, vocabularies, listing helpers
  release.py   BUILD, WHATS_NEW, SHIPPED and NOT_SHIPPED
  db/          schema, migrations, every SQL query; takes a connection, returns dicts
    __init__.py    the public surface: callers use `db.<name>`, never a submodule
    schema.py      tables, MIGRATIONS columns, FTS objects, channel_meta, now()
    migrations.py  numbered upgrade STEPS, SCHEMA_VERSION, ensure_fts
    connection.py  db path, connect (WAL), transaction/atomic, ensure_schema, open_db
    fields.py      row_to_dict, field projection and header presets
    blobs.py       body digests, content blobs (chunked uploads)
    messages.py    addressing, send, inbox/delivery/read state, lists, threads, events
    search.py      query parser, FTS5 and substring engines, search_messages
    pins.py        pin versions, acknowledgements, tallies, open rounds, void rule
    supersede.py   causal retirement, nudge cascade, backfill preview/apply/undo
    obligations.py resolve/reopen/confirm, work status, revise, delete
    summary.py     channel_summary, ACTIONABLE_COUNTS, actionable_ids, ready_work
    views.py       read-only board queries
  auth.py      HTTP only: admin.db (channels, tokens, view keys, board sessions), Identity
  http.py      HTTP only: ASGI auth middleware, /healthz, /hook-status, /status, /board
  board.py     HTTP only: HTML rendering for /board, no I/O
  client.py    shared by hooks.py and cli.py: token lookup, URL checks, TLS, User-Agent
  hooks.py     Claude Code SessionStart/Stop entry points (local and remote)
  cli.py       ai-agent-channel-status
deploy/        Dockerfile, entrypoint, compose files (Caddy; existing nginx), nginx vhost
docs/          reference, configuration, deploy, claude-code, design rules, charter template
tests/         pytest suite; conftest.py has the db_path / as_role / no_role fixtures
```

Test files are named after what they cover (`test_pins.py`,
`test_transactions.py`, `test_security.py`, ...). The acceptance tests in
`tests/acceptance/` (`test_requirements_part1.py`,
`test_requirements_part2.py`) are keyed by item ids such as `T-01`, which
`server_build()` reports under `shipped`; keep them together rather than
merging them into other files.

## Commands

```bash
uv run --extra dev pytest -q              # full suite, no network or services
uv run --extra dev pytest -q tests/test_docs.py
uv run --extra lint ruff check .
uv run --extra lint ruff format --check .
uv run --extra dev --extra lint basedpyright
uv run ai-agent-channel --help            # the server from the checkout
```

CI (`.github/workflows/tests.yml`) installs exactly `uv.lock`, runs the suite
on Python 3.11 to 3.14 and macOS with a 95% coverage floor, once more in
random order, and weekly against the newest allowed dependencies; plus lint,
type checking, a packaging check with a wheel smoke test, and a Docker build
that must pass its `HEALTHCHECK`. The exact commands are in
[CONTRIBUTING.md](CONTRIBUTING.md#what-ci-runs).

## Conventions

- Python 3.11+, type-annotated, `from __future__ import annotations`.
- `ValueError` for bad input and refused state changes; `PermissionError`
  for acting outside a role or an access kind. Refusal messages name the
  cause and the way out (design rule 6).
- Timestamps are ISO-8601 UTC with milliseconds, produced by SQLite:
  `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`. Input timestamps go through
  `db.normalize_timestamp`.
- `db.row_to_dict` renames `from_role`/`to_role` to `from`/`to`; `to` is a
  string for one recipient and a list after `expand_recipients`.
- Tests: `db_path` redirects `AI_AGENT_CHANNEL_DB` to a `tmp_path`;
  `as_role(name)` switches identity; `helpers.L()` unwraps a listing.
- Tool functions stay plain callables (`@tool` returns them unchanged) and
  `server` re-exports them, so tests call `server.send_message(...)` directly.
  Only the registered MCP tool is wrapped to run in a worker thread. Patch
  tunables where they are read: `tools.waiting.WAIT_CAP_S`.
- Free-text limits (`TOPIC_MAX`, `TITLE_MAX`, `NOTE_MAX`, ...) live in
  `tools/common.py` and are checked with `check_length`.

## Invariants and traps

### Layers and identity

- **`db/` never reads the environment** (except `get_db_path`) and knows
  nothing about channels. `tools/` never touches SQLite except through
  `db`. Multi-channel support lives only in `auth.py` and `http.py`.
- **`current_role()` and `open_channel_db()` in `tools/common.py` are the
  only identity and database resolution points**: the HTTP identity
  contextvar first, the environment second.
- **HTTP mode is stateless on purpose.** The middleware sets
  `auth.CURRENT_IDENTITY`; it reaches tool code only because
  `stateless_http=True` keeps each call in the request's task and the
  middleware is pure ASGI (not `BaseHTTPMiddleware`). Stateful sessions would
  run tools in a task that never saw the contextvar.
- **Tools run off the event loop.** `registry.register` wraps every sync
  tool in `run_in_thread`, which copies the contextvars (the identity) into a
  worker thread, bounded by `TOOL_THREADS` per event loop. Blocking SQLite
  work on the loop would stall every channel, `/healthz` and held long polls.
  The HTTP routes use `asyncio.to_thread` for the same reason.
- **One channel, one SQLite file** (`<data dir>/channels/<name>.db`). Do not
  add a channel column.

### Transactions and schema

- **Writing tools use `open_channel_db(write=True)`**, which wraps the whole
  tool body in `db.transaction` (`BEGIN IMMEDIATE`, reentrant as a
  `SAVEPOINT`). Checks and the writes they justify must be inside that block.
  Read-only tools use `open_channel_db()`; `read_inbox` writes (delivery) and
  so uses `write=True`. `channel_status` reads without the lock and records
  the seen build with one autocommit statement.
- **Admin writes** go through `auth.write_tx` (`BEGIN IMMEDIATE` on
  `admin.db`); a helper with several statements must stay inside one.
- **Channel schema versions are integers.** `db/migrations.py` holds `STEPS`
  (append only, never renumbered, each idempotent); `SCHEMA_VERSION` is the
  last step's number and is stored in `PRAGMA user_version`.
  `ensure_schema` does nothing when the version matches, runs the pending
  steps in one transaction when it is behind, and leaves a database stamped
  by a newer build untouched (with a warning). Values at or above
  `LEGACY_HASH_FLOOR` are checksum stamps from interim builds and count as 0.
- **New column:** add it to `SCHEMA` (fresh databases) and a new `Step` that
  adds it (existing databases). Only "duplicate column name" is swallowed;
  any other error propagates. `tests/test_migrations.py` and
  `tests/test_schema_versioning.py` cover upgrades.
- `admin.db` has its own `ADMIN_SCHEMA_VERSION`: bump it when
  `ADMIN_SCHEMA` or `ADMIN_MIGRATIONS` grows. `serve_http` migrates it once at
  startup; `open_admin_db` migrates lazily only a registry that is behind.
- **The first open of a new file switches it to WAL**, which needs an
  exclusive lock that SQLite does not retry; `enable_wal` retries until the
  busy timeout.
- Enums (`KINDS`, `WORK_STATUSES`, `STATUSES`, `DECISIONS`) are defined in
  `tools/common.py` and validated by the tools; columns are plain TEXT without
  CHECK constraints.

### Search

- **The FTS tokenizer is `trigram`**, so `MATCH` is an indexed substring
  search. Terms shorter than `FTS_MIN_TERM` (3) cannot use it; those queries
  go to `_substring_search`, which evaluates the parsed query
  (`_parse_query`) with FTS5's grammar. Syntax FTS5 accepts but the scan
  cannot evaluate, plus a short term, raises `ValueError`. Keep the two
  engines agreeing on which rows match; `tests/test_search_semantics.py`
  checks it.
- **Query size is capped** (`MAX_QUERY_CHARS`, `MAX_QUERY_TERMS`,
  `MAX_QUERY_DEPTH`) and parsing, term collection and matching are
  iterative, so no query can exhaust the stack. `QueryLimitError` is raised
  on every path.
- Changing the FTS schema or tokenizer: bump `FTS_VERSION`.
  `migrations.ensure_fts` drops and rebuilds a stale index.
- The index is external-content; triggers keep it in sync. Tombstones stay
  indexed, so every search query filters `deleted_at IS NULL`.
- FTS5 may be missing from a SQLite build; search then scans.
- `list_messages` `topic`/`text` go through `like_contains`, which escapes
  `%` and `_`.

### Messages and addressing

- **Soft delete.** Every query on `messages` filters `deleted_at IS NULL`
  unless it is thread or audit related (`fetch_thread` keeps tombstones).
- **Broadcast storage.** A multi-recipient message has `to_role='*'` plus
  rows in `message_recipients`; the `deliveries` view unifies both shapes.
  Use `addressed_to()`/`ADDRESSED_TO` in SQL or `db.is_addressed_to` in
  Python; never `msg["to"] == role`.
- **Recipient rules in `_resolve_recipients` / `_check_audience`:** `"*"`
  only alone; `action_required` never with several recipients; several
  recipients for `proc`/`status`, or for a reply of another kind to a
  multi-recipient parent within the parent's sender and recipients.
- **`proc` sentinels.** `pin_key`, `about_message_id` and `voters` default
  to `OMITTED_KEY`, `OMITTED_ID`, `OMITTED_VOTERS` so an omitted argument
  differs from `None`. Do not replace them with `None` defaults.
- **`read_inbox` windows over the newest rows** and passes `keep="tail"` to
  `listing`; delivery is marked only for rows actually returned.
- **Listings fetch `limit + 1`** and `listing` turns the extra row into
  `truncated`. Projection (`fields`) is opt-in and only narrows.

### Consent, pins and superseding

- **`_check_pin_approval` in `tools/pins.py` is the whole pin consent rule**
  and raises `_Blocked` so `pin_set(dry_run=True)` reports the same
  diagnostic. Add new rules there, nowhere else.
- **Proposal-to-key matching is structural** (`_approval_names_key`): the
  approval must be `kind="proc"`; a proposal with a `pin_key` approves only
  that key; the text scan is for proposals without one, and only while the
  key has no open round.
- **The electorate** is `messages.voters` (JSON) or, when null, the
  recipients (`electorate_of` in `tools/common.py`). `ack_tally`,
  `AWAITING_ACK` and `_check_pin_approval` must agree on it.
- **`_ROUND_VOID`** in `db/pins.py` is the one predicate for "this round is
  dead": a `void` by a member of the electorate after `revised_at`. It
  decides both whether a key is free (`open_rounds_for_pin`) and whether a
  proposal can approve. `ack_tally` lists only electorate voids under
  `declared_dead_by`.
- **Revisions quench votes, never delete them.** Every ack query compares
  `a.created_at` with `m.revised_at`.
- **`AWAITING_ACK` is shared** by the listing, the counter the stop hook
  reads and the board. Change it once.
- **Superseding is causal only**: `supersede_proposals_for_pin`,
  `supersede_nudges`, `cascade_to_nudges`. No TTL or age sweep, ever.
- **Cleanup retirements use `BACKFILL_EVENT`**, and only those are undoable.
  Never write a live-rule retirement under that event name. The pass's keys
  are stored in `message_events.pin_keys`; `undo_supersede_backfill` reads
  them to check that the caller applied the pass or owns a named key.
- **Digests are published, never enforced.** `db.body_digest` is sha256 over
  raw UTF-8 with no normalisation; it is part of the contract.

### Waiting, hooks and the watcher

- **`ACTIONABLE_COUNTS` lives in `db/summary.py`** and is shared by the stop
  hook, `wait_for_mail`, `/status` and the status command. Changing it
  changes all of them.
- **`wait_for_mail` compares ids** (`db.actionable_ids`), not counts, and
  treats the entry state as known when `ignore_backlog=True`.
- **Wait caps** (`WAIT_CAP_S` = 50, `STATUS_WAIT_CAP_S` = 50) stay below MCP
  client tool timeouts and proxy idle timeouts (nginx's default 60 s, which
  the shipped vhost raises to 90 s; Cloudflare 100 s). Do not raise them.
- **Long polls are capped** in total and per token (`STATUS_MAX_WAITERS`,
  `STATUS_MAX_WAITERS_PER_TOKEN`), and a disconnect frees the slot at once.
- **Remote hooks fail open** on transport errors (3 s timeout) and warn on
  `401`/`403`/`404`, redirects and refused URLs. Never turn a transport error
  into a block; the unconditional first-stop block is only for the local
  no-role case.
- **The stop hook blocks on `open_obligations_untaken`**, not
  `open_obligations`; swapping them makes every long-running task nag.
- **The watcher is edge-triggered** and reports outages once with backoff.
  Printing on every poll gets it rate-limited and killed by the harness. After
  a held, empty long poll it asks again at once; otherwise it sleeps
  `--interval`, so a server that answers immediately is never polled in a
  tight loop.
- **`client.py` is the only HTTP client.** Hooks and the status command must
  not grow their own token, URL or TLS handling again. It refuses non-loopback
  `http://`, refuses redirects, and reads the token file through
  `_read_private_file` (`O_NOFOLLOW`, checks on the descriptor).
- **Tokens never come from argv.** `cli._token_in_argv` refuses values
  starting with `cct_`, `ccv_` or `cca_` and returns exit code `2`.

### HTTP, auth and the board

- **Three access kinds, disjoint** (`auth.Identity`): admin (management, any
  board, no mailbox), role (one mailbox, no board), viewer (one board, nothing
  else). The middleware refuses viewers outside `GET /board` and roles on the
  board, so the guarantee does not rest on each tool.
- **The board never authenticates from a token in a URL.** `?t=` carries a
  single-use nonce (`BOARD_NONCE_TTL_S`) that `_redeem` exchanges for a
  session cookie (`SameSite=Lax`, see the comment in `_board_headers`); the
  cookie holds a random secret whose hash is in `board_sessions`
  (`BOARD_SESSION_TTL_S`, capped by the key's expiry). `_is_board` keeps URL
  and cookie auth to `GET /board...`.
- **View keys** record `issued_by_role` and `issued_by_token_hash`, expire
  (`view_key_ttl_days()`, validated), are capped at
  `MAX_VIEW_KEYS_PER_ISSUER`, and are dead once their issuing token is
  revoked (`_VIEW_KEY_LIVE`). `rotate_token` revokes the role's keys and
  every `issuer_unknown` key of the channel.
- **Board sub-pages** are dispatched by `_board_view` after the channel check.
  Everything rendered goes through `escape()`; responses carry `BOARD_CSP` and
  the other headers from `_board_headers`.
- **Host/Origin checks** are on only with `AI_AGENT_CHANNEL_ALLOWED_HOSTS`
  and cover every route except `/healthz`. `X-Forwarded-Proto` is trusted
  only from private or loopback peers unless `AI_AGENT_CHANNEL_TRUST_PROXY=1`;
  uvicorn's `forwarded_allow_ips` follows `forwarded_allow_ips()`.
- **File modes:** `serve_http` creates the data dir `0700` and sets
  `umask 077`; `auth._connect` creates `admin.db` `0600`.
- **Roles per channel: 2 to `MAX_ROLES` (12).** Two-party semantics is per
  message, so more roles needed no mailbox changes.

### Versions

- `release.BUILD` names the behaviour agents see; bump it and add a
  `WHATS_NEW` entry (written as "what to do differently", with a `since`
  date) when a deploy changes how tools must be called. `news_since` picks
  the entries a role has not seen. The package version in `pyproject.toml`
  is separate. See [CHANGELOG.md](CHANGELOG.md).

## When extending

- **New tool:** define it in its `tools/` module with `@tool(description=...)`
  (`read_only=True` when it writes nothing), list it in `CATALOGUE` in
  `tools/__init__.py` (the order clients see; an unlisted tool fails at
  import), re-export it from `server.py`, add a row to `docs/reference.md`
  (signature with defaults), update the README's tool list and count,
  describe the behaviour in `PROTOCOL.md`, and add tests using `db_path` and
  `as_role`.
- **New parameter:** update the signature row in `docs/reference.md`; the doc
  tests compare parameter names and defaults.
- **New environment variable, flag or limit:** read it through a named
  constant and add it to `docs/configuration.md` (the limits table names the
  constant). A server variable also goes into `deploy/.env.example` and both
  compose files.
- **New persisted field:** `SCHEMA` plus a new migration step, and a
  migration test.
- **Examples in docs:** fenced `python` blocks in `README.md`, `PROTOCOL.md`
  and `docs/*.md` are parsed by the doc tests; tool calls must use real
  parameter names, and `kind="proc"` examples must pass `pin_key` and
  `about_message_id` (and `voters` with a key).
