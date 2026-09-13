"""Many threads opening a channel database that does not exist yet.

Tool calls run in worker threads, so the first calls on a new channel open its
file concurrently. Switching a fresh file to WAL must not fail with
"database is locked"."""

from __future__ import annotations

import threading
from pathlib import Path

from ai_agent_channel import db


def test_concurrent_first_opens_of_a_new_database_all_succeed(tmp_path: Path) -> None:
    for attempt in range(5):
        path = tmp_path / f"fresh-{attempt}.db"
        start = threading.Barrier(16)
        errors: list[BaseException] = []

        def open_and_read(
            path: Path = path,
            start: threading.Barrier = start,
            errors: list[BaseException] = errors,
        ) -> None:
            try:
                start.wait()
                with db.open_db(path) as conn:
                    conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            except BaseException as exc:  # collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=open_and_read) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        with db.open_db(path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
