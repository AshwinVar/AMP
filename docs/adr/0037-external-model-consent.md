# ADR-0037: A company's data leaves AMP only with that company's consent

**Date:** 2026-09-21
**Status:** Accepted
**Builds on:** ADR-0020 (AMP-native AI; learning consent), ADR-0022 (typed tools and the grounding gate), ADR-0023 (self-hosted model behind an earned switch)

## Context

The Copilot may word an answer with a hosted language model. `ai_copilot.py`
has two: Anthropic (`ANTHROPIC_API_KEY`) and Gemini (`GEMINI_API_KEY`). Both
run outside infrastructure AMP controls, and both are marked `external = True`
on the provider class for exactly that reason. What reaches them is not the
database — ADR-0022 made sure of that: AMP plans, AMP's tools read the data
for the request's principal, and the model only words the answer. But the
wording call carries the question, AMP's draft answer and the evidence behind
it: machine, order and item names, figures, units, time windows. That is a
company's operational data, and it left AMP the moment a key was set on the
platform — for every tenant, with no company ever asked.

ADR-0023 said so and left it open: "Whether a company's data may be sent to an
external provider at all is a per-company consent question. It is the next
change." The sprint tracker carried it as the one open item under the provider
adapter. The self-hosted model closed the question for itself (nothing leaves
AMP, so `external = False` needs no consent); it did not close it for the
hosted ones, which stay configured as fallbacks.

ADR-0020 already built the right decision shape for the anomaly check's
learning step: a per-company row in `ai_learning_consents`, written only by an
Admin of that company, never from a founder preview, committed in the same
transaction as its audit record, read on every request with no cache so a
revocation bites on the next one, and a consent card that says in plain words
what is read, what is kept, who uses it and what happens without it. Learning
from a company's data and sending a company's data outside AMP are different
acts, and the same kind of decision.

## Decision

1. **A second consent capability, `external_model`.** `amp_ai.core.contracts`
   keeps `LEARNING_CAPABILITIES` as it was (`telemetry_baseline` only — AMP
   fits nothing to what it sends a model) and adds `CONSENT_CAPABILITIES`,
   the learning capabilities plus `external_model`: everything a company must
   consent to before AMP uses it. The gate, the writer and the consent page
   validate against `CONSENT_CAPABILITIES`; the card's wording for the new
   capability lives in `CAPABILITY_INFO` beside its enforcement, and names
   the providers, what leaves, what is kept (nothing by AMP; the provider's
   own terms govern the rest) and what happens without it.

2. **One chokepoint.** `ai_copilot._copilot_llm(db, current_user)` is the one
   place a request's `ProviderLLM` is built, and both `/ai/ask` and
   `/ai/report` reach a model only through it. For a provider with
   `external = True` it asks `external_model_allowed(db, current_user)`: the
   request's effective tenant (`request_tenant`, as on every read-model
   route), read through `DbConsentGate` on every call. No row, or a revoked
   one, means the model is not built; the route answers from AMP's own engine
   over the same evidence and the response's `note` says why, in the gate's
   own words (never turned on; revoked by whom and when).

3. **A founder preview never sends a company's data.** The consent an Admin
   gives covers the company's own users. A platform operator previewing the
   company (`X-Tenant`) is refused before the row is read, whatever the
   company or the founder's own workspace decided — ADR-0020's rule for the
   anomaly check, applied to the hosted model.

4. **The self-hosted model is exempt.** `external = False` skips the consent
   read entirely and keeps ADR-0023's adoption gate. The ADR's warning stands:
   pointing `AMP_LLM_BASE_URL` at a third party's service makes the "local"
   model an external one, and code cannot tell.

5. **`/ai/status` says so.** `enabled` keeps meaning "a provider is
   configured". A new `external` block says whether the configured provider is
   hosted and whether THIS company's questions will reach it (`consent`
   true/false with the reason, or null when there is nothing to consent to).
   The Copilot screen shows the reason where it used to promise
   "conversational answers by <model>".

6. **No schema change.** The capability is a new value in the existing
   `capability` column of `ai_learning_consents`; the audit namespace
   (`ai.learning_consent.*`) and its forgery guard are unchanged.

## Consequences

**Positive**
- A key on the platform no longer speaks for a company. Every hosted call is
  preceded by that company's own, audited, revocable decision, and the copilot
  says on every answer when it did not ask the model and why.
- The decision is where the anomaly check's already was: one card, one table,
  one audit trail, one rule about previews. An Admin who has seen one toggle
  understands the other.
- The Gemini free tier — whose data may be used for training, which the
  module docstring has always warned about — can no longer receive a paying
  customer's data by accident of configuration.

**Negative**
- One consent read per copilot request while a hosted key is configured (the
  gate is deliberately uncached, ADR-0020). It is an indexed point read on a
  table with a handful of rows per tenant, on an endpoint whose model call
  costs seconds.
- A configured key is no longer sufficient: an operator connecting a provider
  must know that every company answers from AMP's engine until its Admin
  consents. `/ai/status` and the copilot footer say so; the handbook says so.
- Existing tenants on a platform with a key lose the model's wording at the
  moment this ships, until they consent. That is the point, and it is stated
  in the rollout.

## Alternatives considered

- **An operator-level per-tenant allowlist (an environment variable naming
  the companies).** Not the company's decision, not audited, not revocable by
  the company, and invisible to it. Rejected.
- **Asking per question ("send this to Anthropic?").** Friction on every
  question, no record of the decision, and an Operator answering a question a
  company's Admin should decide. Rejected.
- **Gating inside `provider.ask()`.** The provider has no session and no
  tenant, and ADR-0022 built the model boundary so that it never does.
  Rejected; the gate sits where the request's principal is known.
- **Treating consent as implied by the plan or by using AMP at all.** ADR-0020
  refused that for learning and the reasons hold: "no row means no".

## Rollout

- No migration. Production with no hosted key (today) changes nothing.
- When a hosted key is set: every company's `/ai/ask` and `/ai/report`
  answer from AMP's own engine with a note naming the consent, until that
  company's Admin turns on "Send Copilot questions and evidence to a hosted AI
  model" under Agent Activity → AI consent. `/ai/status` reports the state.
- Pinned by `test_external_model_consent.py` (nine sections at the ASGI
  layer, the provider replaced by a recorder: it is never called without
  consent, is called with it, stops on the next question after revocation,
  another company's consent never counts, a preview never sends, the report
  path and the status agree, the self-hosted provider never consults the
  gate, and the structure has one chokepoint) and
  `mutate_external_model_consent.py` (16 mutations, each caught).
  `test_ai_copilot_fallback.py` grants the consent it needs first, so it still
  tests what happens once the provider IS reached.
