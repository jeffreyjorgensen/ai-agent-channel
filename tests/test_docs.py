"""The documentation has to match the server, and a test is the only way it will.

A hand-maintained list of tools, parameters or environment variables is a
convention, and a convention nobody enforces is wrong within a week. These
tests compare the documents with the code they describe: the registered MCP
tools, their Python signatures and defaults, the fixed limits and
command-line flags, the environment variables the source reads and the
compose files pass on, the tool calls in examples, relative links and
heading anchors, and the repository's English-only and no-internal-names
rules.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import inspect
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ai_agent_channel import server

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SRC = ROOT / "src" / "ai_agent_channel"
REFERENCE = DOCS / "reference.md"
CONFIGURATION = DOCS / "configuration.md"
README = ROOT / "README.md"
PROTOCOL = ROOT / "PROTOCOL.md"
DEPLOY = ROOT / "deploy"

# Documents whose fenced python examples are checked against tool signatures.
EXAMPLE_DOCS = [README, PROTOCOL, ROOT / "AGENTS.md", *sorted(DOCS.glob("*.md"))]

ENV_RE = re.compile(r"\bAI_AGENT_CHANNEL_[A-Z0-9]+(?:_[A-Z0-9]+)*\b")
# Cyrillic, Cyrillic Supplement, Extended-A and Extended-B, built from code
# points so this file stays ASCII.
CYRILLIC_RE = re.compile(
    "["
    + "".join(
        f"{chr(a)}-{chr(b)}"
        for a, b in ((0x0400, 0x04FF), (0x0500, 0x052F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F))
    )
    + "]"
)
SIGNATURE_ROW_RE = re.compile(r"^\|\s*`([a-z_]+)\((.*?)\)`\s*\|", re.MULTILINE)
FENCE_RE = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)
ANY_FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)|!\[[^\]]*\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)

# sha256 of lowercase words and IPv4 addresses that must never appear in the
# repository (hosts, accounts and projects outside it). Stored as digests so
# the check does not publish what it guards against.
FORBIDDEN_TOKEN_DIGESTS = frozenset(
    {
        "45103180c429e52fd68f6ecd172430602936598ca143175ed66212e799e01cc3",
        "7b5affc93afb6814a0624358575ab32cec6242bf394212961eb88aa2d95611d1",
        "47876d31a0f683f94c9d0f05b6cfd9f3d950a20d0a39249b1eaafbc3b1a0e848",
        "7912dd134e3f76aa722a96beb631eb5bb543fc32344d6a580cbe510f4060d1c9",
    }
)
WORD_RE = re.compile(r"[a-z0-9]+")
IPV4_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")

# The marker in docs/configuration.md above the table of fixed limits.
LIMITS_MARKER = "<!-- limits table: checked by tests/test_docs.py -->"
LIMIT_ROW_RE = re.compile(r"^\|[^|\n]*\|\s*`([A-Za-z_][\w.]*)`\s*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|$")
# Limits an environment variable overrides at import time.
LIMIT_ENV_OVERRIDES = {
    "http.STATUS_MAX_WAITERS": "AI_AGENT_CHANNEL_STATUS_MAX_WAITERS",
    "http.STATUS_MAX_WAITERS_PER_TOKEN": "AI_AGENT_CHANNEL_STATUS_MAX_WAITERS_PER_TOKEN",
}
# Constants the limits table must name, whatever else it lists.
REQUIRED_LIMITS = {
    "TOPIC_MAX",
    "PIN_KEY_MAX",
    "TITLE_MAX",
    "VERSION_MAX",
    "NOTE_MAX",
    "LABEL_MAX",
    "FILTER_MAX",
    "LIMIT_MAX",
    "MAX_UPLOAD_BYTES",
    "MAX_QUERY_CHARS",
    "MAX_QUERY_TERMS",
    "MAX_QUERY_DEPTH",
    "WAIT_CAP_S",
    "STATUS_WAIT_CAP_S",
    "STATUS_MAX_WAITERS",
    "STATUS_MAX_WAITERS_PER_TOKEN",
    "BOARD_NONCE_TTL_S",
    "BOARD_SESSION_TTL_S",
    "VIEW_KEY_TTL_DAYS_DEFAULT",
    "VIEW_KEY_TTL_DAYS_MAX",
    "MAX_VIEW_KEYS_PER_ISSUER",
}

# deploy/.env.example variables that deliberately do not reach the container,
# with the reason. Empty: every server variable is passed through.
NOT_PASSED_TO_CONTAINER: dict[str, str] = {}


def _tools() -> dict[str, Any]:
    """{name: the plain tool function} for every tool the server lists."""
    import asyncio

    listed = asyncio.run(server.mcp.list_tools())
    return {t.name: getattr(server, t.name) for t in listed}


def _signature_rows() -> list[tuple[str, str]]:
    return SIGNATURE_ROW_RE.findall(REFERENCE.read_text(encoding="utf-8"))


def _documented_signatures() -> dict[str, list[str]]:
    """{tool: [parameter names]} from the signature rows of the reference."""
    out: dict[str, list[str]] = {}
    for name, params in _signature_rows():
        assert name not in out, f"docs/reference.md has two rows for {name}"
        out[name] = [p.split("=", 1)[0].strip() for p in params.split(",") if p.strip()]
    return out


def _documented_defaults() -> dict[str, dict[str, str | None]]:
    """{tool: {parameter: default text, or None when documented without one}}."""
    out: dict[str, dict[str, str | None]] = {}
    for name, params in _signature_rows():
        out[name] = {}
        for piece in params.split(","):
            if not piece.strip():
                continue
            param, sep, default = piece.partition("=")
            out[name][param.strip()] = default.strip() if sep else None
    return out


def _tracked_files() -> list[Path]:
    """Files under version control, or every file outside build/tool dirs
    when git is not available (an sdist)."""
    try:
        listed = subprocess.run(
            # tracked plus new-but-not-ignored, so a document is checked
            # before its first commit
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout.decode("utf-8")
        files = [ROOT / p for p in listed.split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        skip = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}
        files = [p for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.parts)]
    return [p for p in files if p.is_file()]


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _markdown_files() -> list[Path]:
    return [p for p in _tracked_files() if p.suffix == ".md"]


# --- tools and signatures ----------------------------------------------------


def test_every_registered_tool_has_a_signature_row():
    missing = sorted(set(_tools()) - set(_documented_signatures()))
    assert not missing, (
        f"these tools exist and have no signature row in docs/reference.md: {missing}"
    )


def test_every_signature_row_names_a_registered_tool():
    stale = sorted(set(_documented_signatures()) - set(_tools()))
    assert not stale, f"docs/reference.md describes tools that do not exist: {stale}"


def test_documented_parameters_match_the_tool_signatures():
    tools = _tools()
    problems = []
    for name, documented in _documented_signatures().items():
        if name not in tools:
            continue
        actual = list(inspect.signature(tools[name]).parameters)  # type: ignore[arg-type]
        if documented != actual:
            problems.append(f"{name}: documented {documented}, actual {actual}")
    assert not problems, "docs/reference.md signatures drifted:\n" + "\n".join(problems)


def _default_problem(documented: str | None, actual: Any, sentinels: tuple[Any, ...]) -> str | None:
    """Why a documented default disagrees with the real one, or None."""
    if documented is None:
        return None if actual is inspect.Parameter.empty else f"undocumented default {actual!r}"
    if actual is inspect.Parameter.empty:
        return f"documented default {documented} but the parameter is required"
    if documented == "<omitted>":
        if any(type(actual) is type(s) and actual == s for s in sentinels):
            return None
        return f"documented <omitted>, actual default {actual!r} is not a sentinel"
    try:
        value = ast.literal_eval(documented)
    except (ValueError, SyntaxError):
        return f"documented default {documented} is not a literal"
    if type(value) is not type(actual) or value != actual:
        return f"documented {documented}, actual {actual!r}"
    return None


def test_documented_defaults_match_the_tool_signatures():
    from ai_agent_channel.tools import messaging

    sentinels = (messaging.OMITTED_KEY, messaging.OMITTED_ID, messaging.OMITTED_VOTERS)
    problems = []
    for name, documented in _documented_defaults().items():
        fn = getattr(server, name, None)
        if fn is None:
            continue  # reported by test_every_signature_row_names_a_registered_tool
        params = inspect.signature(fn).parameters
        for param, default in documented.items():
            if param not in params:
                continue  # reported by test_documented_parameters_match_the_tool_signatures
            problem = _default_problem(default, params[param].default, sentinels)
            if problem:
                problems.append(f"{name}({param}): {problem}")
    assert not problems, "docs/reference.md defaults drifted:\n" + "\n".join(problems)


def test_the_reference_counts_the_tools_correctly():
    text = REFERENCE.read_text(encoding="utf-8")
    match = re.search(r"## MCP tools \((\d+)\)", text)
    assert match, "docs/reference.md lost its '## MCP tools (N)' heading"
    assert int(match.group(1)) == len(_tools())


def test_the_readme_names_every_tool():
    named = set(re.findall(r"`([a-z_]+)[(`]", README.read_text(encoding="utf-8")))
    missing = sorted(set(_tools()) - named)
    assert not missing, f"README.md's tool list does not mention: {missing}"


def test_the_readme_states_the_tool_count():
    words = {
        "Thirty-nine": 39,
        "Forty": 40,
        "Forty-one": 41,
        "Forty-two": 42,
        "Forty-three": 43,
        "Forty-four": 44,
        "Forty-five": 45,
    }
    text = README.read_text(encoding="utf-8")
    stated = next((n for w, n in words.items() if f"{w} MCP tools" in text), None)
    assert stated is not None, (
        "README.md no longer states the tool count as '<Number> MCP tools'; "
        "teach this test the new wording rather than deleting it"
    )
    assert stated == len(_tools())


# --- limits and flags --------------------------------------------------------


def _documented_limits() -> list[tuple[str, str]]:
    text = CONFIGURATION.read_text(encoding="utf-8")
    assert LIMITS_MARKER in text, f"docs/configuration.md lost the marker {LIMITS_MARKER!r}"
    rows = []
    for line in text.split(LIMITS_MARKER, 1)[1].lstrip("\n").splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        match = LIMIT_ROW_RE.match(line)
        if match:
            rows.append((match.group(1), match.group(2)))
    return rows


def test_documented_limits_match_the_code():
    rows = _documented_limits()
    problems = []
    for constant, value in rows:
        module_name, _, attribute = constant.rpartition(".")
        module = importlib.import_module(f"ai_agent_channel.{module_name}")
        if not hasattr(module, attribute):
            problems.append(f"{constant}: no such constant")
            continue
        env = LIMIT_ENV_OVERRIDES.get(constant)
        if env and os.environ.get(env, "").strip():
            continue  # overridden in this environment; the default cannot be read
        actual = getattr(module, attribute)
        if float(actual) != float(value):
            problems.append(f"{constant}: documented {value}, actual {actual!r}")
    named = {constant.rpartition(".")[2] for constant, _ in rows}
    missing = sorted(REQUIRED_LIMITS - named)
    assert not missing, f"docs/configuration.md's limits table does not name: {missing}"
    assert not problems, "docs/configuration.md limits drifted:\n" + "\n".join(problems)


class _Parser(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__(parser.prog)
        self.parser = parser


def _parser_built_by(entry, monkeypatch: pytest.MonkeyPatch) -> argparse.ArgumentParser:
    """The parser an entry point builds, captured at parse_args before it runs
    anything."""

    def capture(self, *args, **kwargs):
        raise _Parser(self)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(_Parser) as caught:
        entry()
    return caught.value.parser


def test_every_command_line_flag_is_documented(monkeypatch: pytest.MonkeyPatch):
    from ai_agent_channel import cli

    main_module = importlib.import_module("ai_agent_channel.__main__")
    text = CONFIGURATION.read_text(encoding="utf-8")
    problems = []
    for entry in (lambda: main_module.main([]), lambda: cli.run([])):
        parser = _parser_built_by(entry, monkeypatch)
        for action in parser._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            names = [o for o in action.option_strings if o.startswith("--")]
            names += [str(c) for c in (action.choices or [])] if not action.option_strings else []
            for name in names:
                if f"`{name}`" not in text:
                    problems.append(f"{parser.prog}: {name}")
    assert not problems, "command-line arguments missing from docs/configuration.md:\n" + (
        "\n".join(problems)
    )


# --- environment variables ---------------------------------------------------


def _env_vars_in_source() -> set[str]:
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        found |= set(ENV_RE.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_env_var_in_the_source_is_documented():
    documented = set(ENV_RE.findall(CONFIGURATION.read_text(encoding="utf-8")))
    missing = sorted(_env_vars_in_source() - documented)
    assert not missing, f"docs/configuration.md does not document: {missing}"


def test_every_documented_env_var_exists():
    documented = set(ENV_RE.findall(CONFIGURATION.read_text(encoding="utf-8")))
    invented = sorted(documented - _env_vars_in_source())
    assert not invented, f"docs/configuration.md lists variables the code never reads: {invented}"


def test_env_vars_named_anywhere_in_docs_or_deploy_exist():
    known = _env_vars_in_source()
    files = [
        p
        for p in _tracked_files()
        if p.suffix in {".md", ".yml", ".example", ".conf", ".sh"} or p.name == "Dockerfile"
    ]
    problems = []
    for path in files:
        text = _text(path) or ""
        for var in sorted(set(ENV_RE.findall(text)) - known):
            problems.append(f"{path.relative_to(ROOT)}: {var}")
    assert not problems, "unknown AI_AGENT_CHANNEL_* variables:\n" + "\n".join(problems)


def test_every_env_example_variable_reaches_the_container():
    example = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    offered = set(re.findall(r"^#?\s*(AI_AGENT_CHANNEL_[A-Z0-9_]+)=", example, re.MULTILINE))
    assert offered, "deploy/.env.example names no AI_AGENT_CHANNEL_* variable"
    problems = []
    for compose in ("docker-compose.yml", "docker-compose.vps.yml"):
        text = (DEPLOY / compose).read_text(encoding="utf-8")
        passed = set(re.findall(r"^\s+(AI_AGENT_CHANNEL_[A-Z0-9_]+):", text, re.MULTILINE))
        for var in sorted(offered - passed - set(NOT_PASSED_TO_CONTAINER)):
            problems.append(f"deploy/{compose}: {var}")
    assert not problems, (
        "variables offered in deploy/.env.example that never reach the container "
        "(add them under environment:, or to NOT_PASSED_TO_CONTAINER with a reason):\n"
        + "\n".join(problems)
    )


# --- examples ----------------------------------------------------------------


def _python_examples() -> list[tuple[Path, int, str]]:
    blocks = []
    for path in EXAMPLE_DOCS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for match in FENCE_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 2
            blocks.append((path, line, match.group(1)))
    return blocks


def test_python_examples_parse():
    problems = []
    for path, line, code in _python_examples():
        try:
            ast.parse(code)
        except SyntaxError as exc:
            problems.append(f"{path.relative_to(ROOT)}:{line + (exc.lineno or 1) - 1}: {exc.msg}")
    assert not problems, "python examples that do not parse:\n" + "\n".join(problems)


def _tool_calls(code: str) -> list[ast.Call]:
    tools = _tools()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # reported by test_python_examples_parse
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in tools:
                calls.append(node)
    return calls


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr  # type: ignore[attr-defined]


def test_python_examples_call_tools_with_real_parameters():
    tools = _tools()
    problems = []
    for path, line, code in _python_examples():
        for call in _tool_calls(code):
            name = _call_name(call)
            params = list(inspect.signature(tools[name]).parameters)  # type: ignore[arg-type]
            where = f"{path.relative_to(ROOT)}:{line + call.lineno - 1}: {name}"
            for kw in call.keywords:
                if kw.arg is not None and kw.arg not in params:
                    problems.append(f"{where}: no parameter '{kw.arg}'")
            positional = [a for a in call.args if not isinstance(a, ast.Starred)]
            if len(positional) > len(params):
                problems.append(f"{where}: {len(positional)} positional arguments")
    assert not problems, "examples that would fail against the server:\n" + "\n".join(problems)


def test_proposal_examples_give_the_required_answers():
    """kind='proc' without pin_key and about_message_id is refused by the
    server; an example that omits them teaches a call that cannot work."""
    problems = []
    for path, line, code in _python_examples():
        for call in _tool_calls(code):
            if _call_name(call) != "send_message":
                continue
            kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            kind = kwargs.get("kind")
            if not (isinstance(kind, ast.Constant) and kind.value == "proc"):
                continue
            where = f"{path.relative_to(ROOT)}:{line + call.lineno - 1}"
            for required in ("pin_key", "about_message_id"):
                if required not in kwargs:
                    problems.append(f"{where}: kind='proc' without {required}")
            key = kwargs.get("pin_key")
            has_key = key is not None and not (isinstance(key, ast.Constant) and key.value is None)
            if has_key and "voters" not in kwargs:
                problems.append(f"{where}: a pin proposal without voters")
    assert not problems, "\n".join(problems)


# --- repository-wide text rules ---------------------------------------------


def test_no_cyrillic_in_tracked_files():
    problems = []
    for path in _tracked_files():
        text = _text(path)
        if text is None:
            continue
        for number, content in enumerate(text.splitlines(), 1):
            if CYRILLIC_RE.search(content):
                problems.append(f"{path.relative_to(ROOT)}:{number}")
                break
    assert not problems, "Cyrillic text in tracked files (English only):\n" + "\n".join(problems)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def test_no_internal_names_in_tracked_files():
    problems = []
    for path in _tracked_files():
        text = _text(path)
        if text is None:
            continue
        for number, content in enumerate(text.splitlines(), 1):
            tokens = set(WORD_RE.findall(content.lower())) | set(IPV4_RE.findall(content))
            if any(_digest(t) in FORBIDDEN_TOKEN_DIGESTS for t in tokens):
                problems.append(f"{path.relative_to(ROOT)}:{number}")
    assert not problems, "internal host, account or project names:\n" + "\n".join(problems)


def test_relative_markdown_links_resolve():
    problems = []
    for path in _markdown_files():
        # links inside fenced code are examples, not navigation
        text = ANY_FENCE_RE.sub("", path.read_text(encoding="utf-8"))
        for match in LINK_RE.finditer(text):
            target = match.group(1) or match.group(2)
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            file_part = target.split("#", 1)[0]
            if file_part and not (path.parent / file_part).exists():
                problems.append(f"{path.relative_to(ROOT)}: {target}")
    assert not problems, "broken relative links:\n" + "\n".join(problems)


def _heading_anchors(path: Path) -> set[str]:
    """The anchors GitHub generates for a document's headings: lowercase,
    punctuation other than '-' and '_' dropped, spaces as '-', and '-1',
    '-2' ... for repeats."""
    text = ANY_FENCE_RE.sub("", path.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    anchors = set()
    for match in HEADING_RE.finditer(text):
        heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1).rstrip("#").strip())
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def test_markdown_anchors_resolve():
    problems = []
    cache: dict[Path, set[str]] = {}
    for path in _markdown_files():
        text = ANY_FENCE_RE.sub("", path.read_text(encoding="utf-8"))
        for match in LINK_RE.finditer(text):
            target = match.group(1) or match.group(2)
            if "#" not in target or re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue
            file_part, anchor = target.split("#", 1)
            document = (path.parent / file_part).resolve() if file_part else path
            if document.suffix != ".md" or not document.exists():
                continue  # a missing file is reported by test_relative_markdown_links_resolve
            anchors = cache.setdefault(document, _heading_anchors(document))
            if anchor not in anchors:
                problems.append(f"{path.relative_to(ROOT)}: {target}")
    assert not problems, "links to headings that do not exist:\n" + "\n".join(problems)


def test_the_index_reaches_every_document():
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    for name in (
        "reference.md",
        "configuration.md",
        "claude-code.md",
        "deploy.md",
        "design-rules.md",
        "charter-template.md",
        "PROTOCOL.md",
        "CHANGELOG.md",
    ):
        assert name in index, f"docs/README.md does not link {name}"
    rules = (DOCS / "design-rules.md").read_text(encoding="utf-8")
    assert "A mechanism or nothing" in rules
