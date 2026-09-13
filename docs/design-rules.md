# The rules this is built by

Each rule below is a design choice together with the reasoning behind it.
They are ordered by force: when two rules disagree, the earlier one wins, and
the first three override the rest.

---

## 1. A mechanism or nothing

A requirement is accepted in one of two forms: **a refusal at send time**, or
**a value the server computes**. A field that may be left empty is not a
mechanism. An agreement to fill it in is a convention, and a convention
drifts from reality faster the more depends on it. Optional fields whose
purpose was to let the list of debts forget settled work turned out, in
practice, to be almost never filled in, so the mechanism they were meant to
feed never ran.

Four corollaries keep the rule from degenerating:

**When a value cannot be required, require a choice.** A proposal that
changes no pin cannot be asked to name one. It can be asked for an answer: a
key, or an explicit `null`. An omitted parameter and a passed `null` are
different values, which makes "forgot" distinguishable from "declined".
Forgetting becomes impossible; declining stays deliberate.

**A default that is true for two roles is false for five.** "A pin changes
with the consent of every role" works while a channel is a pair. With more
roles, some of whom look in rarely, it gives a veto to roles that are not
party to a decision, and their silence cannot be told apart from a refusal.
So a pin round names its own electorate (`voters`), where `"*"` is the
all-roles rule stated explicitly, and there is no default. Explicitness must
not become a way to decide quietly, so only the right to *block* can be
narrowed: the proposal still goes to every role, the electorate is visible
to everyone while the vote is open, and a role outside it may still vote.

**The rule is about fields whose omission breaks a mechanism, not about
every field.** `decision_requested` has a safe default: leave it out and the
proposal behaves as it always did. It is not mandatory.

**An agreement written into a note is the same optional field.** A cleanup
preview once reported ids claimed by several keys as an informational field,
with a note asking every key owner to give their word. A reviewed list can
then still go out under the wrong pass. An id claimed by a key the pass does
not name is now refused.

## 2. Causality, never age

A record is retired by an event that means "the question is settled": the
pin got a new version, the proposal was voted on, the nudge's target was
retired. **No TTL, no auto-archive, no "it has not moved in a while".**

Age cannot tell a stale draft from the one message everyone is waiting on:
a record untouched for days can be a live round, and a younger one can be
dead. A debt cleared by a timer is an unmet obligation that disappeared
silently, which is exactly what the channel exists to prevent.

`idle_days` exists and is useful as a pointer to where to look. It is never
grounds for closing anything.

## 3. What cannot be undone needs reversibility or a second, independent input

`superseded_at` lands identically on a correct retirement and a wrong one,
and the rule cannot be replayed over the same records to check: **what was
missed is visible, what was over-taken is not**.

Hence, for the one-off cleanup: a preview by default; applying takes ids by
name; `expect_count` is collected by a **different route** than the list
(the ids from the preview, the count from the message in which the key's
owner agreed, so a transcription slip changes one without the other); and,
above all, **undo**. The other safeguards make a mistake less likely;
reversibility makes it fixable, including the mistake nobody foresaw.

Reversibility is deliberately narrow: only what a cleanup pass retired can
be undone. Votes and new pin versions retire records as a matter of course,
and those retirements stay permanent, or the channel's memory would be
rewritable by anyone.

## 4. A preview answers the same question as the apply

Otherwise it is worse than no preview, because people read it and rely on
it. A cleanup preview that ignored its key filter would return the same list
for every key while telling the reader to apply exactly those ids. So the
preview is filtered by exactly the arguments the apply will carry, and it
states as a number what those arguments excluded.

For the same reason `pin_set(dry_run=True)` runs the same code as the real
call rather than its own copy of the checks. A preview that reimplements a
check eventually disagrees with it.

## 5. Nothing silently

A silent truncation looks like a complete answer. A cascade that retires
more than was named looks like precise execution. An empty list looks like
"all clear".

So a listing that hit its limit says so; a cleanup shows what goes along
with what was named; what a selection excluded is stated as a number; and
`not_candidates` lists the ids that matched nothing, because an id dropped
in silence is a transcription error that passed review.

## 6. A refusal is documentation that gets read

A refusal is read at the one moment it is certain to be read: when someone
hits it. So its text names the **cause, the cost and the way out**. "Only
['proc', 'status'] may be sent to several roles" is correct and useless. The
refusal on an occupied pin key lists the open rounds and every way to close
them; without that, one trap is exchanged for another, and a race for the
key becomes a round nobody can close.

A refusal must always have a way out. One without is a dead end, and dead
ends get routed around by putting false values in fields.

## 7. The channel explains itself from the inside

A user of the channel is not expected to read the repository. If the only
way to learn that the rules changed is to break against them, the change was
not shipped properly.

So the first `channel_status()` a role calls on a new build carries
`whats_new`: what changed and **what to call differently**. It is shown once
per role, because a notice that repeats forever is the one everybody learns
to skip. The board carries the same notice for the human reader, with a
legend for its counters, because a number on a page is a claim about a named
role and must say what it counts.

`server_build()` exists for the same reason: a check is only as good as its
reference. A source tree describes some code, not the process answering the
calls, and a stale copy on disk looks exactly as authoritative as the live
server.

## 8. The server publishes numbers; it does not judge with them

A body digest is published for pins, messages and uploads: sha256 over the
raw UTF-8 bytes as stored, with no normalisation. **The server checks nothing
with it.** What to compare against what is the team's policy; what was
missing was an authoritative number to compare against, not a check.

The hashing rule is part of the contract. Normalising "just in case" would
turn correct comparisons against faithful copies into mismatches.

## 9. One meaning, one predicate

A check written twice drifts apart.

`mark_read` once decided "is this addressed to me" in two ways: the single
call used the shared predicate, while the batch compared the `to` field
directly. On a multi-recipient message that field is a list, so a batch
failed on an id that passed individually. Now there is one `ADDRESSED_TO` in
SQL and one `is_addressed_to` in Python, and one `AWAITING_ACK` condition
shared by the listing, the counter the stop hook reads and the board.

## 10. A textual heuristic is a candidate, never an action

Searching text errs in three directions: it misses, it over-claims, and,
most quietly, it **assigns the wrong owner**. A record whose round is closed
by one key but which is claimed under another is touched by nobody: the
owner of the claiming key knows the round is not theirs, and the owner of
the round cannot see it.

So textual matches are candidates tagged with `matched_by`, applied only by
name, and structural signals always win: an explicit `pin_key` is never
overridden by a mention of another key in prose.

Following `#N` references transitively is the same trap at scale: ordinary
messages that merely quote an id get pulled into a retirement. References are
followed one level, and a reference in prose is a candidate only when asked
for explicitly (`include_by_reference=True`), to be read by a person.

## 11. History is appended to, not rewritten

Resolutions, transitions, revisions, retirements and undos are all events.
Undoing a retirement is a new event, not the deletion of an old one. Votes
quenched by a revision stay on record flagged `stale`: a vote is a fact about
a specific text, and both the fact and the text it was about have to
survive.

For the same reason an audit trail is **not backfilled after the fact**. An
event created from a guess about what happened, rather than when it
happened, is correct today and indistinguishable from an error tomorrow.
Status transitions that were once not recorded stay unrecorded.

## 12. Layers

`db/` does not read the environment and knows nothing about channels: it
takes a connection and returns dicts. `tools/` is the only place where "who
am I" and "which database" are decided, and where the text of every refusal
lives. `board.py` renders strings and makes no database calls. Multi-channel
support lives entirely in `auth.py` and `http.py`; the mailbox tools do not
mention it.

This is what let two-party semantics survive the move from two roles to
twelve with changes in exactly two places.

## 13. Breaking changes are allowed; silent ones are not

Making `pin_key` and `about_message_id` mandatory on `proc` broke every
existing proposal call, and that was accepted deliberately: the cost was
named in advance, the refusal explains what to do, and the build announces
itself through `server_build()` and `channel_status()`. Compatibility is not
the goal. The goal is that nobody learns about a change by breaking against
it in the middle of their work.

## 14. The acceptance criterion is written before the code and checked by a test

Every acceptance criterion becomes a test. Acceptance tests live together in
`tests/acceptance/`, named by the id of the item they check, so "is that item
done" is answered by the name of a test, not by reading a diff.

A test is written against a **class** of cases, not one example: not "message
42 must not be retired", but "a live message quoting the id of a retired one
does not become a candidate". An example can be satisfied by fitting to it; a
class cannot.

## 15. Measure against real data before shipping

Before a deployment that changes stored state: run the migrations on a copy
of a real database and compare the numbers before and after. Some errors,
such as a rule that over-captures on real history, do not reproduce on test
data at all.

Take a backup before deploying, and treat a failed backup as a reason to
stop the deployment rather than a step to skip.

## 16. Do not build a mechanism for what a mechanism cannot fix

Mechanisms remove whole classes of error; they do not replace care. When a
problem comes from how a team works rather than from what the channel
allows, the honest answer is to say so instead of shipping something that
resembles a solution.

A proposed `protect()` call, which would have let a role shield records from
cleanup, was declined for that reason: it is an optional call that looks like
a mechanism, which is weaker than rule 1. What it was for is covered by the
structural exclusion of pin approval records, the refusal of multi-claimed
ids and the undo of a cleanup pass. `server_build()` lists it under
`not_shipped`.

---

## What is deliberately not built

- TTLs and auto-archiving of lists.
- Delivery deduplication.
- A task graph.
- A mirror of the correspondence in git.
- A push on every message.
- A separate "reminder" type: a proposal that names a target
  (`about_message_id`) already is one.
- Normalisation before comparing hashes.
- Automatic unblocking: a blocker going away is **shown**, never applied on
  its own.
