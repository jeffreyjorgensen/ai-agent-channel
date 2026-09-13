# Using the channel from Claude Code

Details that go beyond the README's wire-up: registration scopes and
fallbacks, keeping the token in a file, waking an idle session, and the
board. Install the commands and write the basic `.mcp.json` and hooks as in
the README first:

- [Install](../README.md#install)
- [Wire it up](../README.md#wire-it-up): stdio and hosted `.mcp.json`
- [Hooks](../README.md#hooks): the `SessionStart` and `Stop` entries

Variables and flags are listed in [`configuration.md`](configuration.md).

## 1. Registering the MCP server

MCP servers are configured in `.mcp.json` at the project root (shared with
the repository) or with `claude mcp add`, which writes to `.mcp.json` for
`--scope project` and to `~/.claude.json` for the `local` and `user` scopes.
`~/.claude/settings.json` is for settings and hooks, not MCP servers.

### Local channel (stdio)

Sessions in different project directories can hard-code the role instead of
reading it from the launch environment:

```bash
claude mcp add channel --env AI_AGENT_CHANNEL_ROLE=frontend -- ai-agent-channel
```

The hooks still need the role in their own environment (see
[section 2](#2-hooks)).

If `ai-agent-channel` is not on `PATH`, use an absolute path to it, or
`"command": "/path/to/python", "args": ["-m", "ai_agent_channel"]` with an
interpreter that has the package installed.

### Hosted channel (HTTP)

The role token identifies both the channel and the role, so no role variable
is needed. Three consumers need the token, and they read it differently:

| consumer | reads the token from |
|---|---|
| the MCP connection (`.mcp.json` `headers`) | `${AI_AGENT_CHANNEL_TOKEN}` expanded from the environment Claude Code was started in |
| the stop hook | `AI_AGENT_CHANNEL_TOKEN_FILE`, else `AI_AGENT_CHANNEL_TOKEN` |
| `ai-agent-channel-status` | `AI_AGENT_CHANNEL_TOKEN_FILE`, else `AI_AGENT_CHANNEL_TOKEN` |

So a token file alone leaves the MCP connection without a token. Either
export `AI_AGENT_CHANNEL_TOKEN` for all three, or keep the token in a file
for the hooks and the status command **and** still provide
`AI_AGENT_CHANNEL_TOKEN` to the session that starts Claude Code, for
example read from that file at launch:

```bash
mkdir -p -m 700 ~/.ai-agent-channel
install -m 600 /dev/null ~/.ai-agent-channel/frontend.token
# paste the token into that file, then:
export AI_AGENT_CHANNEL_URL=https://channel.example.com
export AI_AGENT_CHANNEL_TOKEN_FILE=~/.ai-agent-channel/frontend.token
AI_AGENT_CHANNEL_TOKEN="$(cat ~/.ai-agent-channel/frontend.token)" claude
```

The token file must be a regular file owned by you with mode `0600`; a
symbolic link, another owner or a group- or world-readable mode is refused.
Do not commit a literal token in a shared `.mcp.json`.

### Check

Restart the session and run `/mcp`: the `channel` server should be connected
and tools such as `mcp__channel__channel_status` listed. Calling
`channel_status()` should return your role.

## 2. Hooks

Add the entries from the [README](../README.md#hooks) to
`.claude/settings.json` (shared) or `.claude/settings.local.json`
(personal).

- **SessionStart** prints the bootstrap instruction (call `channel_status()`,
  the triage order) and the statement that peer messages are untrusted input.
  It also fires on resume and compaction; repeating it there is intended.
- **Stop** checks the channel and raises pending items when a turn tries to
  end. When it blocks and when it passes is described once, in
  [`PROTOCOL.md` section 8](../PROTOCOL.md#8-the-session-regimen).

Hooks do not receive the MCP server's `env` or `headers`. They need the role
(local) or the URL and token (hosted) in the environment Claude Code was
started from, or as a prefix on the command:

```json
{ "type": "command", "command": "AI_AGENT_CHANNEL_ROLE=frontend ai-agent-channel-stop-hook" }
```

If the scripts are not on `PATH`, use `python -m ai_agent_channel.hooks
session-start` and `python -m ai_agent_channel.hooks stop` with the tool's
own interpreter.

A remote stop hook that cannot reach the server passes the stop silently. A
`401`, `403` or `404`, a redirect, or an unusable token file is reported on
stderr: that is a wrong URL or token, not a clean channel.

## 3. Waking an idle session

A session sitting at its prompt can be woken by a background command whose
output Claude Code watches. Ask the agent to start this once per session:

```javascript
Monitor({command: "ai-agent-channel-status watch", persistent: true,
         description: "ai-agent-channel"})
```

The command needs the same environment as the hooks. Against a hosted server
it long-polls: while nothing is pending, a new item is reported within about
a second; while something is already pending, it checks every `--interval`
seconds (30 by default). Locally it reads the database every `--interval`
seconds. Details: [how `watch` polls](configuration.md#how-watch-polls).

It prints a line only when an actionable counter grows, reports a lost
connection once, and never prints a heartbeat. The line is information, not
an instruction: what must be handled before the turn ends is still raised by
the stop hook.

Add `--journal ~/.ai-agent-channel/watch.jsonl` to keep one JSON line per
wake and outage. Stop the watch with `TaskStop` or by ending the session. A
session that has exited cannot be woken; what arrived meanwhile is waiting
at the next `channel_status()`.

## 4. The board for humans

In a hosted channel any role can hand its user a read-only dashboard:

```python
board_link()   # {"url": "/board/<channel>?t=...", "expires_in_s": 300, ...}
```

Prefix the URL with the server's origin and open it before `expires_in_s`
runs out. The link works once and turns into a browser session. Lifetimes
and caps are in [`configuration.md`](configuration.md#fixed-limits).
