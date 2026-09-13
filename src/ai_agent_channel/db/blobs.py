"""Body digests and content blobs: documents uploaded in pieces, sealed, and
referenced by hash.

The digest rule is part of the contract: sha256 over the body's raw UTF-8 bytes
exactly as stored — no whitespace trimming, newline conversion or Unicode
normalisation. Length is published as bytes and as characters, named apart.
The server publishes these numbers; it never enforces anything with them."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from .schema import NOW_SQL

# Total size of one upload, in UTF-8 bytes.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def body_digest(body: str) -> dict[str, Any]:
    raw = body.encode("utf-8")
    return {
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "body_length_bytes": len(raw),
        "body_length_chars": len(body),
    }


def with_body_digest(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Publish the same three numbers for MESSAGE bodies that pins carry, so
    voters can check a proposal's text against a server-computed hash.

    Over the BODY only, never body+addendum: a broadcast's personal tails
    differ per recipient, and a digest that changed by reader would describe
    nothing.
    """
    for row in rows:
        if isinstance(row.get("body"), str):
            row.update(body_digest(row["body"]))
    return rows


def _check_upload_size(total_bytes: int) -> None:
    if total_bytes > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"an upload may hold at most {MAX_UPLOAD_BYTES} bytes of UTF-8; "
            f"this append would make it {total_bytes}. Nothing was appended"
        )


def blob_append(
    conn: sqlite3.Connection,
    *,
    role: str,
    text: str,
    upload_id: int | None,
    label: str | None,
) -> dict[str, Any]:
    """Start a blob or append to one. Only the uploader may append, and the
    whole upload may not exceed MAX_UPLOAD_BYTES of UTF-8."""
    adding = len(text.encode("utf-8"))
    if upload_id is None:
        _check_upload_size(adding)
        row = conn.execute(
            "INSERT INTO content_blobs (role, label, body) VALUES (?, ?, ?) "
            "RETURNING id, created_at",
            (role, label, text),
        ).fetchone()
        return {"upload_id": row["id"], "created_at": row["created_at"]}
    blob = conn.execute(
        "SELECT id, role, sealed FROM content_blobs WHERE id = ?", (upload_id,)
    ).fetchone()
    if blob is None:
        raise ValueError(f"upload {upload_id} not found")
    if blob["role"] != role:
        raise PermissionError(f"upload {upload_id} belongs to '{blob['role']}', not '{role}'")
    if blob["sealed"]:
        raise ValueError(
            f"upload {upload_id} is sealed — its hash is published and its "
            f"bytes cannot change; start a new upload for a new revision"
        )
    size = conn.execute(
        "SELECT length(CAST(body AS BLOB)) FROM content_blobs WHERE id = ?",
        (upload_id,),
    ).fetchone()[0]
    _check_upload_size(size + adding)
    conn.execute("UPDATE content_blobs SET body = body || ? WHERE id = ?", (text, upload_id))
    return {"upload_id": upload_id}


def blob_seal(conn: sqlite3.Connection, *, upload_id: int, role: str) -> dict[str, Any]:
    blob = conn.execute(
        "SELECT id, role, body, sealed FROM content_blobs WHERE id = ?", (upload_id,)
    ).fetchone()
    if blob is None:
        raise ValueError(f"upload {upload_id} not found")
    if blob["role"] != role:
        raise PermissionError(f"upload {upload_id} belongs to '{blob['role']}', not '{role}'")
    digest = body_digest(blob["body"])
    if not blob["sealed"]:
        conn.execute(
            "UPDATE content_blobs SET sealed = 1, sha256 = ?, length_bytes = ?, "
            f"length_chars = ?, sealed_at = {NOW_SQL} "
            "WHERE id = ?",
            (
                digest["body_sha256"],
                digest["body_length_bytes"],
                digest["body_length_chars"],
                upload_id,
            ),
        )
    return blob_get(conn, upload_id=upload_id)


def blob_get(
    conn: sqlite3.Connection,
    *,
    upload_id: int,
    role: str | None = None,
    with_body: bool = False,
) -> dict[str, Any]:
    """A blob's metadata (and body on request). Readable by any role; `role`
    is accepted for older callers and ignored."""
    del role
    blob = conn.execute("SELECT * FROM content_blobs WHERE id = ?", (upload_id,)).fetchone()
    if blob is None:
        raise ValueError(f"upload {upload_id} not found")
    out = {
        "upload_id": blob["id"],
        "uploaded_by": blob["role"],
        "label": blob["label"],
        "sealed": bool(blob["sealed"]),
        "created_at": blob["created_at"],
        "sealed_at": blob["sealed_at"],
        **body_digest(blob["body"]),
    }
    # Named for the region, not for a message body: this is the number that
    # describes the DOCUMENT.
    out["sha256"] = out.pop("body_sha256")
    out["length_bytes"] = out.pop("body_length_bytes")
    out["length_chars"] = out.pop("body_length_chars")
    if with_body:
        out["body"] = blob["body"]
    return out


def blob_body(conn: sqlite3.Connection, *, upload_id: int, role: str | None = None) -> str:
    """The text of a sealed blob, for use as a message/pin body. `role` is
    accepted for older callers and ignored."""
    del role
    blob = conn.execute(
        "SELECT id, role, body, sealed FROM content_blobs WHERE id = ?", (upload_id,)
    ).fetchone()
    if blob is None:
        raise ValueError(f"upload {upload_id} not found")
    if not blob["sealed"]:
        raise ValueError(
            f"upload {upload_id} is not sealed — seal it first so its hash is "
            f"fixed before anything refers to it"
        )
    return blob["body"]
