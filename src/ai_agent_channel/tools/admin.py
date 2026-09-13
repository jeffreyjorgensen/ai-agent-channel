"""Channel management over the HTTP transport. Every tool answers only to the
admin token, except board_link, which a role may also call for its own channel."""

from __future__ import annotations

from typing import Any

from .. import auth
from .common import LABEL_MAX, check_length, current_identity, require_admin
from .registry import tool


@tool(
    description=(
        "ADMIN ONLY (HTTP transport): create a channel — an isolated mailbox "
        "shared by 2..12 named roles. Returns one bearer token per role; "
        "this is the ONLY time the tokens are shown (the server stores "
        "hashes), so deliver them to the agents now. Messages inside go to "
        "one role, a list of roles, or '*'; protected pins (team-charter, "
        "...) need the consent of the voters each proposal declares. Names "
        "and roles are lowercase slugs "
        "(letters/digits/dash/underscore, max 64 chars)."
    )
)
def create_channel(name: str, roles: list[str]) -> dict[str, Any]:
    require_admin()
    with auth.open_admin_db() as conn:
        return auth.create_channel(conn, name=name, roles=roles)


@tool(
    description=(
        "ADMIN ONLY (HTTP transport): list active channels with their "
        "roles. Tokens are never listed — rotate_token issues a fresh one if "
        "a token is lost."
    ),
    read_only=True,
)
def list_channels() -> list[dict[str, Any]]:
    require_admin()
    with auth.open_admin_db() as conn:
        return auth.list_channels(conn)


@tool(
    description=(
        "ADMIN ONLY (HTTP transport): revoke all tokens of one (channel, "
        "role) pair and issue a fresh token. Use when a token leaked or was "
        "lost. The old token stops working immediately."
    )
)
def rotate_token(channel: str, role: str) -> dict[str, Any]:
    require_admin()
    with auth.open_admin_db() as conn:
        return auth.rotate_token(conn, channel=channel, role=role)


@tool(
    description=(
        "ADMIN ONLY (HTTP transport): add a new role to an existing channel "
        "and return its bearer token (shown exactly once). The channel must "
        "have room (<= 12 roles) and the role must be new. Existing roles, "
        "tokens and message history are untouched; the new role can read the "
        "whole channel. Its consent becomes required for future "
        "protected-pin rounds that do not declare their voters or declare "
        "voters='*'; a round that lists its voters by name is unaffected. "
        "Role is a lowercase slug."
    )
)
def add_role(channel: str, role: str) -> dict[str, Any]:
    require_admin()
    with auth.open_admin_db() as conn:
        return auth.add_role(conn, channel=channel, role=role)


@tool(
    description=(
        "Issue a READ-ONLY viewing link for a channel's /board — the page a "
        "HUMAN opens to watch the channel. Returns {url, expires_in_s, "
        "key_expires_at}: the value in the URL is single-use and short-lived "
        "(expires_in_s), so what stays in browser history opens nothing; the "
        "viewing key behind it lasts until key_expires_at, until revoked, or "
        "until the issuing role's token is rotated. "
        "Callable by the admin token for any channel, and by any ROLE for "
        "its own channel — a viewing key is strictly less than the full read "
        "and write a role already has. It carries no role: every mailbox "
        "tool refuses it and the server accepts it only for a board GET; a "
        "role token is not accepted by the board at all. Call again for "
        "another link. To invalidate every viewing key of a channel at once "
        f"(a lost phone), ask the admin to revoke board access. 'label' is at "
        f"most {LABEL_MAX} characters."
    )
)
def board_link(channel: str = "", label: str = "board") -> dict[str, Any]:
    ident = current_identity()
    if ident is None:
        raise PermissionError(
            "board links exist only over the HTTP transport (stdio mode has no board to open)"
        )
    check_length(label, "label", LABEL_MAX)
    if not ident.is_admin:
        # A role may issue a viewing key for its own channel and no other: it
        # already has full read and write there, so a read-only key is less.
        if ident.role is None or ident.channel is None:
            raise PermissionError(
                "this token has no channel — board links are issued by the "
                "admin token, or by a role for its own channel"
            )
        channel = (channel or "").strip() or ident.channel
        if channel != ident.channel:
            raise PermissionError(
                f"'{ident.role}' can only issue a board link for its own "
                f"channel ('{ident.channel}'), not '{channel}'"
            )
    if not channel:
        raise ValueError("'channel' is required when using the admin token")
    with auth.open_admin_db() as conn, auth.write_tx(conn):
        # a role's keys are recorded as its own: they are capped per role
        # and revoked when that role's token is rotated. The key and its link
        # are one write, so a failure cannot leave a key nobody can open.
        issued = auth.create_view_token(
            conn,
            channel=channel,
            label=label,
            issued_by_role=None if ident.is_admin else ident.role,
            issued_by_token_hash=None if ident.is_admin else ident.token_hash,
        )
        nonce = auth.mint_board_nonce(conn, view_token=issued["token"])
    return {
        "channel": issued["channel"],
        "label": issued["label"],
        "url": f"/board/{issued['channel']}?t={nonce}",
        "expires_in_s": auth.BOARD_NONCE_TTL_S,
        "key_expires_at": issued["expires_at"],
        "note": (
            "prefix the URL with your server's origin. The link is single-use "
            "and short-lived; the viewing key behind it lasts until "
            "key_expires_at, until revoked, or until the issuing role's token "
            "is rotated."
        ),
    }


@tool(
    description=(
        "ADMIN ONLY (HTTP transport): revoke EVERY read-only viewing key of "
        "a channel — the answer to a lost or shared phone. Open board "
        "cookies stop working immediately. Role tokens and the mailbox are "
        "untouched; issue a fresh link with board_link."
    )
)
def revoke_board_access(channel: str) -> dict[str, Any]:
    require_admin()
    with auth.open_admin_db() as conn:
        revoked = auth.revoke_view_tokens(conn, channel=channel)
        remaining = auth.list_view_tokens(conn, channel=channel)
    return {"channel": channel, "revoked": revoked, "keys": remaining}


@tool(
    description=(
        "ADMIN ONLY (HTTP transport): deactivate a channel — revokes its "
        "tokens and hides it from list_channels. The mailbox DB file stays "
        "on the server's disk for audit; remove it manually if the data must "
        "go. The name cannot be reused."
    )
)
def delete_channel(name: str) -> dict[str, Any]:
    require_admin()
    with auth.open_admin_db() as conn:
        return auth.delete_channel(conn, name=name)
