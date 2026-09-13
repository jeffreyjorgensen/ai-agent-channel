"""Only a void from the round's electorate closes it, and the tally names
exactly those voids."""

from __future__ import annotations

from pathlib import Path

from ai_agent_channel import db


def test_declared_dead_by_lists_only_voids_from_the_electorate():
    tally = db.ack_tally({"beta": "void", "gamma": "void"}, author="alpha", voters=["beta"])
    assert tally["declared_dead_by"] == ["beta"]
    assert tally["from_non_recipients"] == {"gamma": "void"}


def test_a_bystander_void_alone_declares_nothing_dead():
    tally = db.ack_tally({"gamma": "void"}, author="alpha", voters=["beta"], declared=True)
    assert "declared_dead_by" not in tally
    assert tally["from_non_voters"] == {"gamma": "void"}


def test_the_tally_and_the_round_agree_on_who_closed_it(tmp_path: Path):
    with db.open_db(tmp_path / "m.db") as conn:
        message_id = db.insert_message(
            conn,
            from_role="alpha",
            to_role="beta",
            topic="proposal",
            body="text",
            action_required=False,
            reply_to=None,
            kind="proc",
            pin_key="notes",
        )["id"]

        def state() -> tuple[list[str], list[str], list[int]]:
            acks = db.acks_by_message(conn, message_ids=[message_id]).get(message_id, {})
            tally = db.ack_tally(acks, author="alpha", voters=["beta"])
            return (
                db.round_voided_by(conn, message_id=message_id),
                tally.get("declared_dead_by", []),
                [r["id"] for r in db.open_rounds_for_pin(conn, key="notes")],
            )

        # a bystander, and the author (who withdraws by deleting, not by acking)
        db.upsert_acknowledgement(
            conn, message_id=message_id, role="gamma", decision="void", note=None
        )
        db.upsert_acknowledgement(
            conn, message_id=message_id, role="alpha", decision="void", note=None
        )
        assert state() == ([], [], [message_id])

        db.upsert_acknowledgement(
            conn, message_id=message_id, role="beta", decision="void", note=None
        )
        assert state() == (["beta"], ["beta"], [])
