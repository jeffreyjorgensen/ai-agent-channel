"""What a caller asks about the channel and the server itself: roster, bootstrap
status, the running build, and the bundled protocol and charter template."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .. import db
from ..release import BUILD, NOT_SHIPPED, SHIPPED, WHATS_NEW
from .common import channel_roles, current_role, open_channel_db
from .registry import tool


@tool(
    description=(
        "Who is in this channel. Returns {roles, you, source}. "
        "'source' is 'channel-registry' when the answer comes from the "
        "server's channel definition (hosted mode, authoritative) or "
        "'observed-in-messages' in stdio mode, where there is no registry "
        "and the list is inferred from who has sent or received something — "
        "that variant can under-report a member who has never spoken."
    ),
    read_only=True,
)
def list_roles() -> dict[str, Any]:
    role = current_role()
    with open_channel_db() as conn:
        roles, source = channel_roles(conn)
    return {"roles": roles, "you": role, "source": source}


def news_since(seen_build: str | None) -> list[dict[str, str]]:
    """The WHATS_NEW entries a role that last saw `seen_build` has not been
    shown: those dated on or after that build's date. A role that never saw a
    build, or whose record cannot be read as one, gets every entry."""
    day = (seen_build or "").split(".", 1)[0]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return [dict(item) for item in WHATS_NEW]
    return [dict(item) for item in WHATS_NEW if item["since"] >= day]


@tool(
    description=(
        "Call this at the start of every session AND before wrapping up a "
        "task — the channel is pull-based, nothing will wake you. Returns your "
        "role's bootstrap: a 'counts' summary for one-glance triage; the "
        "NUMBERS of unread messages, open obligations and proposals awaiting "
        "your ack (fetch those lists with read_inbox, open_obligations and "
        "awaiting_ack); and the LISTS of your still-blocked tasks, blocked "
        "tasks whose blocker is gone ('unblocked', resume via "
        "set_work_status), your in_progress tasks (what you left unfinished), "
        "needs_you tasks (the other side put the ball in your court), "
        "awaiting_done tasks (the other side declared done_local and waits "
        "for your 'done'), resolved_for_you (debts the other side closed that "
        "you must verify — confirm_resolution or reopen_message), and the "
        "pinned entries to read with pin_get before contract-related work. "
        "Task lists follow the LAST transition's author: in_progress/"
        "blocked are yours if YOU set them, needs_you/awaiting_done are yours "
        "if the OTHER side did. "
        "The FIRST call your role makes after the server changes also carries "
        "'server.whats_new': what changed since the build you last saw and "
        "what to do differently. It appears once per role per build; "
        "server_build() returns the full list any time."
    )
)
def channel_status() -> dict[str, Any]:
    role = current_role()
    with open_channel_db() as conn:
        # Read without the write lock; only the one-off notice below writes.
        summary = db.channel_summary(conn, role=role)
        seen_key = f"seen_build:{role}"
        seen = db.meta_get(conn, key=seen_key)
        server_block: dict[str, Any] = {
            "build": BUILD,
            "contract": "get_protocol() — the full behavioural contract",
            "changes": "server_build() — everything this build implements",
        }
        if seen != BUILD:
            # Delivering the notice is the point of recording it: a role that
            # has been told does not need telling again, and one that has not
            # must not depend on someone remembering to mention it.
            news = news_since(seen)
            if news:
                server_block["whats_new"] = news
                server_block["note"] = (
                    f"first call on build {BUILD} for '{role}' — these change "
                    f"how you CALL things, not just what the server does. "
                    f"Shown once; server_build() has it any time"
                )
            db.meta_set(conn, key=seen_key, value=BUILD)
        summary["server"] = server_block
        return summary


@tool(
    description=(
        "What this RUNNING server is: its version, what changed and what to "
        "do differently, and the ids of the feature requests this build "
        "implements, with dates. Ask it instead of reading a source tree — "
        "a checkout tells you what some code says, not what the process "
        "answering your calls does. Also lists what is deliberately NOT "
        "implemented and why, so 'missing' and 'refused' stop looking the "
        "same. Call it after an upgrade instead of discovering the change by "
        "breaking against it."
    ),
    read_only=True,
)
def server_build() -> dict[str, Any]:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        ver = pkg_version("ai-agent-channel")
    except PackageNotFoundError:
        ver = "unknown"
    return {
        "build": BUILD,
        "version": ver,
        "protocol_contract": "PROTOCOL.md (get_protocol)",
        # First, because it is the part that is useful without the ids.
        "whats_new": [dict(item) for item in WHATS_NEW],
        "shipped": [{"id": i, "shipped_at": d, "what": w} for i, d, w in SHIPPED],
        "not_shipped": [{"id": i, "why": w} for i, w in NOT_SHIPPED],
        "source": (
            "'shipped' and 'not_shipped' are keyed by the ids of the feature "
            "requests this build implements or declines, so a request can be "
            "checked by its id; 'whats_new' says the same in terms of what to "
            "do differently"
        ),
    }


@tool(
    description=(
        "The full behavioural contract of this channel (PROTOCOL.md): "
        "permission matrix, work_status transition table, debt mechanics, "
        "pin/approval rules, edge-case FAQ. Available to every participant — "
        "read it once when you join a new team instead of asking the partner "
        "'who can do what'. Channel-specific agreements live in pins "
        "(team-charter, contract-version), not here. "
        "It is long: if you only need what CHANGED, call server_build() — the "
        "same facts in a page, with what to do differently."
    ),
    read_only=True,
)
def get_protocol() -> str:
    return _bundled_doc("PROTOCOL.md", Path("PROTOCOL.md"))


@tool(
    description=(
        "A starter team-charter for a NEW channel: mission/goals skeleton "
        "plus ten ground rules (agree-before-build, honest work_status, "
        "explicit consent, respect for the partner's territory, debts never "
        "dropped, a partner's bug is neither a blocker nor a workaround, ...). "
        "Replace the <placeholders> with project specifics, propose it with "
        "send_message(to='*', kind='proc', pin_key='team-charter', "
        "about_message_id=None, voters='*', topic=..., body=<the charter>), "
        "and once every voter has agreed pin it with "
        "pin_set(key='team-charter', title=..., version=..., body=<the same "
        "text>, approved_by=<the proposal id>)."
    ),
    read_only=True,
)
def get_charter_template() -> str:
    return _bundled_doc("charter-template.md", Path("docs") / "charter-template.md")


def _bundled_doc(packaged_name: str, repo_path: Path) -> str:
    # installed package (wheel force-includes the file) …
    try:
        from importlib.resources import files

        return (files("ai_agent_channel") / packaged_name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, TypeError):
        pass
    # … or a source checkout (editable install / tests)
    repo_copy = Path(__file__).resolve().parents[3] / repo_path
    if repo_copy.exists():
        return repo_copy.read_text(encoding="utf-8")
    raise ValueError(f"{packaged_name} is missing from this installation")
