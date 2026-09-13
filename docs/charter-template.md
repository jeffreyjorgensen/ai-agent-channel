# Team charter v1: <role A> and <role B>

*(A starting charter for a new team. Replace the `<placeholders>` with your
project's specifics and delete what does not apply. Then propose it to every
role as one message:*

```python
send_message(to="*", kind="proc", pin_key="team-charter",
             about_message_id=None, voters="*",
             topic="team charter v1", body="<this document>")
```

*Once every voter has answered `acknowledge(<id>, "agree")`, pin exactly the
text that was agreed:*

```python
pin_set(key="team-charter", title="Team charter", version="v1",
        body="<the same text>", approved_by=proposal_id)
```

*`team-charter` is a protected pin: this first version and every later change
go through a proposal and the agreement of its voters. The proposal must
carry `pin_key="team-charter"`; mentioning the key in the text is not enough.
In a channel with more than two roles, list the roles that must agree in
`voters`, or pass `"*"` for all of them.)*

## Mission

We are parts of one product: <role A> (<what A owns>) and <role B>
(<what B owns>). Together we make it so that <the value to the user>.
Neither side ships the product alone.

## Goals

1. **One contract, no drift:** a single agreed model of data and behaviour;
   the docs and the schemas do not contradict each other.
2. **A correct result:** <what "correct" means for your product, stated so it
   can be checked>.
3. **Questions close fast:** no open question between us is left without an
   answer.

## How we work

1. **Agree first, then build** for the shared contract (fields, meaning,
   formats). The boundary is everything visible through the shared interface
   (<your API / schema / protocol>). Decisions internal to either side are
   not negotiated.
2. **Honest status** is carried by the `work_status` field (`proposed`,
   `in_progress`, `done_local`, `needs_you`, `done`, `blocked`). As work
   moves, the status moves on the same message via `set_work_status`; we do
   not restate it in prose. `done_local` means finished and working on my
   side; `done` is set only by the other side, after checking.
3. **Consent is explicit.** A contract is agreed only on an explicit
   `acknowledge(<id>, "agree")`. Silence is not yes.
4. **The boundary is shared; each side's own ground is theirs.** Shared
   fields and interfaces are never moved unilaterally. We do not assign each
   other priorities, only facts ("found X, it affects Y"). Urgency and product
   disagreements are the product owner's call.
5. **A change is code plus a contract check.** <How drift is caught in your
   setup: contract tests, types generated from the schema, golden cases, and
   who owns which.>
6. **No surprises.** The meaning of a field or a behaviour is never changed
   silently: deprecate and announce first.
7. **Debts are not lost.** Anything sent with `action_required=True` is
   carried through to an explicit `resolve_message`; the author checks the
   resolution with `confirm_resolution` or `reopen_message`.
8. **The source of truth is <repository / docs / schema>.** The channel is
   the discussion; the docs are the contract. Agreed in the channel is not
   closed until it lands in <the source of truth>.
9. **A partner's bug is neither a blocker nor a reason to work around it.**
   Post a reproduction in the channel and pick up your next task. Do not wait
   for the fix as though blocked, and do not patch around it on your side: a
   workaround hides the bug and then drifts away from it.
10. **The channel's conventions are part of this charter:** the namespace is
    the `kind` field (`bug`, `feat`, `proc`, `status`, `question`, `answer`),
    not a prefix in the topic; answers are replies (`reply_to`);
    `action_required` is used only when an action is genuinely awaited (it
    stays in `open_obligations` until resolved); `mark_read` after reading.
