## What and why

<!-- The change, and the problem it solves. Link the issue if there is one. -->

## Checklist

- [ ] Tests added or updated, and `uv run --extra dev pytest -q` passes
- [ ] `uv run --extra lint ruff check .` and `uv run --extra lint ruff format --check .` pass
- [ ] `uv run --extra dev --extra lint basedpyright` passes
- [ ] Behaviour change: `PROTOCOL.md` updated (it is served to agents by `get_protocol()`)
- [ ] Tool or parameter change: `docs/reference.md` and the README tool list updated
- [ ] New environment variable, flag or limit: `docs/configuration.md` updated
- [ ] User-visible change: a line in `CHANGELOG.md` under `[Unreleased]`, and under "Breaking changes and upgrade notes" if it can break a client or a deployment
- [ ] Changes how tools must be called: `release.BUILD` bumped with a `WHATS_NEW` entry
- [ ] English only; no real team, project, host or person names

## Design rules

<!-- If this bends a rule in docs/design-rules.md, name the rule and say why. -->
