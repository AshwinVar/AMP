# ADR-0034: A reasoning model needs room to name a tool, and AMP gave it none

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0023](0023-self-hosted-model-behind-an-earned-switch.md) (a self-hosted model behind an earned switch), [ADR-0022](0022-copilot-typed-tools-evidence-and-grounding.md) (typed, authorized tools)

---

## Context

ADR-0023 built the provider abstraction, the earned switch and the evaluation,
and tested all three end to end against **scripted stand-ins**. No real model had
ever been run, because downloading one needed the founder's permission. That
permission arrived on 2026-09-20.

The stand-ins answer instantly and emit their tool call as the first token they
produce. A real open-weight model of the class AMP can self-host does not:
Qwen3, DeepSeek-R1, and the reasoning-tuned Llama derivatives all *think* before
they answer, and the thinking is tokens.

AMP's planning call allowed **300 tokens**, a number chosen when the only
models in the room were stubs.

## What was measured

First real model, `qwen3:8b` (Apache 2.0, Q4_K_M, 5.2 GB) on an RTX 5070 Ti
Laptop, 100% GPU-resident, through AMP's own `ProviderLLM.plan()` with the real
26-tool catalogue:

| Planning budget | Questions routed | Completion tokens used | `finish_reason` |
|---|---|---|---|
| 300 (the default) | **0 / 8** | 300, every time | `length` |
| 1200 | **8 / 8** | 321–870 | `tool_calls` |

At 300 tokens the model returned **no tool call and no content**. Ollama strips
thinking from `content`, so what reached AMP was an empty message — identical in
shape to a model that had nothing to say.

The routing, once it had room, was correct on the first attempt:

| Question | Tool chosen |
|---|---|
| Why are we behind target? | `explain_production_gap` |
| How much did downtime cost today? | `get_financial_losses` |
| Are we going to run out of anything? | `get_shortage_risk` |
| Which machines need maintenance? | `get_maintenance_status` |
| Give me my morning briefing. | `get_daily_brief` |

So the model was never bad at routing. **AMP was measuring its own ceiling and
attributing the result to the model.**

## Decision

### 1. The planning budget is configurable, and its default is a measured number

`AMP_LLM_PLAN_TOKENS`, default **1200**: the measured floor for a small
reasoning model (870) plus headroom. `max_tokens` is a ceiling, not an
allocation — a model that does not think still returns its call in ~50 tokens
and is unaffected.

### 2. Truncation is reported as truncation

`chat()` now carries `finish_reason` out, and `plan()` reports a budget
exhaustion as exactly that:

> the model reached the 1200-token planning budget before it named a tool

The orchestrator still falls back to AMP's own router either way. What changes
is that the reason reaching `/ai/status` is the true one. "This model cannot
route" and "AMP gave it no room" are different engineering problems, and for
three hours one was wearing the other's clothes.

### 3. No vendor tokens in AMP

Two other fixes were measured and rejected:

- `chat_template_kwargs: {"enable_thinking": false}` — the OpenAI-compatible
  extension vLLM and SGLang honour. **Ollama 0.34.2 ignores it**: 0/4 routed.
- `/no_think` appended to the system prompt — Qwen's own control token. 1/4
  routed, and it teaches AMP a string that means nothing to the next model.

The budget fix is model-agnostic, which is the property that matters: ADR-0023's
whole point is that the runtime and the model can change.

### 4. The report goes through the same gate as every other answer

`POST /ai/report` used to hand a raw text snapshot of the factory to whatever
model was configured and return the model's prose as the report — the one
Copilot path where a model could state a figure nobody measured. It now asks
the orchestrator for the Daily Brief (ADR-0028): AMP's tools produce the
evidence, the model may word it, the grounding gate decides whether that
wording is shown, and a refused wording is replaced by AMP's own sentence with
a note saying so. Its 503 message, which named only `ANTHROPIC_API_KEY`, now
names the self-hosted option too. AMP is demonstrably able to run with **no
hosted key at all** — pinned in `test_copilot_local_provider.py` §4b.

### 5. One log line per answer, and what it must not carry

The orchestrator logs a structured `copilot` object per answered question:
engine, planner, provider, model, the tools that ran with their states and
timings, the gate's verdict and reasons, the overall data state, elapsed time,
and token counts. It never logs the question, the answer, the evidence or the
tenant — the access log already ties the line to its request.

The token counts were logged as `[REDACTED]` on the first try. The log
redactor blanks any key containing `token`, and it is right to: a key called
`anything_token` is a credential until proven otherwise, and loosening that
rule for one field is the wrong trade. So the counts are logged under names
that carry no trigger word — `prompt`, `completion`, `total` — while the
runtime's OpenAI-standard names stay on the provider, where nothing logs them.
Pinned through the real JSON formatter in `test_copilot_orchestrator.py` §8.

### 6. A record says exactly what was measured

An adoption record now carries the runtime (host and the model's `/v1/models`
entry, or the reason the probe failed), the configuration that shapes answers
(planning budget, timeout, temperature) and the identity of the question set
(`cases.py` digest, counts, harness commit). A record for "qwen3:8b" that
cannot say which runtime, which budget or which questions is a record of
nothing in particular — and any of those changing is a reason to re-evaluate.

## Consequences

**Positive.** The first real model routes 8/8 on the owner's own questions.
A truncated plan is now diagnosable instead of being silently scored as a
routing failure. The fix carries to any reasoning model AMP tries next.

**Negative.** Planning costs 2.1–12.9 s on this hardware, against ~4 ms for
AMP's own router. A model that thinks is not free, and the latency is real.

## Honest limits

- **A bigger budget is not a better model.** This ADR removes an artificial
  ceiling; it makes no claim about the model's accuracy. That is the
  evaluation's job, and its result is recorded separately.
- **Measured on one machine.** 12 GB VRAM, one laptop GPU, one quantisation.
  A CPU-only SME box will be slower and the numbers here do not transfer.
- **The stubs were not wrong, they were unrepresentative.** They tested that
  AMP does the right thing with a model's answer. They could not test what it
  costs a real model to produce one, and nothing in the suite noticed the gap
  until a real model was in the room.
