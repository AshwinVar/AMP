# ADR-0023: A self-hosted model behind the Copilot, switched on only when it has earned it

**Status:** accepted · **Date:** 2026-09-19 · **Extends** [ADR-0020](0020-amp-native-ai.md) (earned switches, pinned records), [ADR-0022](0022-copilot-typed-tools-evidence-and-grounding.md) (typed tools, grounding gate, evaluation)

---

## Context

The founder decided on 2026-09-17 that AMP must not depend on external AI models. The sprint asks for a self-hosted, open-weight model behind a provider abstraction, with Gemini kept only as an optional fallback, and with local inference becoming the default **only after it passes the evaluation thresholds**. ADR-0022 built most of what this needs: typed tools, the grounding gate and the evaluation harness.

Three things were missing:

1. **A provider** that speaks to a model AMP runs itself.
2. **An adapter** that gives a provider the orchestrator's `plan` and `phrase` methods.
3. **A mechanical switch.** "Only after it passes" was a sentence, not a check. Nothing stopped an operator from pointing AMP at an unevaluated model and having its wording shown to customers.

`/ai/ask` still used the snapshot prompt. The model received the whole factory as text and returned prose that nothing checked. It also took the tenant from the token's claim rather than from `request_tenant`, so a founder previewing a company got that company's rows priced at the founder's own unit value.

## Decision

### 1. One provider for every OpenAI-compatible runtime (`ai_copilot.LocalOpenAIProvider`)

Ollama, the llama.cpp server and vLLM all serve `POST {base}/chat/completions` with tool calling. The provider is configured by environment variables:

| Variable | Meaning |
|---|---|
| `AMP_LLM_BASE_URL` | the runtime's base URL (http or https only) |
| `AMP_LLM_MODEL` | the exact model name |
| `AMP_LLM_API_KEY` | optional bearer token |
| `AMP_LLM_TIMEOUT` | per-call timeout in seconds, default 30 |

Behaviour:

- **Scheme.** Only `http://` and `https://` are accepted. `urllib` would also open `file://`, and a mistyped setting must not turn the Copilot into a file reader.
- **Requests.** Temperature 0, not streamed, bounded tokens.
- **Errors.** Every failure raises an error that names what failed, never the response body, because the body can echo the prompt.
- **Registration.** It is registered **last** in `PROVIDERS`, so configuring it never takes over from a hosted provider an operator already relies on. `AI_PROVIDER=local` selects it explicitly.
- **Data leaving AMP.** It is the only provider marked `external = False`. The base URL must be infrastructure AMP runs, and the ADR says so because code cannot tell.

### 2. The adapter (`ai/llm.py`)

`ProviderLLM(provider)` gives a provider two methods:

- **`plan(question, tools)`** runs only when the provider has native tool calling (`can_plan`). It offers exactly the catalogue the asker's role may call, and reads `tool_calls` or a call written into the text. Every step still goes through `run_tool`.
- **`phrase(question, facts, draft)`** sends the question, AMP's draft and the facts. The facts and the draft are wrapped as **data**, and the prompt says instructions inside them are not to be followed.

A `<think>` block, which some runtimes emit, is stripped. Hosted providers have no tool calling here, so they word the answer and AMP's router plans, as `/ai/ask` always did: one model call per question.

**What the model sees.** It sees the question, the tool catalogue and the evidence facts. It never sees a tenant, a role, a user or a session. `test_copilot_local_provider.py` reads every request body sent to the stub model and checks none of those appear, and that no other factory's data appears either.

### 3. The earned switch (`ai/llm_adoption.py`, `ai/adopted_models.json`, `copilot_eval/adoption.py`)

A self-hosted model is used for `/ai/ask` only when a committed record says **that exact provider and model string** passed the adoption gate. The gate is AMP's own engine as the baseline, on the same three factories, questions and adversarial prompts:

- zero unauthorized disclosures;
- zero ungrounded texts shown;
- zero money figures fabricated;
- core tool selection, unseen tool selection and factual accuracy each at least AMP's own;
- a run measured on a different number of cases than its baseline fails.

The record is produced by `python -m copilot_eval --provider local --record FILE` against the real model, read in review, and committed. `test_copilot_model_adoption.py` fails the build if any record's `passed` verdict disagrees with the gate applied to its own metrics. The reader matches exactly: a record for `qwen3:8b` adopts neither `qwen3:4b` nor another quantisation. A missing or corrupt file adopts nothing.

**Until it has passed, the model is never called.** `/ai/ask` answers from AMP's own engine, with the evidence, and the note says why. No factory data reaches an unvalidated model. `/ai/status` reports whether a self-hosted model is configured, its model name and whether it is adopted; the endpoint address is not shown.

### 4. `/ai/ask` goes through the orchestrator

`/ai/ask` now runs `orchestrator.ask` with the principal from `Principal.from_user`, which takes the tenant from `request_tenant`. That fixes the preview's tenant. The answer carries the same evidence, tools and state as `/copilot/ask`:

- **Model worded it:** `source` is `llm` and `model` is set.
- **AMP worded it:** `source` is `rules`, `model` is `None`, and `note` says why. That happens when the model failed, timed out, was not adopted, or its wording did not pass the gate.

The drill-in view comes from the tools AMP ran, never from the model. `/ai/report` is unchanged in this ADR; the Daily Brief will replace it.

### 5. The orchestrator binds the tenant for the whole question

Routing reads the machine list to spot a named machine. When nothing bound a tenant (a background job, a test, a future caller), that read saw every company's machines. Tenant A asking "how is WELD-07?" then got a different kind of answer when another company had a WELD-07 than when nobody did, which reveals that the other company's machine exists.

The middleware binds a tenant for every request, so HTTP requests were not affected. `orchestrator.ask` now binds the principal's tenant itself for the whole question. `test_copilot_orchestrator.py` asserts that an unbound question naming another company's machine routes exactly like one naming a machine nobody has.

### 6. What stays as it was: the hosted providers

Anthropic and Gemini are used, when configured, exactly as before, now through the orchestrator and behind the grounding gate. They are not subject to the adoption record, because the sprint's rule is not to remove a working fallback before its replacement is verified.

Whether a company's data may be sent to an external provider **at all** is a per-company consent question. It is the next change, and it is recorded as open in the sprint tracker.

## Consequences

**Positive.**

- AMP can run a self-hosted open-weight model with no SDK and no new dependency.
- The switch to that model is a reviewed record of an evaluation, not an environment variable.
- `/ai/ask` answers carry evidence.
- The preview's tenant is fixed.
- The latent existence side channel is closed for every caller.

**Negative.**

- `/ai/ask` costs the plan's tool queries rather than the snapshot's. Two pinned tests now assert the new invariant: a model and the drill-in view add no queries beyond AMP's own answer.
- A self-hosted model has to be evaluated before it is useful.

## Honest limits

- **No real model has been evaluated.** The only model run through this path is a stub server. Downloading weights is waiting on the founder's permission.
- **Local tool calling is unverified.** It depends on the runtime and the model; smaller models often produce malformed calls. `run_tool`'s validation and the fallback to AMP's plan are what make that safe, and the evaluation is what will say whether it is useful.
- **Production has no GPU.** Railway has none. Where the model runs is an infrastructure and cost decision for the founder.
