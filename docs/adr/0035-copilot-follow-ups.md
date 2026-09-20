# ADR-0035: Follow-up questions — a thread the client carries, re-authorized every turn

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0022](0022-copilot-typed-tools-evidence-and-grounding.md) (typed, authorized tools), [ADR-0023](0023-self-hosted-model-behind-an-earned-switch.md) (the model plans, AMP authorizes)

---

## Context

The Copilot answered one question at a time. Its request was `{question}` and
nothing else; the screen kept a thread of turns, the server kept nothing. So
"How is CNC-01 doing?" followed by "and its downtime this week?" answered the
second question about the whole plant: with no machine named, the router
could not know what *its* meant.

A conversation is how people ask about a factory. The founder's brief for the
sprint asked for multi-turn context with one condition: **conversation memory
must never bypass authorization.** Every design that stores a conversation
server-side, or lets a previous answer feed the next, has to prove that the
thing carried across turns cannot be a permission, a tenant, or a fact the
caller is not allowed to see now.

## Decision

### 1. The client carries its own thread; the server stores nothing

`POST /copilot/ask` and `POST /ai/ask` accept an optional `thread`: the
caller's prior turns, each `{"question", "calls"}`, where `calls` is what the
response already returns as `plan.calls` — the tool names and arguments AMP
itself ran for that question. At most the last six turns are considered
(`MAX_THREAD_TURNS`), each with at most four calls, each question cut to
`MAX_QUESTION`. Anything else a turn carries — an answer, evidence, a result,
a role, a tenant, a claim — is dropped by `clean_thread` before anything
downstream can see it.

Nothing is stored. There is no conversation to leak between users, no session
to hijack, and a forged thread is just text.

### 2. A thread can name things, never authorize them

The thread is used for exactly one purpose: to work out what a follow-up
*refers to*. Every tool the follow-up then runs goes through `run_tool` for
**this** principal — role, licence, tenant, arguments — exactly as a first
question does. A prior turn's results are never reused, because they are
never accepted.

The referent is resolved by name through the **scoped** machine list, with
the tenant bound for the whole question (`orchestrator.ask`). A thread that
names another company's machine — whether the caller typed it or forged a
`get_machine_history(machine=WELD-07)` call — resolves to nothing, and the
follow-up routes on its own words. Measured in the evaluation's adversarial
threads across three factories and three roles: zero disclosures.

### 3. A follow-up refers to a machine only when it says so

`"and its downtime?"`, `"is it running now?"`, `"that machine's inspections"`
carry a pronoun or a demonstrative from a short fixed list
(`_REFERENT_WORDS`). `"and what about the plant OEE?"` carries neither, and
routes on its own words. A conversation about CNC-01 must not turn every later
question into a question about CNC-01; the rule is narrow on purpose, and the
evaluation's `follow_plant` case pins the narrowness.

### 4. What a planning model is told

When a tool-calling model plans, the prior turns are sent as earlier `user`
messages, each followed by one `assistant` line of the form
`AMP ran: get_machine_history(machine=CNC-01)`. The model sees what was asked
and which tools AMP chose — never an answer, never evidence — and it still
only *names* tools; AMP authorizes and runs them. The context-window check
(ADR-0034 §7) counts the thread.

### 5. The answer says what it resolved

The response carries `thread: {turns, resolved}`. `resolved` is
`{"machine": "CNC-01"}` when a referent was filled by AMP's planner and
`null` otherwise, so the screen can say "about CNC-01" rather than leave the
reader guessing which machine a pronoun landed on.

## Consequences

**Positive.** Follow-ups about a machine work, through the same tools,
evidence and grounding as any first question. No new storage, no new
authorization path, nothing new for a model to be trusted with.

**Negative.** Only a *machine* can be referred to: the machine detail is the
one tool with a referent argument. "And for the night shift?" after a
downtime question does not carry the shift, because no tool takes one. Six
turns is a cap, not a memory. The rule planner recognises a fixed list of
words; a follow-up phrased outside it routes as a fresh question, which is
the safe failure.

## Honest limits

- The model path may still misread a pronoun; the evaluation's thread cases
  (`copilot_eval.cases.THREADS`) score it the same way as every other question,
  and the adoption record's question-set digest changes with them, so a model's
  promotion has to be re-earned on the new set.
- A forged thread cannot obtain data, but it can steer a follow-up to a machine
  of the caller's own tenant. That is the caller steering their own question.

## Where the code is

| Concern | File | Tests |
|---|---|---|
| Thread cleaning, referent, follow-up routing | `ai/orchestrator.py` (`clean_thread`, `_referent`, `plan_rules`) | `test_copilot_follow_ups.py`, `mutate_copilot_follow_ups.py` |
| What a model is told | `ai/llm.py` (`ProviderLLM.plan`) | `test_copilot_follow_ups.py` §6–7 |
| Routes | `read_model_routes.copilot_ask`, `ai_copilot.ai_ask` | `test_copilot_follow_ups.py` §8 |
| Evaluation | `copilot_eval/cases.py` (`THREADS`, `ADVERSARIAL_THREADS`), `copilot_eval/harness.py` | `test_copilot_eval.py` |
| Screen | `frontend/components/AICopilot.tsx` | `AICopilot.test.tsx` |
