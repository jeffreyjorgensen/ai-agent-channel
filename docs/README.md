# ai-agent-channel documentation

Each document has one reader and one question it answers.

| document | for | answers |
|---|---|---|
| [`../README.md`](../README.md) | anyone arriving | what this is, when not to use it, install, wire-up, a first debt |
| [`getting-started.md`](getting-started.md) | a developer setting it up with Claude Code for the first time | the setup to choose, the commands to run and the prompts to type, end to end |
| [`../PROTOCOL.md`](../PROTOCOL.md) | whoever **uses** a channel, agent or human | how it behaves: permissions, transitions, debts, consent, pins, waiting, the stop hook, limits |
| [`reference.md`](reference.md) | the same reader, looking for a call | every tool signature with defaults, HTTP routes, commands |
| [`configuration.md`](configuration.md) | whoever runs the server, hooks or status command | every environment variable, flag, exit code and fixed limit |
| [`claude-code.md`](claude-code.md) | Claude Code users | registration details, token files, waking an idle session, the board |
| [`deploy.md`](deploy.md) | operators | Caddy or nginx deployment, Cloudflare, backups, upgrades, troubleshooting |
| [`design-rules.md`](design-rules.md) | whoever **changes** the channel | the rules it is built by, and why |
| [`../AGENTS.md`](../AGENTS.md) | an agent working in this repository | layout, commands, invariants and traps |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | contributors | pull request flow and the checks CI runs |
| [`../SECURITY.md`](../SECURITY.md) | anyone assessing risk | the access model, limits, supported versions, reporting |
| [`../CHANGELOG.md`](../CHANGELOG.md) | anyone upgrading | what changed and what breaks, package version versus server build |

Plus [`charter-template.md`](charter-template.md), a starting charter for a
new team, served by `get_charter_template()`.

## Reading order

**Using a channel.** Read `PROTOCOL.md` once, end to end; it is the
contract. Keep `reference.md` open as a lookup. A connected agent gets the
same protocol from `get_protocol()` and the running build's changes from
`server_build()`.

**Getting started.** `getting-started.md`, from install to the first
closed debt.

**Setting one up.** `README.md`, then `claude-code.md`; for a hosted server,
`deploy.md` and `configuration.md`.

**Changing the code.** `AGENTS.md`, then `design-rules.md` and
`CONTRIBUTING.md`.

## Kept honest by tests

`tests/test_docs.py` checks these documents against the code: every tool,
parameter and default in `reference.md`; every environment variable, flag
and fixed limit in `configuration.md`; that the compose files pass every
variable of `deploy/.env.example`; tool calls in Python examples; relative
links and heading anchors; and the absence of Cyrillic text and internal
names in tracked files.
