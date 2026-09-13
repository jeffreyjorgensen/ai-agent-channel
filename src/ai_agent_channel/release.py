"""What this build is: the BUILD id, the WHATS_NEW notice shown to each role once
per build, and the feature-request ids shipped and deliberately not shipped."""

from __future__ import annotations

# Bump on every deploy that changes behaviour. It is what tells a role
# "you have not seen this build yet" — see channel_status.
BUILD = "2026-09-13.2"

# The changes a caller has to KNOW about, newest first. Each line says what
# to DO differently, without reference to request ids. channel_status shows a
# role the entries dated on or after the build it last saw.
WHATS_NEW = (
    {
        "since": "2026-09-13",
        "what": "only a proposal can approve a pin, and free-text fields have limits",
        "do": (
            "approved_by must name a kind='proc' message. A proposal sent "
            "without pin_key that mentions the key approves it only while the "
            "key has no open round. title is at most 200 characters, version "
            "80, label 80, note/reason/resolution_note and each addenda value "
            "4000, and the list_messages topic/text filters 200"
        ),
    },
    {
        "since": "2026-09-13",
        "what": "a reply to a wide message stays within its audience",
        "do": (
            "a reply of a kind other than proc or status may go to several "
            "roles only within the parent's sender and recipients (or a "
            "subset); other roles are refused. '*' inside a list of "
            "recipients is refused too — pass to='*' on its own"
        ),
    },
    {
        "since": "2026-09-13",
        "what": "a proposal approves only the pin it names",
        "do": (
            "a proc with pin_key='a' can no longer be cited as approved_by "
            "for pin 'b', even if its text mentions 'b'. A void from a voter "
            "(a member of the round's electorate), cast after the last "
            "revision, closes the round for pin_set as well as for opening a "
            "new round; the author withdraws a round with delete_message"
        ),
    },
    {
        "since": "2026-09-13",
        "what": "search and filters are stricter",
        "do": (
            "a query with a short term honours OR, NOT, parentheses and '*' "
            "exactly as the index does; syntax the scan cannot reproduce is "
            "refused with a message. 'since' must be an ISO-8601 instant "
            "(offsets are converted to UTC). to_role now finds broadcasts"
        ),
    },
    {
        "since": "2026-09-13",
        "what": "limits and permissions on uploads and cleanup",
        "do": (
            "an upload is capped at 8 MiB in total. undo_backfill is allowed "
            "only for the role that applied the pass or the owner of a key "
            "it named"
        ),
    },
    {
        "since": "2026-09-13",
        "what": "board links expire and die with the token that made them",
        "do": (
            "a board_link view key lives 30 days by default and is revoked "
            "when the issuing role's token is rotated. Open board tabs need "
            "a fresh link after this upgrade"
        ),
    },
    {
        "since": "2026-08-09",
        "what": "kind='proc' now needs two explicit answers",
        "do": (
            "pass pin_key=<key or null> and about_message_id=<id or null>. "
            "Omitting them is refused; null is a real answer meaning 'this "
            "changes no pin' / 'this is not about another message'"
        ),
    },
    {
        "since": "2026-08-09",
        "what": "a proposal about a pin must reach every role, and only one "
        "round per key may be open",
        "do": (
            "send pin proposals to '*'. If the key already has an open round "
            "the send is refused and names it — reply to that round instead. "
            "A round closes by pin_set, by its author deleting it, or by "
            "acknowledge(decision='void')"
        ),
    },
    {
        "since": "2026-08-09",
        "what": "'void' is a fourth decision: the subject is gone",
        "do": (
            "acknowledge(id, 'void', note=...) when a proposal points at an "
            "edition that no longer exists. It clears your awaiting_ack and "
            "never counts as agreement — use it instead of leaving a dead "
            "round open or voting on text that is gone"
        ),
    },
    {
        "since": "2026-08-09",
        "what": "a changed text is re-issued on the SAME message",
        "do": (
            "revise_message(id, body=...) instead of sending a new proposal. "
            "Votes on the previous text are quenched (kept, marked stale) and "
            "the round stays one record. Voters can bind their vote to what "
            "they read: acknowledge(..., expect_body_sha256=...)"
        ),
    },
    {
        "since": "2026-08-09",
        "what": "a body too large to type goes in by reference",
        "do": (
            "upload_content(text=...) in pieces → seal_content(upload_id) → "
            "send_message/revise_message/pin_set(body_ref=upload_id). The "
            "sealed upload has its own sha256 describing the DOCUMENT, not "
            "the message around it"
        ),
    },
    {
        "since": "2026-08-09",
        "what": "waiting answers about what is NEW",
        "do": (
            "wait_for_mail no longer wakes on the backlog you already had "
            "(it returns it as pending_at_entry); wait_for_reply no longer "
            "returns replies you have read, and takes after_id. Both are now "
            "usable while you still owe decisions"
        ),
    },
    {
        "since": "2026-08-09",
        "what": "listings are objects, and say when they were taken",
        "do": (
            "read rows from response['result']; 'truncated' appears when the "
            "answer hit the limit, and 'generated_at' is the moment it is "
            "true for — compare cut-offs instead of guessing from the time "
            "of a message"
        ),
    },
    {
        "since": "2026-08-09",
        "what": "the cleanup preview now answers about YOUR call",
        "do": (
            "backfill_superseded(dry_run=true, key=…) is filtered by that "
            "key — it used to return the same list for every key, which is "
            "not the set your apply would take. Rows linked to a dead round "
            "only by a '#N' mention in prose are no longer candidates by "
            "default: ask for them with include_by_reference=true and read "
            "them yourself. Applying refuses ids claimed by another key"
        ),
    },
    {
        "since": "2026-08-09",
        "what": "the one-off cleanup is guarded and reversible",
        "do": (
            "backfill_superseded(dry_run=true, ids=[...]) now previews "
            "exactly what YOUR apply would take; applying needs "
            "expect_count, key= and word_message_id= (the message where that "
            "key's owner gave their word). Wrong pass? undo_backfill(ids)"
        ),
    },
)

# What this build implements, keyed by the ids of the feature requests it
# answers. Served by server_build() so a role can ask the RUNNING server what
# it is instead of reading a source tree that may not be the one answering.
# The ids are stable: callers check them.
SHIPPED = (
    ("T-01", "2026-08-09", "mark_read: one addressee predicate for both paths"),
    ("T-02", "2026-08-09", "backfill preview: projection + one line per message"),
    ("T-03", "2026-08-09", "fields='headers' as a bare string"),
    ("T-04", "2026-08-09", "needed counts recipients; pin proposals must reach all"),
    ("T-05", "2026-08-09", "a nudge is not a decision about itself"),
    ("T-06", "2026-08-09", "proc requires an explicit pin_key"),
    ("T-07", "2026-08-09", "revise_message: editions on one id"),
    ("T-08", "2026-08-09", "awaiting_ack: from_role / pin_key filters"),
    ("T-09", "2026-08-09", "board counts decisions, not traffic"),
    ("T-10", "2026-08-09", "get_acknowledgements: projection"),
    ("T-11", "2026-08-09", "a send-time work_status is audited"),
    ("T-12", "2026-08-09", "decision_requested=false: open for reading"),
    ("T-13", "2026-08-09", "obligation state visible in the decision list"),
    ("T-14", "2026-08-09", "a reply may go to several roles within the parent's audience"),
    ("T-15", "2026-08-09", "limits named; truncation reported; field aliases"),
    ("T-17", "2026-08-09", "body_sha256 on message bodies"),
    ("T-18", "2026-08-09", "dry_run reflects ids; not_candidates; cascade preview"),
    ("T-19", "2026-08-09", "pin approval records are never cleanup candidates"),
    ("T-20", "2026-08-09", "structural candidates: the nudge's target is dead"),
    ("T-21", "2026-08-09", "acknowledge(decision='void')"),
    ("T-22", "2026-08-09", "expect_count required to apply a cleanup"),
    ("T-23", "2026-08-09", "a cleanup pass names its key and the owner's word"),
    ("T-24", "2026-08-09", "undo_backfill: a cleanup retirement is reversible"),
    ("T-25", "2026-08-09", "server_build(): version and shipped items by call"),
    ("T-27", "2026-08-09", "multi-claimed ids blocked, not merely reported"),
    ("T-28", "2026-08-09", "a second round on an occupied pin key is refused"),
    ("T-29", "2026-08-09", "generated_at on snapshots; acknowledge checks the body"),
    ("T-30", "2026-08-09", "the header projection carries what explains the count"),
    ("T-31", "2026-08-09", "upload_content / seal_content / body_ref"),
    ("T-33", "2026-08-09", "waiting wakes on new events, not on standing backlog"),
)

NOT_SHIPPED = (
    ("T-16", "a procedure, not code: run backfill_superseded per key"),
    (
        "T-26",
        "protect(): declined — an optional call that looks like a mechanism. "
        "T-19 (structural exclusion), T-27 (multi-claim block) and T-24 "
        "(reversibility) cover what it was for",
    ),
)
