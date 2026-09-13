"""sha256 + length of a pin body — the number the consent machinery was missing.

The channel refuses to update a protected pin without every role's consent,
but the roles vote on text quoted in a proposal while what gets stored is
whatever the author retypes. These fields are what makes those two
comparable; the server publishes them and enforces nothing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from ai_agent_channel import db, server
from helpers import L, present

BODY = "Team charter.\nRule 1: we agree before we build.\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_digest_is_published_everywhere_a_pin_appears(db_path: Path, as_role):
    as_role("frontend")
    written = server.pin_set(key="notes", title="Notes", body=BODY, version="v1")
    assert written["body_sha256"] == _sha(BODY)

    got = present(server.pin_get(key="notes"))
    listed = next(p for p in L(server.pin_list()) if p["key"] == "notes")
    versioned = L(server.pin_history(key="notes"))[0]
    for view in (got, listed, versioned):
        assert view["body_sha256"] == _sha(BODY)
        assert view["body_length_bytes"] == len(BODY.encode("utf-8"))
        assert view["body_length_chars"] == len(BODY)


def test_bytes_and_chars_differ_on_multibyte_text(db_path: Path, as_role):
    """Why the unit is in the field name: outside ASCII the two numbers come
    apart — for this body by more than a factor of two — and a bare 'length'
    would be read both ways. The body is symbols rather than prose so that the
    ratio does not depend on which language the channel happens to speak."""
    as_role("frontend")
    body = "\u2705 \u2705 \u2705 \U0001f9fe \U0001f9fe \U0001f9fe\n"
    pin = server.pin_set(key="notes", title="N", body=body, version="v1")
    assert pin["body_length_bytes"] > pin["body_length_chars"] * 1.5


def test_hash_is_raw_bytes_with_no_normalisation(db_path: Path, as_role):
    """A trailing newline MUST change the hash. If the server quietly
    normalised, a team's independent check would report 'identical' for texts
    that are not — the exact failure the number exists to catch."""
    as_role("frontend")
    a = server.pin_set(key="doc", title="D", body=BODY, version="v1")
    b = server.pin_set(key="doc", title="D", body=BODY + "\n", version="v2")
    assert a["body_sha256"] != b["body_sha256"]
    assert b["body_length_bytes"] == a["body_length_bytes"] + 1

    same = server.pin_set(key="doc2", title="D", body=BODY, version="v1")
    assert same["body_sha256"] == a["body_sha256"]


def test_a_copy_that_differs_by_one_space_is_visible_without_reading_bodies(db_path: Path, as_role):
    as_role("frontend")
    server.pin_set(key="charter", title="C", body=BODY, version="v1")
    server_number = L(server.pin_list())[0]["body_sha256"]
    my_copy = BODY + " "
    assert _sha(my_copy) != server_number  # caught by arithmetic, not by eye


def test_digests_are_backfilled_for_pins_written_before_the_column(db_path: Path, as_role):
    as_role("frontend")
    server.pin_set(key="notes", title="N", body=BODY, version="v1")
    # simulate the pre-digest state (such a database predates the schema
    # marker too, which is what makes the next open upgrade it)
    with db.open_db() as conn:
        conn.execute(
            "UPDATE pinned_entries SET body_sha256 = NULL, "
            "body_length_bytes = NULL, body_length_chars = NULL"
        )
        conn.execute("PRAGMA user_version = 0")
    assert present(server.pin_get(key="notes"))["body_sha256"] == _sha(BODY)


def test_old_database_without_the_columns_migrates(db_path: Path, as_role):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE pinned_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            version TEXT NOT NULL, updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """
    )
    conn.execute(
        "INSERT INTO pinned_entries (key, title, body, version, updated_by) "
        "VALUES ('legacy', 'L', ?, 'v1', 'frontend')",
        (BODY,),
    )
    conn.commit()
    conn.close()

    as_role("frontend")
    assert present(server.pin_get(key="legacy"))["body_sha256"] == _sha(BODY)
