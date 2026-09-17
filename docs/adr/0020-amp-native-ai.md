# ADR-0020: AMP-native AI (standard-library models, pinned artifacts, per-tenant learning consent)

**Status:** accepted · **Date:** 2026-09-17 · **Extends** [ADR-0002](0002-tenant-scope-core-domain.md) (tenant scope), [ADR-0003](0003-ai-as-event-consuming-platform.md) (AI platform), [ADR-0015](0015-server-side-approval-gate.md) (server-side gates)

---

## Context

Until this change AMP had no trained models. Its "predictive" features are the
hand-weighted rule scorer (`predictive_engine.py`, `ai/prediction.py`), and its
copilot is a keyword router with an optional external LLM. A founder asking
"can AMP have its own AI?" faces five risks that are easy to walk into:

1. **Dependencies.** numpy, scikit-learn or torch would change the deploy and
   the attack surface for a small model.
2. **Silent training on customer data.** Once a model can learn, the easy path
   learns from whatever tenant data is at hand, without the tenant being asked.
3. **Circularity.** A model trained on labels produced by the rule scorer just
   learns the rules, and then "beats" them on its own terms.
4. **Unmeasured claims.** An accuracy figure without a held-out evaluation against
   the existing baseline on the same data is marketing, not engineering.
5. **Authorization by model.** A model that builds queries, picks tenants or
   decides roles is a new, unaudited access path.

## Decision

### 1. Pure Python, JSON artifacts, pinned hashes

`backend/amp_ai/` implements training and inference with the standard library
only (`amp_ai/core`: logistic and multinomial regression, robust baselines,
metrics, cluster bootstrap, splits). No entry was added to `requirements.txt`,
and a purity test fails on any import of an ML, network or pickle-style module.

A model is a JSON artifact: schema version, model type, created_at, training data
source, consent basis, features, parameters, held-out metrics, baseline metrics,
the adoption verdict, the evaluation ledger and a SHA-256 of the canonical
payload. `load_artifact` refuses NaN/Infinity, duplicate keys and oversize files,
then checks the embedded hash AND the hash **pinned in code**
(`predict.ARTIFACT_SHA256`, `service.EVAL_SHA256`, `classifier.ARTIFACT_SHA256`).
Anyone who edits a verdict and recomputes the embedded hash still fails the pin,
so changing a model is a reviewed code change. Nothing is ever pickled, eval'd or
imported from an artifact.

### 2. Synthetic training only, from independent generators

Each model is trained or evaluated only on data from a documented generator
committed BEFORE the feature code (`failure_risk/synthetic.py`,
`telemetry_anomaly/synthetic.py`, the AMP-authored copilot corpus). No generator
imports or is labelled by the rule scorer. Each generator's SHA-256 is recorded in
the artifact. Every surface carries the caveat **"Evaluated on synthetic machines
only; not evidence of accuracy on real plants."**

### 3. Measure before claiming: gates and a test-set ledger

Every figure comes from a runnable build with a fixed seed (20260917), compared
with the existing baseline on the SAME held-out rows, with 95% cluster-bootstrap
intervals. Each build has an adoption gate. The artifact's ledger counts runs per
test set, and adoption requires the first run, so a model cannot be tuned against
its test set until it passes.

| Model | Baseline (same data) | Verdict |
|---|---|---|
| `failure_risk` (logistic, 7-day breakdown start) | rule scorer | **Adopted** (on synthetic data). PR-AUC 0.194 vs 0.094; paired difference +0.099 [0.029, 0.175]; ROC-AUC 0.698 vs 0.612; Brier 0.0491 below base rate 0.0524; ECE 0.006 |
| `telemetry_anomaly` (robust median/MAD + Mahalanobis) | mean/std z-score, static range checks | **Not adopted.** Beats static ranges (PR-AUC +0.225 [0.150, 0.304]) but loses to mean/std (−0.103 [−0.158, −0.047]) |
| `copilot_intent` (multinomial, hashed n-grams) | keyword router | **Not adopted.** External pool of 64 questions: 38 vs 36 correct (+3.1 points, McNemar p = 0.625); the gate needs +5 points and p < 0.05 |

"Adopted" for failure risk does **not** change any existing screen. It lets
`GET /ai/native/failure-risk` show the model's probability first, with the rule
score beside it. Replacing the rule on Machine Health, the briefing or the agents
would need a real-data backtest, and that needs its own consent capability and ADR.

### 4. The authorization chain, and where the model sits in it

**USER → AUTHENTICATION → RBAC → TENANT / OEM / CONSENT → AMP TOOL → DATA → MODEL.**

`native_ai_routes.py` authenticates with `get_current_user` (which refuses OEM
principals), checks roles with `require_roles`, takes the tenant from
`request_tenant(current_user)`, and hands the model only what the tenant-scoped
data layer returned. Every query in `db_history` and `db_telemetry` filters
`tenant_code` explicitly and bounds time at both ends, so a request with no
ambient tenant still reads one tenant.

| Route | Roles | Plan pack |
|---|---|---|
| `GET /ai/native/failure-risk` | Admin, Supervisor | Intelligence |
| `GET /ai/native/anomaly/machines/{id}` | Admin, Supervisor | Intelligence |
| `GET /ai/models`, `GET /ai/models/{name}` | any signed-in factory user | Intelligence |
| `GET /ai-consent` | Admin, Supervisor | none, on purpose |
| `PUT /ai-consent/{capability}` | Admin, not from a founder preview | none, on purpose |

The copilot model only proposes an intent: a `RouteDecision` naming one of the
15 allowlisted pillars. `ai.assistant.answer(..., proposer=)` asks it only after
the machine-name lookup and the `find` prefix. It uses the proposal only if it
names an allowlisted pillar, and falls back to the keyword router on anything
else, including an exception. The pillar runs with the request's tenant. The
model never sees a session, a tenant or a role.

A model card name is looked up in the fixed `registry.MODELS`; it is never used
to build a path. Cards carry metadata and metrics, never `parameters`.

### 5. Inference is not learning; learning needs stored, audited, revocable consent

SCORING a tenant's records with a synthetic-trained artifact reads the data but
learns nothing: `predict()` fits nothing, imputes from training medians, and
leaves the artifact unchanged (tested). FITTING anything to a tenant's own data
is learning. The one capability that does so today is `telemetry_baseline`: the
anomaly check fits one machine's normal range from its own last 14 days.

- Consent is stored in `ai_learning_consents` (migration `0009_native_ai_consent`),
  one row per (tenant_code, capability), UNIQUE. **No row means no.**
- The table is outside the ADR-0002 hook (the `AgentPolicy` precedent); every read
  filters `tenant_code` explicitly, and `test_unscoped_model_reads.MANUALLY_SCOPED`
  records why.
- `DbConsentGate` reads the row on every call, with no cache, so a revocation
  applies to the next request.
- `amp_ai.consent.set_consent` is the only writer. It adds the consent row and its
  `AuditLog` row (built by `platform_routes.build_audit_row`, the same factory
  `log_audit` now uses) and **commits once**. If the audit row cannot be written,
  both are rolled back. `log_audit` itself commits separately and swallows errors,
  which is right for a login line and wrong here.
- `service.score_machine` checks that the machine belongs to the tenant (404)
  BEFORE consulting the gate, so another tenant's machine id reveals nothing about
  consent. Without consent it raises `ConsentRequired` and reads no telemetry; the
  route answers 403 `{code: "learning_consent_required", capability, reason}`.
- **Founder preview cannot consent, and cannot learn.** A platform Admin previewing a
  customer (X-Tenant) may read the page, but the PUT refuses when the effective tenant
  differs from the token's tenant claim. Consent to learn from a company's data is
  that company's decision. The founder's own DEFAULT workspace may consent for itself.
  The same test refuses the learning step itself: what an Admin agrees to is the
  company's OWN Admins and Supervisors opening the anomaly check (the consent text
  says so), so from a preview `GET /ai/native/anomaly/machines/{id}` answers 403
  `{code: "learning_not_from_preview"}` before the consent row or any telemetry is
  read. Failure risk learns nothing and answers in a preview like any factory read.
- `/ai-consent` is not under `/ai/`, so the API is not plan-gated: a downgraded tenant
  can still see and withdraw consent. **Known gap:** the consent card is rendered in
  Agent Activity, which the UI shows only with the Intelligence Pack, so today a
  downgraded tenant withdraws through the API (or support) until the card is also
  placed in an always-open view.
- Offboarding with `?purge=true` purges consent rows (they carry `tenant_code`) and
  keeps the audit trail. Deleting a company from the registry WITHOUT purge also
  deletes its consent rows (`consent.remove_for_company`), in the same commit as the
  registry row and with one `ai.learning_consent.removed_with_company` audit record
  per row: a delete without purge leaves the tenant's other rows behind and the code
  can be registered again for a different company, which never opted in. (Those
  other leftover rows are a pre-existing problem outside this ADR.)
- The consent history cannot be written by hand: `POST /audit-logs` answers 400 for
  any action in `ai.learning_consent.*` or entity type `ai_learning_consent` (case
  and padding ignored), so every record there was written by `amp_ai.consent`.

### 6. No persistence of tenant baselines; windows kept apart

The anomaly baseline exists only for the request. Nothing tenant-derived is
stored, cached or pooled, so there is nothing to leak between tenants and nothing
to delete on revocation. The score window is `[now − 1h, now)` and the baseline
window is `[now − 14d, now − 1h)`: a real anomaly cannot shift its own baseline.
Inside the baseline, fit (oldest 70%) and calibration (newest 30%) are disjoint.
Any stored baseline would need a new ADR with consent-scoped storage and deletion
on revoke.

### 7. Why amp-native is not in `PROVIDERS`

`ai_copilot.PROVIDERS` is the LLM precedence list, and `_provider()` /
`_ai_enabled()` mean "an LLM is configured" to every caller and to two pinned
suites. `AmpNativeProvider` (`NATIVE`) sits beside it. `is_configured()` is true
only when the artifact verifies against the pin, its gate says adopted, and
`AMP_NATIVE_COPILOT` is not `off`. The committed v1 is not adopted, so today the
engine is `rules` and every `/copilot/ask` answer is exactly the keyword router's.
`/ai/status` reports `engine` and `native {available, adopted, version}`.

### 8. What is not a source

OEM telemetry is not stored by AMP (ADR-0017), so it cannot feed a baseline.
`industrial_signals` rows count only with `quality == "Good"`, and only by the
`machine_id` stored on the row, which is set when the row is written, so
relinking a device does not move its history. Where that id comes from depends on
the write path: the demo adapters (`industrial_adapters.py`) copy the device's
`linked_machine_id`, but `POST /industrial/signals` stores `machine_id` (and
`quality`, default "Good") straight from the request body, without checking that
the device is linked to that machine. Other tenants' rows are still excluded (the
loader filters `tenant_code` explicitly), but **within a tenant any Admin or
Supervisor can post "Good" readings for any machine from any device, and those
readings feed that machine's consented baseline.** The loader does not yet require
`machine_id` to equal the device's `linked_machine_id`; that is an open item.

## Consequences

**Positive**
- A model's claim is a file anyone can rebuild, bound to a hash in code, with the
  baseline's number beside it. Two of three models honestly say "not adopted".
- A tenant's data is read for inference only through the existing tenant-scoped
  data layer, and learned from only with an Admin's stored, audited, revocable yes.
- No new runtime dependency and no network call on any native path (tested with
  sockets and `urlopen` stubbed to fail).

**Negative**
- Synthetic evaluations measure the generator authors' assumptions. Misspecification
  suites are reported beside each result, but only real-plant data can confirm them.
- Per-request cost: failure risk loads 120 days of history for every machine and
  re-verifies its artifact; the anomaly check reads up to 50k telemetry rows. Both
  are rate limited (`RATE_LIMIT_AI`), and so are the model cards. The bucket is the
  route prefix AND the verified token's principal (`http_security.PRINCIPAL_KEYED_PREFIXES`),
  not the client address: walking machine ids or sending a new X-Forwarded-For does
  not buy a fresh budget, and a request without a validly signed token is not
  counted (authentication refuses it before any work), so a stranger who knows a
  factory's address cannot spend its users' budget. A single user who holds a valid
  token is limited per instance only (one uvicorn worker today). `/ai/ask`,
  `/ai/report` and `/copilot/ask` still key on the left-most X-Forwarded-For hop,
  as before this ADR.
- The copilot artifact is ~0.9 MB of JSON; every rebuild adds that to git history.
- 14-day telemetry retention means baselines see no seasonality, and a new machine
  gets `insufficient_history` for at least 3 days.

## Alternatives considered

- **scikit-learn / numpy.** Rejected: a dependency, and pickle-shaped artifacts.
- **An LLM as the intent router.** Already exists (with its data terms); the point
  here is a model that runs inside AMP with nothing leaving it.
- **Consent as a TenantConfig flag or env var.** Rejected: not per capability,
  not audited in the same transaction, and an env var is not the tenant's decision.
- **Storing per-machine baselines.** Deferred: it creates tenant-derived state that
  must be deleted on revocation and isolated per tenant; nothing needs it yet.

## Rollout

1. Migration `0009_native_ai_consent` is additive (one table). Downgrade drops
   only that table; `AuditLog` keeps who granted what.
2. No existing screen changes. The Agent Activity view gains the consent card
   (Admin can toggle, Supervisor read-only, Operators not shown) and the model cards.
3. A future model is adopted only by a new build that passes its gate on a FRESH
   test set (new seed or question set), with the new hash pinned in code.

## Verification

`backend/test_amp_ai_integration_{migration,consent,routes,copilot,isolation,structural}.py`,
`backend/mutate_amp_ai_integration.py` (integration guards), `backend/verify_pg_native_ai.py`
(PostgreSQL), the capability suites `test_amp_ai_core_*`, `test_amp_ai_failure_risk_*`,
`test_amp_ai_anomaly_*` and `test_amp_ai_intent_*`, and `frontend/lib/aiModels.test.ts`.
