from __future__ import annotations

import multiprocessing as mp
import sqlite3
from pathlib import Path

from ai_agent_channel import db
from helpers import concurrent_writer


def test_concurrent_writes_no_loss(tmp_path: Path):
    db_file = tmp_path / "messages.db"
    n = 100
    db.init_db(db_file)

    # spawn, not fork: each writer is a fresh interpreter with its own
    # connection, which is the situation two agent sessions are in. The
    # database path and role travel as arguments; nothing here touches this
    # process's environment.
    ctx = mp.get_context("spawn")
    writers = {
        "frontend": ctx.Process(
            target=concurrent_writer, args=(str(db_file), "frontend", "backend", n)
        ),
        "backend": ctx.Process(
            target=concurrent_writer, args=(str(db_file), "backend", "frontend", n)
        ),
    }
    try:
        for p in writers.values():
            p.start()
        for p in writers.values():
            p.join(timeout=25)
        for role, p in writers.items():
            assert p.exitcode == 0, f"{role} writer failed: {p.exitcode}"
    finally:
        for p in writers.values():
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
            if p.is_alive():
                p.kill()
                p.join()

    conn = sqlite3.connect(str(db_file))
    try:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        from_fe = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE from_role = 'frontend'"
        ).fetchone()[0]
        from_be = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE from_role = 'backend'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert total == 2 * n, f"expected {2 * n} messages, got {total}"
    assert from_fe == n
    assert from_be == n
