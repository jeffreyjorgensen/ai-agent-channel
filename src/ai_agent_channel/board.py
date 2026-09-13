"""Read-only HTML board: what the channel looks like to a human.

The agents have channel_status(); the person running four of them has had
nothing but `sqlite3` until now. This renders one channel — who owes whom,
which files are claimed, what the pinned contract says, what was said
recently — as a single self-contained page.

Deliberately dumb: server-rendered strings, no JS, no external assets (the
VPS serves this behind Caddy with no CDN reachable), one <meta refresh> for
liveness. It is a VIEW — every route that reaches here is GET-only and
nothing in this module writes to the database.
"""

from __future__ import annotations

from html import escape
from typing import Any

REFRESH_S = 20

_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --dim: #6b7280; --line: #e5e7eb;
  --head: #f9fafb; --accent: #b45309; --ok: #15803d; --warn: #b91c1c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --fg: #e6e6e6; --dim: #9199a6; --line: #262b33;
    --head: #171a21; --accent: #f0b429; --ok: #4ade80; --warn: #f87171;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.5rem; background: var(--bg); color: var(--fg);
  font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
h1 { font-size: 1.15rem; margin: 0 0 .25rem; }
h2 { font-size: .95rem; margin: 2rem 0 .5rem; color: var(--accent);
     text-transform: uppercase; letter-spacing: .06em; }
.sub { color: var(--dim); margin: 0 0 1rem; font-size: .85rem; }
.wrap { max-width: 1100px; margin: 0 auto; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; white-space: nowrap; }
th { background: var(--head); color: var(--dim); font-weight: 600; }
td.wrap-cell { white-space: normal; min-width: 18rem; }
.cards { display: flex; flex-wrap: wrap; gap: .75rem; }
.card { border: 1px solid var(--line); border-radius: 6px; padding: .6rem .8rem;
        min-width: 11rem; flex: 1 1 11rem; }
.card b { display: block; margin-bottom: .3rem; }
.kv { color: var(--dim); }
.kv span { color: var(--fg); }
.zero { color: var(--dim); }
.hot { color: var(--warn); font-weight: 600; }
.empty { color: var(--dim); font-style: italic; }
a { color: var(--accent); }
a.plain { color: inherit; text-decoration: none; border-bottom: 1px dotted var(--dim); }
a.plain:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav { display: flex; gap: 1rem; margin: 0 0 1rem; font-size: .85rem; }
.msg { border: 1px solid var(--line); border-radius: 6px; padding: .7rem .9rem;
       margin: 0 0 .75rem; }
.msg.gone { opacity: .55; border-style: dashed; }
.msg header { display: flex; flex-wrap: wrap; gap: .5rem; align-items: baseline;
              margin-bottom: .5rem; font-size: .85rem; color: var(--dim); }
.msg header b { color: var(--fg); }
.body { white-space: pre-wrap; word-break: break-word; font-size: .9rem;
        border-left: 2px solid var(--line); padding-left: .8rem; }
.tag { border: 1px solid var(--line); border-radius: 999px; padding: 0 .5rem;
       font-size: .75rem; }
.tag.ok { color: var(--ok); border-color: var(--ok); }
.tag.warn { color: var(--warn); border-color: var(--warn); }
.news-box { border: 1px solid var(--line); border-radius: 6px;
            padding: .5rem .8rem; margin: .75rem 0 0; font-size: .85rem; }
.news-box summary { cursor: pointer; color: var(--dim); }
.news-box summary b { color: var(--fg); }
.news-box p { margin: .6rem 0 .2rem; }
ul.news { margin: .4rem 0 .2rem; padding-left: 1.1rem; }
ul.news li { margin: 0 0 .5rem; }
.feed-body { white-space: pre-wrap; word-break: break-word; font-size: .85rem;
             color: var(--dim); margin-top: .35rem; }
"""


def _page(title: str, body: str, *, refresh: bool = True) -> str:
    # Error pages opt out of the refresh: nothing about a 403 gets better by
    # re-requesting it every 20 seconds.
    meta = f"<meta http-equiv='refresh' content='{REFRESH_S}'>" if refresh else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{meta}"
        f"<title>{escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><div class='wrap'>{body}</div></body></html>"
    )


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f"<p class='empty'>{escape(empty)}</p>"
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(r) + "</tr>" for r in rows)
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _link(href: str, text: str) -> str:
    return f"<a class='plain' href='{escape(href)}'>{escape(str(text))}</a>"


def _topic_cell(channel: str, message: dict[str, Any]) -> str:
    """The topic is the way into the conversation — the overview says what
    state things are in, the thread says what was actually said."""
    href = f"/board/{channel}/thread/{message['id']}"
    return f"<td class='wrap-cell'>{_link(href, message['topic'])}</td>"


def _nav(channel: str) -> str:
    return (
        "<div class='nav'>"
        f"<a href='/board/{escape(channel)}'>overview</a>"
        f"<a href='/board/{escape(channel)}/feed'>feed</a>"
        "</div>"
    )


def _cell(value: Any, *, wrap: bool = False) -> str:
    text = "" if value is None else str(value)
    cls = " class='wrap-cell'" if wrap else ""
    return f"<td{cls}>{escape(text)}</td>"


def _short_ts(ts: str | None) -> str:
    # 2026-08-07T11:22:33.123Z → 08-07 11:22 (the year is never the question
    # a human has when scanning a board)
    if not ts or len(ts) < 16:
        return ts or ""
    return ts[5:10] + " " + ts[11:16]


def render_message(title: str, detail: str) -> str:
    """Error/notice page — the board's answers to 401/403/404."""
    return _page(
        title,
        f"<h1>{escape(title)}</h1><p class='sub'>{escape(detail)}</p>",
        refresh=False,
    )


def render_index(channels: list[dict[str, Any]]) -> str:
    rows = [
        [
            f"<td><a href='/board/{escape(c['name'])}'>{escape(c['name'])}</a></td>",
            _cell(", ".join(c["roles"])),
            _cell(_short_ts(c.get("created_at"))),
        ]
        for c in channels
    ]
    body = "<h1>ai-agent-channel</h1><p class='sub'>hosted channels — pick one</p>" + _table(
        ["channel", "roles", "created"], rows, "no active channels"
    )
    return _page("ai-agent-channel", body)


# What each counter on the cards actually measures. A number on a page a
# human reads is a claim about a named role, and this board has already made
# a false one: "to decide 33" counted incoming proposals rather than
# decisions owed, and the owner spent a review asking a role to explain a
# debt it did not have. The label was fixed; the explanation of what the
# number counts belongs next to it, not in a document.
_COUNT_LEGEND = (
    ("unread", "messages this role has not called mark_read on"),
    ("owes", "open debts: action_required, not yet resolved"),
    (
        "decisions pending",
        'proposals this role still owes a vote on. Nudges ("vote on #N"), '
        "read-only proposals and settled rounds are NOT counted here — they "
        "used to be, and the number then measured incoming traffic rather "
        "than decisions",
    ),
    ("ball at them", "tasks whose last transition was made by the other side"),
    ("to verify", "debts closed by the other side, waiting on your confirmation"),
    ("in progress", "tasks this role has declared it is working on"),
)


def _whats_new(build: str | None, items: tuple[dict[str, str], ...]) -> str:
    """The server moved — say so here too.

    The agents get this in channel_status(); the human watching them gets it
    nowhere, and then reads a number whose meaning changed without notice.
    Collapsed by default: it is context for the moment something looks odd,
    not something to read every twenty seconds.
    """
    if not build:
        return ""
    lines = "".join(
        f"<li><b>{escape(i['what'])}</b><br><span class='kv'>{escape(i['do'])}</span></li>"
        for i in items
    )
    body = f"<ul class='news'>{lines}</ul>" if lines else ""
    return (
        "<details class='news-box'><summary>server build "
        f"<b>{escape(build)}</b> — what changed in it</summary>"
        "<p class='kv'>The roles get this same text when they first enter on a "
        "new build. If a number on a card looks different from yesterday, this "
        "is most likely why.</p>"
        f"{body}</details>"
    )


def _legend() -> str:
    items = "".join(
        f"<li><b>{escape(name)}</b> — {escape(text)}</li>" for name, text in _COUNT_LEGEND
    )
    return (
        "<details class='news-box'><summary>what these numbers count</summary>"
        f"<ul class='news'>{items}</ul></details>"
    )


def _counts_cards(snapshot: dict[str, Any]) -> str:
    # Only the counters a human triages on; the full set lives in
    # channel_status() and would turn this into a wall of numbers.
    # "to decide" used to print the raw inbox count of proposals, which
    # measured incoming traffic rather than decisions: a role was shown as
    # owing 33 while genuinely owing none of them — inside the channel that
    # is noise in a list, on a page a human reads it is an accusation of a
    # named role. The counter behind this label now excludes nudges,
    # read-only proposals and settled rounds, so the name and the number
    # describe the same thing.
    shown = [
        ("unread", "unread"),
        ("open_obligations", "owes"),
        ("awaiting_ack", "decisions pending"),
        ("needs_you", "ball at them"),
        ("resolved_for_you", "to verify"),
        ("in_progress", "in progress"),
    ]
    cards = []
    for role in snapshot["roles"]:
        counts = snapshot["counts"].get(role, {})
        items = []
        for key, label in shown:
            n = counts.get(key, 0)
            cls = "hot" if n and key != "in_progress" else ("zero" if not n else "")
            items.append(f"<div class='kv'>{escape(label)} <span class='{cls}'>{n}</span></div>")
        cards.append(f"<div class='card'><b>{escape(role)}</b>{''.join(items)}</div>")
    return f"<div class='cards'>{''.join(cards)}</div>"


def render_board(
    channel: str,
    snapshot: dict[str, Any],
    *,
    build: str | None = None,
    whats_new: tuple[dict[str, str], ...] = (),
) -> str:
    debts = _table(
        ["#", "from", "to", "topic", "work_status", "since"],
        [
            [
                _cell(m["id"]),
                _cell(m["from"]),
                _cell(m["to"]),
                _topic_cell(channel, m),
                _cell(m["work_status"] or "—"),
                _cell(_short_ts(m["created_at"])),
            ]
            for m in snapshot["open_obligations"]
        ],
        "no open obligations — nobody owes anybody",
    )
    pins = _table(
        ["key", "title", "version", "by", "updated"],
        [
            [
                f"<td>{_link('/board/' + channel + '/pin/' + p['key'], p['key'])}</td>",
                _cell(p["title"], wrap=True),
                _cell(p["version"]),
                _cell(p["updated_by"]),
                _cell(_short_ts(p["updated_at"])),
            ]
            for p in snapshot["pins"]
        ],
        "nothing pinned — no charter, no contract version",
    )
    recent = _table(
        ["#", "from", "→", "to", "topic", "kind", "status", "when"],
        [
            [
                _cell(m["id"]),
                _cell(m["from"]),
                "<td>→</td>",
                _cell(m["to"]),
                _topic_cell(channel, m),
                _cell(m["kind"] or "—"),
                _cell(m["status"] or m["work_status"] or "—"),
                _cell(_short_ts(m["created_at"])),
            ]
            for m in snapshot["recent"]
        ],
        "no messages yet",
    )
    body = (
        f"<h1>{escape(channel)}</h1>"
        f"<p class='sub'>{len(snapshot['roles'])} roles · "
        f"{snapshot['totals']['messages']} messages · "
        + (f"build {escape(build)} · " if build else "")
        + f"auto-refresh {REFRESH_S}s</p>"
        + _nav(channel)
        + _counts_cards(snapshot)
        + _legend()
        + _whats_new(build, whats_new)
        + "<h2>Open obligations</h2>"
        + debts
        + "<h2>Pinned</h2>"
        + pins
        + "<h2>Recent messages</h2>"
        + recent
    )
    return _page(f"{channel} · ai-agent-channel", body)


def _recipients(value: Any) -> str:
    return ", ".join(value) if isinstance(value, list) else str(value or "")


def _tags(message: dict[str, Any]) -> str:
    """Only the fields that are set — an empty tag row is worse than none."""
    out = []
    if message.get("deleted_at"):
        out.append("<span class='tag warn'>deleted</span>")
    if message.get("superseded_at"):
        out.append("<span class='tag'>superseded</span>")
    for field, css in (("kind", ""), ("work_status", ""), ("status", "")):
        value = message.get(field)
        if value:
            klass = "ok" if value == "resolved" else (css or "")
            out.append(f"<span class='tag {klass}'>{escape(str(value))}</span>")
    if message.get("action_required"):
        out.append("<span class='tag warn'>debt</span>")
    return "".join(out)


def _message_block(message: dict[str, Any], *, body: bool = True) -> str:
    votes = "".join(
        f"<span class='tag {'ok' if a['decision'] == 'agree' else 'warn'}'>"
        f"{escape(a['role'])}: {escape(a['decision'])}</span>"
        for a in message.get("acks", [])
    )
    moves = "".join(
        f"<div class='kv'>{escape(_short_ts(e['created_at']))} "
        f"{escape(e['role'])} — {escape(e['event'])}"
        + (f": {escape(e['note'])}" if e.get("note") else "")
        + "</div>"
        for e in message.get("events", [])
    )
    gone = " gone" if message.get("deleted_at") else ""
    text = f"<div class='body'>{escape(message.get('body') or '')}</div>" if body else ""
    addenda = ""
    if isinstance(message.get("addenda"), dict):
        addenda = "".join(
            f"<div class='feed-body'><b>{escape(role)}:</b> {escape(tail)}</div>"
            for role, tail in sorted(message["addenda"].items())
        )
    return (
        f"<div class='msg{gone}'><header>"
        f"<b>#{message['id']}</b> <b>{escape(str(message['from']))}</b> → "
        f"<b>{escape(_recipients(message.get('to')))}</b>"
        f"<span>{escape(_short_ts(message.get('created_at')))}</span>"
        f"{_tags(message)}</header>"
        f"<div><b>{escape(message.get('topic') or '')}</b></div>"
        f"{text}{addenda}{votes}{moves}</div>"
    )


def render_thread(channel: str, messages: list[dict[str, Any]]) -> str:
    if not messages:
        return render_message("No such message", "That thread was not found.")
    body = (
        f"<h1>{escape(messages[0].get('topic') or 'thread')}</h1>"
        f"<p class='sub'>{escape(channel)} · {len(messages)} messages</p>"
        + _nav(channel)
        + "".join(_message_block(m) for m in messages)
    )
    return _page(f"thread · {channel}", body, refresh=False)


def render_feed(channel: str, messages: list[dict[str, Any]]) -> str:
    body = (
        f"<h1>{escape(channel)}</h1>"
        f"<p class='sub'>latest {len(messages)} messages · "
        f"auto-refresh {REFRESH_S}s</p>"
        + _nav(channel)
        + (
            "".join(
                _message_block(m).replace(
                    f"<b>{escape(m.get('topic') or '')}</b>",
                    _link(f"/board/{channel}/thread/{m['id']}", m.get("topic") or ""),
                )
                for m in messages
            )
            or "<p class='empty'>nothing yet</p>"
        )
    )
    return _page(f"feed · {channel}", body)


def render_pin(channel: str, key: str, versions: list[dict[str, Any]]) -> str:
    if not versions:
        return render_message("No such pin", f"The key '{key}' has never been pinned.")
    current, *older = versions
    history = _table(
        ["version", "who", "when", "approved_by", "sha256", "bytes"],
        [
            [
                _cell(v["version"]),
                _cell(v["updated_by"]),
                _cell(_short_ts(v["updated_at"])),
                _cell(v.get("approved_by") or "—"),
                _cell((v.get("body_sha256") or "")[:16]),
                _cell(v.get("body_length_bytes")),
            ]
            for v in older
        ],
        "no earlier versions",
    )
    body = (
        f"<h1>{escape(key)}</h1>"
        f"<p class='sub'>{escape(current['title'])} · version "
        f"{escape(current['version'])} · {escape(current['updated_by'])} · "
        f"{escape(_short_ts(current['updated_at']))}</p>"
        + _nav(channel)
        + "<div class='msg'><header>"
        + f"<span>sha256 {escape(current.get('body_sha256') or '—')}</span>"
        + f"<span>{current.get('body_length_bytes')} bytes / "
        + f"{current.get('body_length_chars')} chars</span>"
        + (
            f"<span>approved by message #{current['approved_by']}</span>"
            if current.get("approved_by")
            else "<span class='tag warn'>not approved</span>"
        )
        + "</header>"
        + f"<div class='body'>{escape(current['body'])}</div></div>"
        + "<h2>Version history</h2>"
        + history
    )
    return _page(f"{key} · {channel}", body, refresh=False)
