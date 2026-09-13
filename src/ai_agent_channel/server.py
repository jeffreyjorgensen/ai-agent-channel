"""The MCP server: `mcp` with every tool registered, and serve() for stdio.

The tools live in `tools/`; names are re-exported here so `server.<tool>` keeps
working. Tunables (tools.waiting.WAIT_CAP_S, _poll_until) are not
re-exported: patch them on the module that reads them.
"""

from __future__ import annotations

from . import db
from .app import mcp
from .release import BUILD, WHATS_NEW
from .tools.admin import (
    add_role,
    board_link,
    create_channel,
    delete_channel,
    list_channels,
    revoke_board_access,
    rotate_token,
)
from .tools.cleanup import backfill_superseded, undo_backfill
from .tools.content import get_content, seal_content, upload_content
from .tools.messaging import (
    OMITTED_VOTERS,
    PROVENANCE_DOC,
    delete_message,
    get_thread,
    list_messages,
    mark_read,
    message_history,
    read_inbox,
    revise_message,
    search_messages,
    send_message,
)
from .tools.meta import (
    channel_status,
    get_charter_template,
    get_protocol,
    list_roles,
    server_build,
)
from .tools.obligations import (
    awaiting_ack,
    confirm_resolution,
    open_obligations,
    ready_work,
    reopen_message,
    resolve_message,
    set_work_status,
)
from .tools.pins import (
    acknowledge,
    get_acknowledgements,
    pin_get,
    pin_history,
    pin_list,
    pin_set,
)
from .tools.waiting import wait_for_mail, wait_for_reply

__all__ = [
    "BUILD",
    "OMITTED_VOTERS",
    "PROVENANCE_DOC",
    "WHATS_NEW",
    "acknowledge",
    "add_role",
    "awaiting_ack",
    "backfill_superseded",
    "board_link",
    "channel_status",
    "confirm_resolution",
    "create_channel",
    "delete_channel",
    "delete_message",
    "get_acknowledgements",
    "get_charter_template",
    "get_content",
    "get_protocol",
    "get_thread",
    "list_channels",
    "list_messages",
    "list_roles",
    "mark_read",
    "mcp",
    "message_history",
    "open_obligations",
    "pin_get",
    "pin_history",
    "pin_list",
    "pin_set",
    "read_inbox",
    "ready_work",
    "reopen_message",
    "resolve_message",
    "revise_message",
    "revoke_board_access",
    "rotate_token",
    "seal_content",
    "search_messages",
    "send_message",
    "serve",
    "server_build",
    "set_work_status",
    "undo_backfill",
    "upload_content",
    "wait_for_mail",
    "wait_for_reply",
]


def serve() -> None:
    db.init_db()
    mcp.run()
