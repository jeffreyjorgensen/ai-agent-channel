"""The MCP tools. Importing this package registers all of them on `app.mcp`, in
CATALOGUE order, which is the order clients list them in."""

from __future__ import annotations

from ..app import mcp
from . import admin, cleanup, content, messaging, meta, obligations, pins, waiting
from .registry import register

CATALOGUE = (
    messaging.send_message,
    messaging.read_inbox,
    messaging.mark_read,
    messaging.delete_message,
    messaging.list_messages,
    messaging.search_messages,
    obligations.resolve_message,
    obligations.confirm_resolution,
    obligations.reopen_message,
    obligations.open_obligations,
    obligations.set_work_status,
    obligations.ready_work,
    obligations.awaiting_ack,
    messaging.get_thread,
    content.upload_content,
    content.seal_content,
    content.get_content,
    messaging.revise_message,
    messaging.message_history,
    pins.pin_set,
    pins.pin_get,
    pins.pin_list,
    pins.pin_history,
    pins.acknowledge,
    pins.get_acknowledgements,
    cleanup.backfill_superseded,
    cleanup.undo_backfill,
    meta.list_roles,
    meta.channel_status,
    waiting.wait_for_reply,
    waiting.wait_for_mail,
    meta.server_build,
    meta.get_protocol,
    meta.get_charter_template,
    admin.create_channel,
    admin.list_channels,
    admin.rotate_token,
    admin.add_role,
    admin.board_link,
    admin.revoke_board_access,
    admin.delete_channel,
)

register(mcp, CATALOGUE)
