# ADR-0038: A consent names the provider it was given for

**Date:** 2026-09-21
**Status:** Accepted
**Builds on:** ADR-0037 (a company's data leaves AMP only with its consent), ADR-0020 (learning consent), ADR-0017 (a grant whose meaning changed is asked again)

## Context

ADR-0037 made a company's Copilot questions and evidence go to a hosted
language model only after an Admin of that company turned `external_model` on.
The consent row recorded which company decided and when. It recorded nothing
about **what** they decided on.

That matters because two hosted providers can be configured, and they do not
offer the same terms. `ai_copilot.py` has said so from the start: Anthropic
under commercial data terms, for real clients; Gemini's free tier, "DEMO USE
ONLY; free-tier data may be used for training". The operator switches with
one variable (`AI_PROVIDER`), and when it is unset the registry auto-detects
whichever key is present, Anthropic first. So an operator who removed the
Anthropic key while a Gemini key was still set would have moved every
consenting company's questions onto a free tier that trains on them — with no
decision made, no row changed, and nothing on the consent page looking any
different.

The card's wording named both providers, which made the consent honest as
words. It did not make it a decision about a provider. A consent whose meaning
can change under it without anyone deciding is the shape ADR-0017 refused for
OEM grants ("ask again rather than start sharing on them"), and #616 refused
for advance consent to reserved grants.

## Decision

1. **`external_model` is a SCOPED capability** (`SCOPED_CAPABILITIES` in
   `amp_ai.core.contracts`). A grant to it records the hosted provider
   configured at that moment in a new column, `ai_learning_consents.scope`
   (`"anthropic"`, `"gemini"`). The learning capabilities name no third party
   and keep `scope` NULL.

2. **The gate honours a scoped row only for the provider it names.**
   `DbConsentGate.check(db, tenant, capability, scope=...)` is asked, by the
   Copilot's chokepoint, with the name of the provider about to be used. A row
   given for another provider, or a row from before AMP recorded providers
   (NULL), is a refusal that says what it was given for and what is configured
   now, until an Admin decides again. A scoped check with no provider is a
   refusal too: there is nothing to apply it to.

3. **No advance consent.** `PUT /ai-consent/external_model` records the
   provider configured at that moment and is refused (400, with the reason)
   when none is; `set_consent` refuses the same directly. The switch on the
   card is disabled with the reason when nothing hosted is configured. This is
   founder decision 3's answer applied here: a consent to nothing is not a
   consent.

4. **The page says what a decision was for.** Each capability carries
   `scoped`, `scope` (what the grant named), `configured` (what AMP would use
   now) and `active` (granted, and for what is configured). The card shows a
   grant given for another provider as *Not active*, names the configured one,
   and a click reviews and re-grants for it rather than withdrawing; the
   confirmation names the provider the consent is for and says it lapses if
   the platform is switched.

5. **A revocation keeps the scope** (the row says what was withdrawn); the
   next grant writes its own. The audit record of a scoped grant carries the
   provider; a learning capability's record is unchanged.

## Consequences

**Positive**
- Switching providers, or auto-detection falling through to the other key,
  can no longer carry one company's decision about one provider's data terms
  over to another's. Every company answers from AMP's own engine until its
  Admin decides again, and every surface says why.
- The migration invents nothing: a row from before this revision has no
  provider named, and the gate treats that as "decide again", which is the
  only true reading of it.

**Negative**
- A schema change (migration `0012_consent_scope`, one nullable column, added
  at boot too by `main.py` for a database that starts before its migrate step
  ran). It brings the full set: fresh-schema and upgrade-from-previous tests,
  the PostgreSQL verifier in the migration gate, and this record.
- Existing grants on a platform with a hosted key lapse at deploy time until
  re-given. That is the point, and the card says so.
- The consent does not (yet) name the model, only the provider: a provider
  changing its default model under the same terms does not lapse a consent. A
  provider whose *terms* change is a matter for the provider's contract, which
  AMP cannot read.

## Alternatives considered

- **Naming both providers in the card's wording and leaving the row
  provider-less** — what ADR-0037 shipped. Honest words, not a decision about
  a provider; the auto-detect fallthrough makes the switch an accident, not
  a choice. Rejected.
- **One capability per provider (`external_model:anthropic`,
  `external_model:gemini`)** — no schema change, but the card would show a
  toggle for a provider that is not configured, or hide rows an Admin needs
  to revoke, and the audit trail would split one decision across two names.
  Rejected.
- **Storing the provider in the audit record only** — the gate reads the
  consent row, not the log; reading the latest grant's log entry to learn its
  scope would make the audit trail load-bearing for authorization, which it
  is not designed to be. Rejected.
- **Refusing the Gemini free tier outright for consenting companies** — a
  product decision about a provider's terms that is the founder's to make,
  and one this record does not need: with the provider named, an Admin who
  consents to Gemini has consented to Gemini.

## Rollout

- `alembic upgrade head` adds the column; `main.py` adds it at boot as well.
  No row is rewritten.
- Production has no hosted key, so nothing changes there. When one is set,
  every company's Admin decides for that provider.
- Pinned by `test_external_model_consent.py` §10 (granted under Anthropic →
  sent; Gemini configured → not sent, the note and `/ai/status` and the page
  say what it was given for and what is configured; decided again for Gemini
  → sent, the audit record names it; a learning consent has no scope; no
  provider → the grant is refused by the route and by the writer; a NULL-scope
  row is asked again), `mutate_external_model_consent.py` (+7 mutations),
  `test_migration_0012_consent_scope.py` (chain, model, upgrade from the
  previous revision with rows, downgrade round trip, boot repair) and
  `verify_pg_consent_scope.py` in the migration gate (from the frozen baseline
  with consent rows, on PostgreSQL).
