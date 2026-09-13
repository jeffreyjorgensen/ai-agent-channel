"""Tool declaration and ordered registration: modules declare tools with @tool,
and the package registers them once, in catalogue order.

Every tool body does blocking SQLite work (a writer may wait up to the busy
timeout for the lock). FastMCP calls a sync tool function inline on the event
loop, so one waiting write would freeze every channel, /healthz and the board.
register() therefore hands FastMCP an async wrapper that runs the function in
a worker thread, with the caller's contextvars (the request identity) copied
in. The module attribute stays the plain sync function, so `server.<tool>(...)`
is still an ordinary call.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import anyio
import anyio.to_thread
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

_F = TypeVar("_F", bound=Callable[..., Any])
_T = TypeVar("_T")

# Worker threads tool calls may occupy at once, per event loop. Its own limiter
# rather than anyio's default one, so a burst of tool calls waiting on a lock
# cannot starve other users of the default pool (the stdio transport reads
# stdin through it).
TOOL_THREADS = 32

_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter] = (
    weakref.WeakKeyDictionary()
)


@dataclass(frozen=True)
class Declaration:
    fn: Callable[..., Any]
    description: str
    title: str | None
    annotations: ToolAnnotations | None


_declared: dict[str, Declaration] = {}


def qualified_name(fn: Callable[..., Any]) -> str:
    return f"{fn.__module__}.{fn.__qualname__}"


def tool(
    *,
    description: str,
    title: str | None = None,
    annotations: ToolAnnotations | None = None,
    read_only: bool = False,
) -> Callable[[_F], _F]:
    """Declare an MCP tool without registering it, so the order clients list
    tools in is the catalogue's, not the order modules happen to be imported.

    read_only=True is shorthand for annotations with readOnlyHint set: the
    tool changes nothing a client could observe.
    """
    if read_only:
        annotations = (annotations or ToolAnnotations()).model_copy(update={"readOnlyHint": True})

    def declare(fn: _F) -> _F:
        name = qualified_name(fn)
        if name in _declared:
            raise RuntimeError(f"tool {name} is declared twice")
        _declared[name] = Declaration(fn, description, title, annotations)
        return fn

    return declare


def _limiter() -> anyio.CapacityLimiter:
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = _limiters[loop] = anyio.CapacityLimiter(TOOL_THREADS)
    return limiter


async def run_in_thread(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run blocking `fn` in a worker thread with a copy of the caller's
    contextvars — the request identity lives in one — bounded by TOOL_THREADS."""
    ctx = contextvars.copy_context()

    def call() -> _T:
        return ctx.run(fn, *args, **kwargs)

    return await anyio.to_thread.run_sync(call, limiter=_limiter())


def off_the_loop(fn: Callable[..., Any]) -> Callable[..., Any]:
    """An async twin of sync `fn` that runs it via run_in_thread. It keeps the
    signature, annotations and name, so FastMCP builds the same input and
    output schema from it as from `fn`. Async functions are returned as is."""
    if inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Any:
        return await run_in_thread(fn, **kwargs)

    # evaluated: the tool modules use postponed annotations, and FastMCP
    # builds its pydantic models from real types
    wrapper.__signature__ = inspect.signature(fn, eval_str=True)  # pyright: ignore[reportAttributeAccessIssue]
    return wrapper


def register(mcp: FastMCP, catalogue: Sequence[Callable[..., Any]]) -> None:
    """Register every declared tool in catalogue order; a declared tool missing
    from the catalogue, a catalogue entry never declared, or one listed twice
    is an error that names it."""
    listed = [qualified_name(fn) for fn in catalogue]
    seen: set[str] = set()
    duplicates = sorted({n for n in listed if n in seen or seen.add(n)})
    unlisted = sorted(set(_declared) - set(listed))
    undeclared = sorted(set(listed) - set(_declared))
    if unlisted or undeclared or duplicates:
        raise RuntimeError(
            "tool catalogue out of step: "
            f"declared but not in the catalogue {unlisted}, "
            f"in the catalogue but never declared {undeclared}, "
            f"listed more than once {duplicates}"
        )
    for name in listed:
        declared = _declared[name]
        mcp.add_tool(
            off_the_loop(declared.fn),
            description=declared.description,
            title=declared.title,
            annotations=declared.annotations,
        )
