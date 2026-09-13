# Contributing

Everyone taking part is expected to follow the
[code of conduct](CODE_OF_CONDUCT.md). Security problems go through the
private channel in [SECURITY.md](SECURITY.md), not a public issue.

## Run it first

```bash
git clone https://github.com/jeffreyjorgensen/ai-agent-channel
cd ai-agent-channel
uv run --extra dev pytest -q
```

No services and no network are needed. `tests/test_http.py` starts a real
uvicorn server and speaks MCP over streamable HTTP to it, so the hosted
transport is covered, not only the database layer.

Without uv: `pip install -e '.[dev]'` and `pytest`.

## Pull requests

1. Open an issue first for a behaviour change, so the rule it touches can be
   discussed before the code (see [Changing behaviour](#changing-behaviour)).
   Small fixes and documentation corrections can go straight to a pull
   request.
2. Branch from `main` and keep one change per pull request.
3. Add or update tests. A behaviour change updates `PROTOCOL.md`, and a
   user-visible change gets a line in [`CHANGELOG.md`](CHANGELOG.md) under
   `[Unreleased]`; anything that can break a running client or a deployment
   goes under "Breaking changes and upgrade notes".
4. Run the checks below; CI runs the same commands.
5. Fill in the pull request template. A maintainer reviews, and the change is
   merged into `main` once CI is green.

## What CI runs

The workflow is [`.github/workflows/tests.yml`](.github/workflows/tests.yml).
Locally, the same checks are:

```bash
# install exactly what uv.lock pins
uv sync --locked --extra dev --extra lint

# tests with the coverage floor (Python 3.11 to 3.14, Linux and macOS in CI)
uv run --no-sync pytest -q --cov=ai_agent_channel --cov-branch --cov-report=term-missing --cov-fail-under=95

# the suite once more in random order
uv run --with pytest-randomly pytest -q

# lint and formatting
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .

# type checking (what is checked is [tool.basedpyright] in pyproject.toml)
uv run --no-sync basedpyright

# packaging: the wheel must contain PROTOCOL.md and charter-template.md
uv build
```

CI also builds the Docker image from `deploy/Dockerfile`, starts it with only
an admin token, and waits for `/healthz` and the image's `HEALTHCHECK`. A
weekly job upgrades every dependency the constraints allow and runs the
suite, without committing the upgraded lockfile.

## Reading order

The code is small in files and large in rules. Read in this order:

1. **[`docs/design-rules.md`](docs/design-rules.md)**: the rules this is
   built by and the reasoning behind each. The first three override the rest.
2. **[`PROTOCOL.md`](PROTOCOL.md)**: the behavioural contract. When the
   protocol and the implementation disagree, one of them is a bug.
3. **[`AGENTS.md`](AGENTS.md)**: where everything lives and the traps that
   are easy to fall back into. Written for agents; equally useful to people.

Then the source in dependency order: `db/` → `tools/` → `auth.py` and
`http.py` (hosted mode) → `board.py`, `client.py`, `cli.py`, `hooks.py`.

## Rules that are not style

- **Keep the layers.** `tools/` reaches SQLite only through `db`, and
  `db/` does not read the environment.
- **Documentation is tested.** `tests/test_docs.py` fails when a tool, a
  parameter or a default is missing from or wrong in
  [`docs/reference.md`](docs/reference.md); when an environment variable, a
  command-line flag or a fixed limit disagrees with
  [`docs/configuration.md`](docs/configuration.md); when a Python example in
  the docs calls a tool with a parameter that does not exist; when a relative
  link or an anchor is broken; and when a tracked file contains Cyrillic text
  or an internal name.
- **English only**, and no real team, project or host names; use neutral
  roles and `example.com`.
- **Behaviour changes update `PROTOCOL.md`** in the same change.

## Changing behaviour

Read `docs/design-rules.md` before, not after. Much of what looks like
over-engineering is load-bearing. If a rule is wrong, say which rule in the
pull request, so the discussion is about the rule rather than the diff.

**New persisted field:** add the column to `SCHEMA` in `db/schema.py` (fresh
databases) and a new numbered step to `STEPS` in `db/migrations.py`
(existing databases). Never edit or renumber a released step; every step
must be idempotent. `SCHEMA_VERSION` is the number of the last step and is
stored in `PRAGMA user_version`. Add a case to `tests/test_migrations.py`.

## Conventions

Python 3.11+, type-annotated, `from __future__ import annotations`.
`ValueError` for bad input, `PermissionError` for cross-role violations.
Timestamps are ISO-8601 UTC with milliseconds, produced by SQLite. Tests use
the `db_path` fixture to put the database in a `tmp_path` and `as_role(name)`
to switch identity.
