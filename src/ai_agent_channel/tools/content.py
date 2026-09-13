"""Chunked uploads: a body too large for one call is uploaded, sealed, and then
passed by reference (body_ref)."""

from __future__ import annotations

from typing import Any

from .. import db
from .common import LABEL_MAX, check_length, current_role, open_channel_db
from .registry import tool


@tool(
    description=(
        "Upload a document in pieces, so a body too large to type in one "
        "call can still be sent. Call it repeatedly with the same "
        "'upload_id' to append; call seal_content when the last piece is in. "
        "Then pass body_ref=<upload_id> to send_message, revise_message or "
        "pin_set instead of 'body'. Only the uploader may append; a sealed "
        "upload cannot be appended to; an append that would take the upload "
        f"past {db.MAX_UPLOAD_BYTES // (1024 * 1024)} MiB of UTF-8 is refused. "
        f"'label' is at most {LABEL_MAX} characters. "
        "A sealed upload has its own sha256 "
        "and both lengths, describing the document itself rather than the "
        "message that carries it. Returns the upload's current digest and "
        "upload_id."
    )
)
def upload_content(
    text: str,
    upload_id: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    role = current_role()
    if not text:
        raise ValueError("'text' is required")
    check_length(label, "label", LABEL_MAX)
    with open_channel_db(write=True) as conn:
        result = db.blob_append(conn, role=role, text=text, upload_id=upload_id, label=label)
        state = db.blob_get(conn, upload_id=result["upload_id"])
        return {
            **state,
            "note": (
                "append more with the same upload_id, then seal_content to "
                "fix its hash; the digest above is of what has arrived so far"
            ),
        }


@tool(
    description=(
        "Seal an upload: fix its bytes and publish the sha256 and both "
        "lengths of the DOCUMENT. A sealed upload cannot be appended to — "
        "its number is published, so its bytes must stop moving. Only then "
        "can it be used as body_ref. Returns the digest to compare against "
        "'shasum -a 256' of your own copy: same rule as everywhere here, "
        "sha256 over the raw UTF-8 bytes exactly as stored, no normalisation."
    )
)
def seal_content(upload_id: int) -> dict[str, Any]:
    role = current_role()
    with open_channel_db(write=True) as conn:
        return db.blob_seal(conn, upload_id=upload_id, role=role)


@tool(
    description=(
        "Read back an uploaded document: its digest, lengths and (with "
        "with_body=true) its text. Anyone in the channel may verify an "
        "upload — that is the point of publishing the number."
    ),
    read_only=True,
)
def get_content(upload_id: int, with_body: bool = False) -> dict[str, Any]:
    current_role()  # reading needs a caller identity, as every tool does
    with open_channel_db() as conn:
        return db.blob_get(conn, upload_id=upload_id, with_body=with_body)
