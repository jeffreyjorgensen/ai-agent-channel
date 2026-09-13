# Configuration

Every environment variable, command-line flag and fixed limit, in one place.
`tests/test_docs.py` checks this page against the source: every
`AI_AGENT_CHANNEL_*` variable the code reads, every command-line flag, and
every value in the [fixed limits](#fixed-limits) table.

## Naming

All variables use the `AI_AGENT_CHANNEL_` prefix, matching the package name.
Token prefixes are shorter and older: `cct_` (role token), `ccv_` (board view
key) and `cca_` (the suggested prefix for the admin token). They date from
the project's earlier name and are kept so existing tokens, configs and
secret scanners keep working. The status command recognises all three when
it refuses a token on the command line.

## Which process reads what

There are four kinds of process, and they do not share an environment:

| process | started by | mode |
|---|---|---|
| MCP server, stdio | the MCP client (Claude Code) | one local channel |
| MCP server, HTTP | you, or the Docker image | many channels |
| hooks (`ai-agent-channel-session-hook`, `ai-agent-channel-stop-hook`) | Claude Code, per event | local or remote |
| status command (`ai-agent-channel-status`) | a shell, a git hook, or a `Monitor` | local or remote |

The `env` and `headers` of an MCP server entry reach only that server
connection. Hooks and the status command read the environment they were
started in.

"Remote" for hooks and the status command means `AI_AGENT_CHANNEL_URL` is set
**and** a token is available (from `AI_AGENT_CHANNEL_TOKEN_FILE` or
`AI_AGENT_CHANNEL_TOKEN`). Otherwise they read the local database.

## Environment variables

| variable | read by | default | meaning |
|---|---|---|---|
| `AI_AGENT_CHANNEL_ROLE` | stdio server; local hooks; local status command | none | The caller's role. Required for every mailbox tool in stdio mode. Ignored over HTTP, where the token implies the role. |
| `AI_AGENT_CHANNEL_DB` | stdio server; local hooks; local status command | `~/.ai-agent-channel/messages.db` | Path of the single local channel database. The local stop hook keeps its snapshot (`.stop-hook-<role>.json`) in the same directory. |
| `AI_AGENT_CHANNEL_DATA_DIR` | HTTP server | `~/.ai-agent-channel` (`/data` in the Docker image) | Holds `admin.db` and `channels/<name>.db`. Created with mode `0700`; the server refuses to start if it is not writable. |
| `AI_AGENT_CHANNEL_ADMIN_TOKEN` | HTTP server | none (required) | The admin bearer token. Grants the management tools and any board; no mailbox. Compared in constant time. A token shorter than 32 characters starts with a warning. Generate one: `python3 -c "import secrets; print('cca_'+secrets.token_urlsafe(32))"`. |
| `AI_AGENT_CHANNEL_ALLOWED_HOSTS` | HTTP server | unset (no check, warning at startup) | Comma-separated `Host` values the server answers to, e.g. `channel.example.com` or `channel.example.com,127.0.0.1:*` (`name:*` matches any port number). Enables DNS-rebinding protection on every route except `/healthz`: a wrong `Host` gets `421`, a wrong `Origin` gets `403`. Allowed origins are `https://` and `http://` of each host. |
| `AI_AGENT_CHANNEL_TRUST_PROXY` | HTTP server | unset | `1` believes `X-Forwarded-Proto` from any peer, and makes uvicorn accept `X-Forwarded-For`/`X-Forwarded-Proto` from any peer. By default `X-Forwarded-Proto` is believed only from a loopback or private-network peer (a reverse proxy on the same host or Docker network). It decides the board cookie's `Secure` flag. |
| `AI_AGENT_CHANNEL_STATUS_MAX_WAITERS` | HTTP server | `64` | How many `/status?wait=N` long polls may be held at once; the next one gets `429` with `Retry-After: 5`. Values below 1 become 1; a non-integer falls back to the default. |
| `AI_AGENT_CHANNEL_STATUS_MAX_WAITERS_PER_TOKEN` | HTTP server | `8` | How many of those long polls one token may hold at once; same `429` and parsing rules. |
| `AI_AGENT_CHANNEL_VIEW_KEY_TTL_DAYS` | HTTP server | `30` | Lifetime of a board view key, in days. A finite number from 1 to 3650 (fractions allowed); anything else logs a warning and uses 30. Applied when a key is issued; keys issued before expiry existed get an expiry counted from their creation on the first start. |
| `AI_AGENT_CHANNEL_URL` | hooks; status command | unset | Base URL of a hosted server. A trailing `/mcp` is accepted and stripped, so the MCP URL can be reused. Must be `https://`, or `http://` to `127.0.0.1`, `::1` or `localhost`. Redirects are not followed. |
| `AI_AGENT_CHANNEL_TOKEN` | hooks; status command | unset | Role token for remote mode. |
| `AI_AGENT_CHANNEL_TOKEN_FILE` | hooks; status command | unset | Path to a file holding the role token; takes precedence over `AI_AGENT_CHANNEL_TOKEN`. It must be a regular file (not a symbolic link), owned by the reading user, with no group or other permission bits (`chmod 600`), and not empty. A bad file makes the status command exit `2` and makes the stop hook print a warning and skip the check. It is not read by the MCP connection itself; see [`claude-code.md`](claude-code.md#hosted-channel-http). |

Two more variables are not read through the prefix:

- `FORWARDED_ALLOW_IPS` is uvicorn's own setting, read by the HTTP server: the
  comma-separated peers allowed to rewrite the client address and scheme
  with `X-Forwarded-For` and `X-Forwarded-Proto`. Default `127.0.0.1`;
  ignored (every peer is trusted) when `AI_AGENT_CHANNEL_TRUST_PROXY=1`. The
  shipped compose files do not pass it; add it to `environment:` if needed.
- `CHANNEL_DOMAIN` (in `deploy/.env`) is used only by the deployment files:
  the public hostname Caddy obtains a certificate for, and the default for
  `AI_AGENT_CHANNEL_ALLOWED_HOSTS` in `deploy/docker-compose.yml`.

### In Docker

Only variables listed under `environment:` in a compose file reach the
container. Both shipped compose files pass every `AI_AGENT_CHANNEL_*`
variable in `deploy/.env.example`; one left empty in `.env` means the
built-in default. See [`deploy.md`](deploy.md#environment-for-the-container).

## `ai-agent-channel` (the MCP server)

```text
ai-agent-channel [--http] [--host HOST] [--port PORT]
```

| flag | default | meaning |
|---|---|---|
| (none) | | Serve MCP over stdio: one local channel, identity from `AI_AGENT_CHANNEL_ROLE`. This is what an MCP client spawns. |
| `--http` | off | Serve MCP over streamable HTTP at `/mcp`, plus `/healthz`, `/hook-status`, `/status` and `/board`. Requires `AI_AGENT_CHANNEL_ADMIN_TOKEN`. |
| `--host` | `127.0.0.1` | HTTP bind address. Loopback by default; the Docker image passes `0.0.0.0` so the reverse proxy can reach it. |
| `--port` | `8765` | HTTP port. |

`python -m ai_agent_channel` is equivalent. In HTTP mode the process
migrates `admin.db` at startup, sets `umask 077` so channel databases it
creates are mode `0600`, and creates `admin.db` `0600` as well. Files that
already exist keep their modes.

## `ai-agent-channel-status`

```text
ai-agent-channel-status [status|pins|watch] [--role ROLE] [--text]
                        [--interval SECONDS] [--journal PATH]
```

| argument | default | meaning |
|---|---|---|
| `status` | yes | Counters, open debts and the triage lists, as JSON. |
| `pins` | | One entry per pin: key, version, update time and author, `approved_by`, `body_sha256`, `body_length_bytes`. No bodies. |
| `watch` | | Run until interrupted and print one line whenever an actionable counter grows, plus once at start if anything is already pending. See [how `watch` polls](#how-watch-polls). A read failure is printed once, retried with exponential backoff capped at 300 s, and recovery is printed once. |
| `--role` | `AI_AGENT_CHANNEL_ROLE` | Role to report on. Local mode only. |
| `--text` | off | Line-oriented output (TSV for `pins`) instead of JSON. |
| `--interval` | `30` | `watch` only: seconds between polls, and the base of the backoff. Must be at least 1. |
| `--journal` | none | `watch` only: append one JSON line per wake, outage and recovery. |

A token is never accepted as an argument: any argument that starts with
`cct_`, `ccv_` or `cca_`, bare or as `--flag=value`, makes the command
refuse to run, because arguments are visible to every process through `ps`.

### How `watch` polls

- **Remote.** Each request is `GET /status?wait=50`. The server holds it,
  checking about once a second, until an actionable counter is non-zero or
  50 seconds pass. When the request was held and came back with nothing
  pending, the watcher asks again at once, so on a quiet channel a new item
  is reported within about a second. When something is already pending, the
  server answers at once and the watcher waits `--interval` seconds before
  asking again, so a further item is reported within `--interval` (30 s by
  default). A server that does not hold requests is polled every
  `--interval`.
- **Local.** The database is read every `--interval` seconds.

Exit codes:

| code | `status` | `pins` | `watch` |
|---|---|---|---|
| `0` | nothing actionable | printed | stopped with Ctrl-C |
| `1` | something actionable is pending | | |
| `2` | the command failed: channel unreadable (network, TLS, HTTP error, redirect, refused URL, bad token file, unopenable database, no role), a token in the arguments, invalid arguments | same | a token in the arguments, `--interval` below 1, invalid arguments |

"Actionable" is the same set everywhere (`db.ACTIONABLE_COUNTS`): `unread`,
`open_obligations_untaken`, `awaiting_ack`, `unblocked`, `needs_you`,
`awaiting_done`, `resolved_for_you`. The stop hook blocks on it and
`wait_for_mail` wakes on it.

## Hooks

| entry point | alternative | reads |
|---|---|---|
| `ai-agent-channel-session-hook` | `python -m ai_agent_channel.hooks session-start` | nothing; prints the bootstrap and provenance text |
| `ai-agent-channel-stop-hook` | `python -m ai_agent_channel.hooks stop` | the hook JSON on stdin (`stop_hook_active`); `AI_AGENT_CHANNEL_ROLE`, `AI_AGENT_CHANNEL_DB` locally; `AI_AGENT_CHANNEL_URL` with `AI_AGENT_CHANNEL_TOKEN_FILE` or `AI_AGENT_CHANNEL_TOKEN` remotely |

The `python -m` form needs the interpreter that has the package installed
(for a `uv tool` or `pipx` install, the `python` inside that tool's
environment).

When the stop hook blocks and when it passes is described once, in
[`PROTOCOL.md` section 8](../PROTOCOL.md#8-the-session-regimen). Operational
details: the hook always exits `0`; remote checks ask `/hook-status`;
remote snapshots live in `~/.ai-agent-channel/.stop-hook-<hash>-<role>.json`.

Both the hooks and the status command send a `User-Agent` of the form
`ai-agent-channel/<version>` and verify TLS against `certifi`'s CA bundle.

## Fixed limits

These are constants in the source, not configuration. The `constant` column
names the attribute under `ai_agent_channel`; `tests/test_docs.py` compares
each `value` with it. Environment variables above override the two
`STATUS_MAX_WAITERS` defaults and the view key lifetime.

<!-- limits table: checked by tests/test_docs.py -->

| limit | constant | value |
|---|---|---|
| `topic` length, characters | `tools.common.TOPIC_MAX` | 80 |
| pin `key` / `pin_key` length, characters | `tools.common.PIN_KEY_MAX` | 80 |
| pin `title` length, characters | `tools.common.TITLE_MAX` | 200 |
| pin `version` length, characters | `tools.common.VERSION_MAX` | 80 |
| `note`, `reason`, `resolution_note`, each `addenda` value, characters | `tools.common.NOTE_MAX` | 4000 |
| `label` (`upload_content`, `board_link`), characters | `tools.common.LABEL_MAX` | 80 |
| `list_messages` `topic` / `text` filter, characters | `tools.common.FILTER_MAX` | 200 |
| `limit` on listing tools, maximum (minimum 1) | `tools.common.LIMIT_MAX` | 1000 |
| one upload (`upload_content`), bytes of UTF-8 (8 MiB) | `db.blobs.MAX_UPLOAD_BYTES` | 8388608 |
| search query length, characters | `db.search.MAX_QUERY_CHARS` | 4096 |
| search query terms | `db.search.MAX_QUERY_TERMS` | 256 |
| search query parenthesis depth | `db.search.MAX_QUERY_DEPTH` | 256 |
| shortest term the full-text index can use, characters | `db.search.FTS_MIN_TERM` | 3 |
| effective wait per call (`wait_for_reply`, `wait_for_mail`), seconds | `tools.waiting.WAIT_CAP_S` | 50 |
| `timeout_s` accepted, maximum (minimum 0) | `tools.waiting.TIMEOUT_MAX_S` | 3600 |
| `poll_interval_s` accepted, minimum | `tools.waiting.POLL_MIN_S` | 0.1 |
| `poll_interval_s` accepted, maximum | `tools.waiting.POLL_MAX_S` | 60 |
| `/status?wait=N` hold, seconds | `http.STATUS_WAIT_CAP_S` | 50 |
| held `/status` long polls in total (default) | `http.STATUS_MAX_WAITERS` | 64 |
| held `/status` long polls per token (default) | `http.STATUS_MAX_WAITERS_PER_TOKEN` | 8 |
| admin token length below which a warning is logged | `http.ADMIN_TOKEN_MIN_LENGTH` | 32 |
| board link nonce lifetime, seconds (single use) | `auth.BOARD_NONCE_TTL_S` | 300 |
| board session cookie lifetime, seconds (7 days; never past its view key) | `auth.BOARD_SESSION_TTL_S` | 604800 |
| view key lifetime, days (default) | `auth.VIEW_KEY_TTL_DAYS_DEFAULT` | 30 |
| view key lifetime, days (minimum accepted) | `auth.VIEW_KEY_TTL_DAYS_MIN` | 1 |
| view key lifetime, days (maximum accepted) | `auth.VIEW_KEY_TTL_DAYS_MAX` | 3650 |
| live view keys per issuer per channel (issuing another revokes the oldest) | `auth.MAX_VIEW_KEYS_PER_ISSUER` | 20 |
| roles per channel, maximum (minimum 2) | `auth.MAX_ROLES` | 12 |
| SQLite busy timeout, milliseconds | `db.connection.BUSY_TIMEOUT_MS` | 5000 |
| tool calls running at once in worker threads, per event loop | `tools.registry.TOOL_THREADS` | 32 |
| remote stop hook timeout, seconds | `hooks.REMOTE_TIMEOUT_S` | 3 |
| stop hook blocks per stop chain | `hooks.MAX_BLOCKS_PER_STOP` | 3 |
| `watch` default `--interval`, seconds | `cli.WATCH_INTERVAL_S` | 30 |
| `watch` long poll requested, seconds | `cli.WATCH_LONGPOLL_S` | 50 |
| `watch` backoff cap, seconds | `cli.BACKOFF_MAX_S` | 300 |

Other fixed rules:

- Channel, role and board label names are lowercase slugs matching
  `[a-z0-9][a-z0-9_-]{0,63}`.
- SQLite runs in WAL mode; mailbox tools that write run in `BEGIN IMMEDIATE`.
