"""Full-text search: one query language answered by two engines.

The FTS5 trigram index answers when every term is long enough; otherwise the
same query is evaluated over the rows in Python. Both must agree on which
messages qualify, so the scan follows FTS5's grammar: bare words and "phrases"
(case-insensitive substrings), adjacent phrases (implicit AND, tightest), then
NOT, AND, OR, parentheses, a trailing '*', and a 'topic:' / 'body:' column
filter. A query FTS5 cannot parse is retried as a conjunction of literal
whitespace-separated terms by both engines.

Parsing, term collection and matching are iterative, and a query is capped
in length, term count and nesting depth, so no query can exhaust the stack."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .fields import row_to_dict
from .messages import message_filters

# Trigram indexes 3-character windows, so nothing shorter can be looked up.
FTS_MIN_TERM = 3

MAX_QUERY_CHARS = 4096
MAX_QUERY_TERMS = 256
MAX_QUERY_DEPTH = 256

# Characters that are syntax to FTS5 but have no equivalent in the scan.
_UNSUPPORTED_SYNTAX = set("{}+-,^")
_FTS_COLUMNS = ("topic", "body")

# Binary operators by binding strength; adjacency binds tighter than all.
_PRECEDENCE = {"OR": 1, "AND": 2, "NOT": 3}


def fts_available(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
        ).fetchone()
    )


class _QuerySyntaxError(ValueError):
    pass


class QueryLimitError(ValueError):
    """The query exceeds a size limit; refused on every search path."""


@dataclass(frozen=True, slots=True)
class Term:
    text: str
    column: str | None = None


@dataclass(frozen=True, slots=True)
class And:
    children: tuple[Query, ...]


@dataclass(frozen=True, slots=True)
class Or:
    children: tuple[Query, ...]


@dataclass(frozen=True, slots=True)
class Not:
    """`positive NOT n1 NOT n2 ...`: positive and none of the negatives."""

    positive: Query
    negatives: tuple[Query, ...]


Query = Term | And | Or | Not


def _bareword_char(ch: str) -> bool:
    return not ch.isascii() or ch.isalnum() or ch in "_\x1a"


def _query_tokens(query: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    terms = 0
    i, n = 0, len(query)
    while i < n:
        ch = query[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j, text = i + 1, []
            while True:
                if j >= n:
                    raise _QuerySyntaxError("unterminated string")
                if query[j] == '"':
                    if j + 1 < n and query[j + 1] == '"':
                        text.append('"')
                        j += 2
                        continue
                    break
                text.append(query[j])
                j += 1
            tokens.append(("TERM", "".join(text)))
            i = j + 1
        elif ch in "():*":
            tokens.append((ch, ch))
            i += 1
        elif _bareword_char(ch):
            j = i
            while j < n and _bareword_char(query[j]):
                j += 1
            word = query[i:j]
            if word in ("AND", "OR", "NOT"):
                tokens.append((word, word))
            elif word == "NEAR" and query[j:].lstrip().startswith("("):
                raise ValueError(
                    "NEAR(...) cannot be combined with a term shorter than 3 "
                    "characters — lengthen the terms or drop NEAR"
                )
            else:
                tokens.append(("TERM", word))
            i = j
        elif ch in _UNSUPPORTED_SYNTAX:
            raise ValueError(
                f"the query syntax {ch!r} cannot be combined with a term "
                f"shorter than 3 characters — quote the text to search for "
                f"it literally, or lengthen the terms"
            )
        else:
            raise _QuerySyntaxError(f"unexpected {ch!r}")
        if tokens[-1][0] == "TERM":
            terms += 1
            _check_term_count(terms)
    return tokens


def _check_term_count(count: int) -> None:
    if count > MAX_QUERY_TERMS:
        raise QueryLimitError(
            f"a search query may have at most {MAX_QUERY_TERMS} terms — narrow it down"
        )


def _combine(op: str, left: Query, right: Query) -> Query:
    """Join two operands, flattening chains of the same operator so the tree
    depth follows parenthesis nesting, not query length."""
    if op == "NOT":
        if isinstance(left, Not):
            return Not(left.positive, (*left.negatives, right))
        return Not(left, (right,))
    if op == "AND":
        return And((*_flat(left, And), *_flat(right, And)))
    return Or((*_flat(left, Or), *_flat(right, Or)))


def _flat(node: Query, kind: type[And] | type[Or]) -> tuple[Query, ...]:
    return node.children if isinstance(node, kind) else (node,)


def _phrase(tokens: list[tuple[str, str]], pos: int) -> tuple[Term, int]:
    """`[column ':'] TERM ['*']` starting at a TERM token."""
    n = len(tokens)
    column = None
    if pos + 1 < n and tokens[pos + 1][0] == ":":
        column = tokens[pos][1]
        pos += 2
        if column not in _FTS_COLUMNS:
            raise _QuerySyntaxError(f"no such column: {column}")
        if pos < n and tokens[pos][0] == "(":
            raise ValueError(
                "a column filter on a group cannot be combined with a term shorter than 3 characters"
            )
    if pos >= n or tokens[pos][0] != "TERM":
        raise _QuerySyntaxError("expected TERM")
    term = Term(tokens[pos][1], column)
    pos += 1
    if pos < n and tokens[pos][0] == "*":
        pos += 1
    return term, pos


def _parse_query(query: str) -> Query:
    """Operator-precedence parse of the FTS5 subset (see module doc)."""
    tokens = _query_tokens(query)
    if not tokens:
        raise _QuerySyntaxError("empty query")
    n = len(tokens)
    operands: list[Query] = []
    ops: list[str] = []  # "AND" / "OR" / "NOT" / "("
    pos = depth = 0

    def reduce_top() -> None:
        op = ops.pop()
        right = operands.pop()
        operands.append(_combine(op, operands.pop(), right))

    while True:
        # An operand: open groups, then a run of adjacent phrases.
        while pos < n and tokens[pos][0] == "(":
            depth += 1
            if depth > MAX_QUERY_DEPTH:
                raise QueryLimitError(
                    f"a search query may nest parentheses at most {MAX_QUERY_DEPTH} deep"
                )
            ops.append("(")
            pos += 1
        if pos >= n or tokens[pos][0] != "TERM":
            raise _QuerySyntaxError("expected TERM")
        run: list[Query] = []
        while pos < n and tokens[pos][0] == "TERM":
            term, pos = _phrase(tokens, pos)
            run.append(term)
        operands.append(run[0] if len(run) == 1 else And(tuple(run)))
        # Close groups, then an operator or the end.
        while pos < n and tokens[pos][0] == ")":
            while ops and ops[-1] != "(":
                reduce_top()
            if not ops:
                raise _QuerySyntaxError("unexpected ')'")
            ops.pop()
            depth -= 1
            pos += 1
        if pos >= n:
            break
        op = tokens[pos][0]
        if op not in _PRECEDENCE:
            raise _QuerySyntaxError(f"unexpected {tokens[pos][1]!r}")
        while ops and ops[-1] != "(" and _PRECEDENCE[ops[-1]] >= _PRECEDENCE[op]:
            reduce_top()
        ops.append(op)
        pos += 1
    while ops:
        if ops[-1] == "(":
            raise _QuerySyntaxError("expected ')'")
        reduce_top()
    return operands[0]


def _literal_query(query: str) -> Query:
    """What FTS5 is retried with when it cannot parse a query: every
    whitespace-separated piece as a literal phrase, all of them required."""
    terms = query.split()
    _check_term_count(len(terms))
    if len(terms) == 1:
        return Term(terms[0])
    return And(tuple(Term(t) for t in terms))


def _query_terms(root: Query) -> list[str]:
    out: list[str] = []
    stack: list[Query] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, Term):
            out.append(node.text)
        elif isinstance(node, Not):
            stack.append(node.positive)
            stack.extend(node.negatives)
        else:
            stack.extend(node.children)
    return out


def _operands(node: And | Or | Not) -> tuple[Query, ...]:
    if isinstance(node, Not):
        return (node.positive, *node.negatives)
    return node.children


def _query_matches(root: Query, fields: dict[str, str]) -> bool:
    """Evaluate the tree over casefolded fields, short-circuiting, with an
    explicit stack. A frame (node, i) resumes after its operand i - 1, whose
    value is in `result`."""
    stack: list[tuple[Query, int]] = [(root, 0)]
    result = False
    while stack:
        node, i = stack.pop()
        if isinstance(node, Term):
            needle = node.text.casefold()
            columns = (node.column,) if node.column else _FTS_COLUMNS
            result = any(needle in fields[c] for c in columns)
            continue
        if i > 0:
            if isinstance(node, Or):
                if result:
                    continue
            elif isinstance(node, And):
                if not result:
                    continue
            elif i == 1:
                if not result:  # the positive side failed
                    continue
            elif result:  # a negative side matched
                result = False
                continue
        operands = _operands(node)
        if i == len(operands):
            result = not isinstance(node, Or)
            continue
        stack.append((node, i + 1))
        stack.append((operands[i], 0))
    return result


def _is_fts_syntax_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc)
    return text.startswith(("fts5:", "no such column", "unterminated string"))


def _fts_parses(conn: sqlite3.Connection, query: str) -> bool:
    try:
        conn.execute(
            "SELECT 1 FROM messages_fts WHERE messages_fts MATCH ? LIMIT 1", (query,)
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_fts_syntax_error(exc):
            return False
        raise
    return True


def _fts_fallback_query(query: str) -> str:
    """Re-express a query FTS5 refused to parse as a conjunction of quoted
    terms, so punctuation such as 'C++', 'foo-bar' or 'a:b' is literal."""
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _substring_search(
    conn: sqlite3.Connection,
    *,
    tree: Query,
    clauses: list[str],
    params: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    """The no-index path: the parsed query evaluated over each row, newest
    first, stopping at `limit` hits."""
    out: list[dict[str, Any]] = []
    cursor = conn.execute(
        f"SELECT m.* FROM messages m WHERE {' AND '.join(clauses)} ORDER BY m.id DESC",
        params,
    )
    for raw in cursor:
        fields = {"topic": raw["topic"].casefold(), "body": raw["body"].casefold()}
        if not _query_matches(tree, fields):
            continue
        row = row_to_dict(raw)
        row["snippet"] = row["body"][:200]
        row["match"] = "substring"
        out.append(row)
        if len(out) >= limit:
            break
    cursor.close()
    return out


def _parse_for_scan(query: str) -> tuple[Query | None, ValueError | None]:
    """The query tree, or None when the scan must use literal terms. The
    second value is the error for syntax the scan cannot evaluate, raised
    only if the scan is actually needed. Size limits always raise."""
    try:
        return _parse_query(query), None
    except QueryLimitError:
        raise
    except _QuerySyntaxError:
        return None, None
    except ValueError as exc:
        return None, exc


def search_messages(
    conn: sqlite3.Connection,
    *,
    query: str,
    from_role: str | None = None,
    to_role: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Full-text search over topic+body, best match first.

    Falls back to a scan in two cases, both tagged `match: "substring"` in
    the result: this SQLite build has no FTS5, or the query has a term below
    the trigram window. The scan follows the same query semantics.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("'query' is required")
    if len(query) > MAX_QUERY_CHARS:
        raise QueryLimitError(
            f"a search query may be at most {MAX_QUERY_CHARS} characters, got {len(query)}"
        )
    clauses, params = message_filters(
        from_role=from_role, to_role=to_role, kind=kind, status=status
    )
    has_fts = fts_available(conn)
    tree, unsupported = _parse_for_scan(query)
    terms = _query_terms(tree if tree is not None else _literal_query(query))
    too_short = any(len(t) < FTS_MIN_TERM for t in terms)

    if not has_fts or too_short:
        if has_fts and not _fts_parses(conn, query):
            tree = None  # FTS5 retries it as literal terms; so does the scan
        elif unsupported is not None:
            raise unsupported
        return _substring_search(
            conn,
            tree=tree if tree is not None else _literal_query(query),
            clauses=clauses,
            params=params,
            limit=limit,
        )

    sql = (
        "SELECT m.*, snippet(messages_fts, -1, '[', ']', '…', 12) AS snippet, "
        "bm25(messages_fts) AS score "
        "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
        "WHERE messages_fts MATCH :query AND "
        + " AND ".join(clauses)
        +
        # bm25() is negative and MORE negative = better, so plain ASC is
        # "best first"; id DESC breaks ties towards recent messages.
        " ORDER BY score ASC, m.id DESC LIMIT :limit"
    )
    for attempt in (query, _fts_fallback_query(query)):
        if not attempt:
            continue
        try:
            rows = conn.execute(sql, {**params, "query": attempt, "limit": limit}).fetchall()
        except sqlite3.OperationalError as exc:
            if not _is_fts_syntax_error(exc):
                raise
            continue  # unparseable as FTS syntax — retry quoted
        out = [row_to_dict(r) for r in rows]
        for r in out:
            r["match"] = "fts"
        return out
    raise ValueError(
        f"could not parse '{query}' as a search query — try plain words, "
        f'a "quoted phrase", or word AND word'
    )
