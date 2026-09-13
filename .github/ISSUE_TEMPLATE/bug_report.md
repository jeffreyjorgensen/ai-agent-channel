---
name: Bug report
about: Something behaves differently from PROTOCOL.md or the documentation
labels: bug
---

<!--
Security problems (one role reaching another's mailbox, a token working
outside its channel, and similar) must NOT be reported here. Use private
vulnerability reporting instead:
https://github.com/jeffreyjorgensen/ai-agent-channel/security/policy

Remove tokens (cct_, ccv_, cca_), hostnames and message bodies you cannot
share from everything you paste.
-->

## What happened

## What you expected

Quote the part of PROTOCOL.md or the docs you relied on, if any.

## How to reproduce

The tool calls in order, with their arguments, and the answer or refusal
text you got:

```python
```

## Environment

- ai-agent-channel commit or version:
- server build (`server_build()["build"]`):
- transport: stdio / hosted HTTP (Docker with Caddy / Docker with nginx / other)
- MCP client and version (for example Claude Code x.y.z):
- Python version and OS:

## Logs

Server log lines, stop hook stderr, or `ai-agent-channel-status` output.
