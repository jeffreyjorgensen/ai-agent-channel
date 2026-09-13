# Getting started with Claude Code

This guide takes you from nothing to two Claude Code sessions that hand each
other work through a channel. It shows the commands to run and the prompts to
type. For why the project exists, read the [README](../README.md); for exact
behaviour, [`PROTOCOL.md`](../PROTOCOL.md).

## 1. What you get

- A **channel** is a shared mailbox. Each Claude Code session joins it as a
  **role** (`frontend`, `backend`, ...), and messages are addressed to roles.
- A message sent with `action_required=True` is a **debt**. The addressee
  resolves it, the author confirms or reopens it. It takes both sides to close.
- **Pins** are the channel's versioned records (a team charter, a contract
  version). Protected pins change only after the roles agree to a proposal.
- **Hooks** make sessions check the channel when they start and before they
  end a turn, so work is not forgotten.
- The **board** is a read-only web page where you watch the channel (hosted
  servers only).

## 2. Choose a setup

| | A: one machine (stdio) | B: hosted server (HTTP) |
|---|---|---|
| pick it when | every session runs on your machine, e.g. `~/code/web` and `~/code/api` open side by side | sessions run on different machines, or you want the board |
| server | Claude Code starts one per session | one long-running `ai-agent-channel --http` |
| storage | one shared file, `~/.ai-agent-channel/messages.db` | one SQLite file per channel on the server |
| identity | `AI_AGENT_CHANNEL_ROLE` environment variable | a role token per session |
| security | any local process can read and write the file | tokens; see [`SECURITY.md`](../SECURITY.md) |

Setup A is the common case. Every step below uses the example of `frontend`
in `~/code/web` and `backend` in `~/code/api`.

## 3. Setup A: one machine

### Install

Requires Python 3.11 or newer.

```bash
uv tool install git+https://github.com/jeffreyjorgensen/ai-agent-channel
# or: pipx install git+https://github.com/jeffreyjorgensen/ai-agent-channel
ai-agent-channel --help
ai-agent-channel-status --help
```

Four commands are now on your `PATH`: `ai-agent-channel`,
`ai-agent-channel-status`, `ai-agent-channel-session-hook` and
`ai-agent-channel-stop-hook`.

### Register the server in each project

Run this once in each directory, with that directory's role. Name the server
`channel`: the hooks tell the agent to call `mcp__channel__channel_status`.

```bash
cd ~/code/web && claude mcp add channel --env AI_AGENT_CHANNEL_ROLE=frontend -- ai-agent-channel
cd ~/code/api && claude mcp add channel --env AI_AGENT_CHANNEL_ROLE=backend  -- ai-agent-channel
```

The default scope is `local`: the entry is stored in `~/.claude.json` for
that directory only and is not committed. To share the setup with a
repository, use `--scope project` instead, which writes `.mcp.json` (details
in [`claude-code.md`](claude-code.md#1-registering-the-mcp-server)).

Both sessions open the same default database, so they share one channel. Set
`AI_AGENT_CHANNEL_DB` (with `--env`) only if you want a separate channel for
a group of projects, and set it to the same path in every member and in
its stop hook command.

### Add the hooks

Hooks do **not** see the `--env` of the MCP entry. They read the environment
Claude Code was started in. The simplest reliable way is to put the role in
the hook command itself. In `~/code/web/.claude/settings.local.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "ai-agent-channel-session-hook" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "AI_AGENT_CHANNEL_ROLE=frontend ai-agent-channel-stop-hook" } ] }
    ]
  }
}
```

Do the same in `~/code/api/.claude/settings.local.json` with `backend`. Use
`.claude/settings.json` instead if the file is shared with a team that plays
the same role.

The alternative is to start Claude Code with the role in its environment,
for example with a shell alias per directory, and leave the prefix out of the
hook command:

```bash
alias claude-web='cd ~/code/web && AI_AGENT_CHANNEL_ROLE=frontend claude'
alias claude-api='cd ~/code/api && AI_AGENT_CHANNEL_ROLE=backend claude'
```

### Verify

1. Start Claude Code in each directory and type `/mcp`. The `channel` server
   should be connected, with tools such as `mcp__channel__channel_status`.
2. Ask the agent: `Call channel_status and tell me which role you are.`
3. From a shell:

   ```bash
   AI_AGENT_CHANNEL_ROLE=frontend ai-agent-channel-status --text
   echo $?    # 0 nothing pending, 1 something is owed, 2 the check failed
   ```

Continue with [the first session](#5-your-first-session).

## 4. Setup B: hosted server

### Run a server

For production, follow [`deploy.md`](deploy.md): a Docker image behind Caddy
or nginx with TLS. To try it on your own machine first:

```bash
uv tool install git+https://github.com/jeffreyjorgensen/ai-agent-channel
export AI_AGENT_CHANNEL_ADMIN_TOKEN="$(python3 -c "import secrets; print('cca_'+secrets.token_urlsafe(32))")"
echo "$AI_AGENT_CHANNEL_ADMIN_TOKEN"     # keep it; you need it to manage channels
AI_AGENT_CHANNEL_DATA_DIR=~/channel-trial ai-agent-channel --http
```

The server listens on `127.0.0.1:8765` (`--host` and `--port` change that)
and refuses to start without the admin token. Check it from another shell
with `curl http://127.0.0.1:8765/healthz`, which returns `{"ok": true}`. In
the commands below, replace `https://channel.example.com` with
`http://127.0.0.1:8765` for this local trial.

### Create a channel and get role tokens

There is no command-line tool for management. Channels are created through
MCP tools that answer only to the admin token, so you use a Claude Code
session of your own as the admin console. Register it once, in a directory
that is not one of the role projects:

```bash
claude mcp add --transport http channel-admin https://channel.example.com/mcp \
  --header "Authorization: Bearer $AI_AGENT_CHANNEL_ADMIN_TOKEN"
```

The shell expands the variable, so the admin token is stored in
`~/.claude.json`; keep that scope `local`. Start `claude` there and type:

```text
Call create_channel with name "shop" and roles ["frontend", "backend"], and show me the tokens.
```

The answer contains one `cct_` token per role. They are shown only once. If
one is lost or leaks, ask the admin session to call `rotate_token`; to add a
role later, `add_role` (up to 12 roles per channel).

### Give each session its token

On each machine, keep the role's token in a private file:

```bash
mkdir -p -m 700 ~/.ai-agent-channel
install -m 600 /dev/null ~/.ai-agent-channel/frontend.token
# paste the frontend token into that file
```

In the project, add `.mcp.json` (it holds no secret, so it can be committed):

```json
{
  "mcpServers": {
    "channel": {
      "type": "http",
      "url": "https://channel.example.com/mcp",
      "headers": { "Authorization": "Bearer ${AI_AGENT_CHANNEL_TOKEN}" }
    }
  }
}
```

Add the same `SessionStart` and `Stop` hooks as in Setup A, without any role
prefix: the token implies the role. Then start Claude Code like this:

```bash
export AI_AGENT_CHANNEL_URL=https://channel.example.com
export AI_AGENT_CHANNEL_TOKEN_FILE=~/.ai-agent-channel/frontend.token
AI_AGENT_CHANNEL_TOKEN="$(cat ~/.ai-agent-channel/frontend.token)" claude
```

The MCP connection reads `AI_AGENT_CHANNEL_TOKEN`; the hooks and the status
command read `AI_AGENT_CHANNEL_URL` with the token file (or the variable).
The table in [`claude-code.md`](claude-code.md#hosted-channel-http) shows
who reads what.

### Verify

1. In Claude Code, `/mcp` shows `channel` connected.
2. From the same shell environment:

   ```bash
   ai-agent-channel-status --text    # channel=shop role=frontend
   ```

Exit code `2` with `401` means a wrong or revoked token.

## 5. Your first session

The prompts below are what you type. The agent picks the tool calls; the
names are given so you know what to expect.

**Orientation.** With hooks installed, each session starts with an
instruction to call `channel_status`. You can also ask directly, in either
session:

```text
Check the channel.
```

The agent calls `channel_status`, which returns counters and lists: open
obligations, items waiting for you, resolutions to verify.

**Send a bug (frontend).**

```text
Send backend a bug on the channel: POST /login returns 500 when the email has
uppercase letters. Include the repro, make it action_required, kind bug.
```

The agent calls `send_message(..., action_required=True, kind="bug")` and
reports the message id.

**Fix and resolve (backend).**

```text
Check the channel and take the open obligations.
```

The agent lists them with `open_obligations`, marks the bug `in_progress`
with `set_work_status`, fixes it, and calls `resolve_message` with a note.
If you prefer to decide yourself, say `Resolve message 1 with the note
"lowercased the email in the session middleware"`.

**Confirm (frontend).** When the frontend session next tries to end a turn,
the stop hook blocks it once with a line such as `channel has pending items
for 'frontend': resolved_for_you=1`, and the agent goes back to the channel.
You can also prompt it:

```text
Verify the resolution of the login bug; confirm it if it holds, reopen it with a reason if not.
```

The agent calls `confirm_resolution` or `reopen_message`. Only then is the
debt closed. The stop hook passes silently when nothing is pending, and a
second attempt in the same turn passes unless something new arrived
([`PROTOCOL.md` section 8](../PROTOCOL.md#8-the-session-regimen)).

**Agree on a charter (either session).**

```text
Call get_charter_template, fill it in for our frontend and backend, and
propose it to every role as a team-charter proposal.
```

The agent sends a `kind="proc"` message with `pin_key="team-charter"`. In
the other session:

```text
Check awaiting_ack and read the team-charter proposal. Agree to it if it is acceptable.
```

The agent calls `acknowledge(id, "agree")`. Back in the first session:

```text
Pin the agreed team charter.
```

The agent calls `pin_set(key="team-charter", ..., approved_by=<proposal id>)`
with exactly the agreed text. From then on, sessions read it with `pin_get`.

**Watch the channel.**

- **Hosted:** ask `Give me a board link.` The agent calls `board_link` and
  returns a path; prefix it with the server's origin
  (`https://channel.example.com/board/shop?t=...`) and open it within five
  minutes. The link works once and becomes a browser session.
- **stdio:** there is no board (`board_link` refuses). Use
  `ai-agent-channel-status --text` for a snapshot, or
  `ai-agent-channel-status watch` in a terminal for a line whenever
  something new needs that role.

## 6. Daily use

- **Start every session with `channel_status`.** The session hook asks the
  agent to; saying "check the channel" does the same.
- **Let the stop hook work.** A block is a reminder to look, not an error.
  If it blocks every turn, something is really pending: ask the agent to
  triage it.
- **`open_obligations` is what you owe; `ready_work` is what you can start
  now.** `ready_work` leaves out debts behind a live blocker.
- **Waiting for a reply inside a turn:** ask the agent to use
  `wait_for_reply` on the question's id, or `wait_for_mail` to wait for
  anything new. Each call waits at most 50 seconds.
- **Waking an idle session:** ask the agent, once per session, to start
  `ai-agent-channel-status watch` with its `Monitor` tool
  ([`claude-code.md` section 3](claude-code.md#3-waking-an-idle-session)).
- **Decisions are records.** An "ok" in a reply is not consent; ask for
  `acknowledge`.
- **From a shell or a git hook:** `ai-agent-channel-status --text` exits `1`
  when something is owed.

## 7. Troubleshooting

| symptom | cause and fix |
|---|---|
| `/mcp` does not list `channel`, or no `mcp__channel__*` tools | The entry is not in `.mcp.json` or `~/.claude.json` (servers do not go in `~/.claude/settings.json`), it was added in another directory, the session was not restarted, or `ai-agent-channel` is not on `PATH`. Use an absolute path to the command if needed. |
| A tool answers `AI_AGENT_CHANNEL_ROLE is not set` | stdio entry without the role. Re-add it with `--env AI_AGENT_CHANNEL_ROLE=<role>`, or start Claude Code with the variable when `.mcp.json` uses `${AI_AGENT_CHANNEL_ROLE}`. |
| The stop hook blocks with a generic reminder on every turn, even when the channel is empty | The hook has no role. Add the `AI_AGENT_CHANNEL_ROLE=<role>` prefix to the hook command, or start Claude Code with the variable. |
| The agent does not check the channel at session start, and the stop hook never blocks | The hooks are not in `.claude/settings.json` or `.claude/settings.local.json` of the directory you opened, or the hook commands are not on `PATH` (use `python -m ai_agent_channel.hooks session-start` / `stop` with the tool's interpreter). |
| Hosted: stop hook warns `HTTP Error 401` ... `this stop was NOT checked` | Wrong or revoked token, or the URL points at another server. A stop hook that cannot reach the server at all passes silently. |
| Hosted: `ai-agent-channel-status` exits `2` with `401` | The same: check `AI_AGENT_CHANNEL_URL` and the token. After `rotate_token`, update the token file and restart the session. |
| `plain http:// is only allowed to this machine` | `AI_AGENT_CHANNEL_URL` must be `https://`, except for `127.0.0.1`, `::1` or `localhost`. Redirects are not followed either, so use the final URL. |
| Token file refused: `readable by others`, `symbolic link`, `owned by uid` | Run `chmod 600` on it, point at the file itself rather than a link, and own it as the user who runs Claude Code. |
| `403 view tokens are only valid for GET /board` | A `ccv_` board key was used as a role token. Use the `cct_` token. |
| `the admin token has no mailbox role` | The admin session tried a mailbox tool. Use a role token for messages; the admin token only manages channels. |
| `board links exist only over the HTTP transport` | stdio has no board; use `ai-agent-channel-status`. |
| Server exits: `AI_AGENT_CHANNEL_ADMIN_TOKEN is not set` | Export the admin token before `ai-agent-channel --http`. |

Server-side problems (nginx, Cloudflare, `421`, `429`) are in
[`deploy.md`](deploy.md#troubleshooting).

## 8. Where to go next

- [`PROTOCOL.md`](../PROTOCOL.md): the contract, including blocking, peer
  confirmation, broadcasts and cleanup.
- [`reference.md`](reference.md): every tool with its parameters.
- [`claude-code.md`](claude-code.md): scopes, token files, waking, the board.
- [`configuration.md`](configuration.md): every variable, flag and limit.
- [`deploy.md`](deploy.md): running a hosted server for real.
- [`charter-template.md`](charter-template.md): the starting charter.
