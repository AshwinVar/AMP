# ADR-0020: Agreed downtime attribution for service contracts

**Status:** accepted · **Date:** 2026-09-17 · **Extends [ADR-0017](0017-oem-fleet-and-cross-tenant-equipment.md) and [ADR-0019](0019-factory-controlled-machine-claim.md).**

---

## Problem

Machine makers already sign service contracts with the factories that run their
machines: annual maintenance contracts, warranties, uptime clauses. At the end
of every month the two sides argue about the same thing: whose fault was the
downtime? The machine stopped. Was that a fault in the machine, or was the
factory out of material, out of operators, in a changeover, or without power?

Each side answers from its own records, and neither trusts the other's. The
uptime clause then either goes unenforced or becomes a negotiation.

AMP already receives both halves of the evidence for connected machines: a
status feed that says when a machine stopped (and when AMP heard nothing), and
the factory's own MES downtime reasons that say why.

## Prior art, and what is therefore NOT the product

Research before building found that the obvious product already exists:

- **Pay-per-use contracts and telemetry metering** exist (SteamChain,
  PayperChain, Linxfour).
- **Shared usage ledgers** exist, including Rockwell's **US10747201B2** (active
  to 2038) on subscription and usage billing recorded on a blockchain shared
  with the OEM.

So AMP **does not** build a blockchain, a smart contract, a hash-chained usage
or billing ledger, usage metering for billing, or pay-per-output billing. It
claims **no novelty** for metering or for shared ledgers.

What the research did not find is a statement that splits downtime between the
machine maker and the factory **using the factory's own MES reasons**, agreed
by both parties. That attribution is the differentiator, and it is the whole of
this decision. **A freedom-to-operate review is needed before commercial
launch**, especially for the United States.

## Decision

### 1. A monthly (or quarterly) attribution statement

For a contract the factory has accepted, AMP computes one statement per whole,
closed period. Every covered second of every covered machine goes into exactly
one bucket, with a cause and the evidence for it:

| Bucket | Meaning |
|---|---|
| **AVAILABLE** | a trusted source reported Running or Idle |
| **OEM** | a down episode attributed to the machine (its reason maps to OEM, or no reason and the agreed default for that status is OEM) |
| **FACTORY** | a down episode attributed to the factory's own stop (planned stop, no material, no operator, power, changeover...) |
| **DISPUTED** | the rules cannot decide (an unmapped reason, reasons for both parties, sources that disagree, an unrecognised status, an open dispute) |
| **UNMEASURED** ("No data") | no trusted telemetry held that time, or the installation was unlinked |

UNMEASURED is never counted as uptime or as downtime. Percentages are intensive:
no attributable time gives no availability and no credit, never 0.

### 2. Attribution rules (the engine is a pure function: `attribution_engine.py`)

- **Status history comes from per-source spans**, not `MachineEvent`
  (`telemetry_coverage.py`, table `machine_telemetry_spans`). `MachineEvent` is
  written only when a status differs from the shared `Machine.status`, so it
  cannot say what one source reported: MQTT Running, a manual PATCH to
  Breakdown, MQTT Breakdown leaves no MQTT event for the second report, and a
  timeline filtered to MQTT would read Running throughout. Every status-bearing
  message of a source extends or opens a span of that source. The manual status
  PATCH writes no span.
- A span's status holds for `SPAN_GAP_SECONDS` (300 s) after its last message,
  cut at the next span of the same source. Where no trusted span holds: UNMEASURED.
- Running or Idle is AVAILABLE (Idle means ready but not scheduled). Breakdown,
  Maintenance and Offline start a down episode.
- A down episode is attributed from the factory's `DowntimeLog` reasons logged
  from `reason_lead_seconds` before it starts until it ends. Generic reasons
  (MQTT's automatic "Breakdown", "unknown") count as no reason. No explicit
  reason: the contract's `status_defaults`. All explicit reasons map to one
  party: that party. Any unmapped reason, or reasons for both parties: DISPUTED.
  Reasons cannot be backdated: the server stamps `created_at`.
- An open dispute makes its window DISPUTED; a resolved dispute overrides the
  window with the agreed bucket.
- **Coverage ends where the installation's link changes** (`contract_linkage.py`).
  A before-flush listener stamps `coverage_ended_at` when an installation's
  `machine_id` or `factory_tenant_code` changes; time after it is UNMEASURED
  `installation_unlinked`. Earlier periods are never rewritten. A bulk UPDATE
  bypasses the listener, so the one bulk writer that can change a covered
  link (releasing equipment) calls `end_coverage` itself, and a structural test
  fails for any other.

### 3. SLA and credit (`contract_money.py`)

Pooled across the contract's machines, with exact decimals (`decimal.Decimal`,
money as two-place decimal text, `ROUND_HALF_UP` once, never float):

- not evaluable when there is no covered time, when measured time is below
  `min_measured_pct`, or when nothing is attributable;
- **pending disputes** when any time is DISPUTED: the availability and credit
  range across the possible outcomes, and no amount, and the statement cannot be
  accepted until a dispute settles that time;
- otherwise availability = (base − OEM) / base where base = covered − no data −
  factory time, compared with each tier by exact integer cross-multiplication;
  the credit is the period fee × the largest matching credit percentage.

AMP **computes** the credit. It never invoices, charges a card, moves money or
integrates a payment processor. The signed paper contract governs; the statement
is an annex.

### 4. Explicit acceptance by both parties, bound to a revision

- Nothing is attributed and no statement exists until the **factory explicitly
  accepts the contract** with the exact terms hash it reviewed **and**
  `grant_downtime_sharing: true`. The grant widens the existing
  `OemDataSharingPolicy` through `oem_sharing.widen_grants`; it never replaces
  it. The screen tells the factory exactly what that discloses. (Spans are
  recorded for every connected machine as part of ingest, like any other
  telemetry; the manufacturer can read them only through an accepted contract's
  statements and only while the grant stands.)
- Terms change only by an amendment its author proposes with a hash and the
  other party accepts with the same hash, effective at a period boundary.
- Each statement revision has **one SHA-256** of its canonical content
  (`canonical.py`: sorted keys, no whitespace, NFC text, whole-second UTC
  timestamps, decimals as text; floats refused).
- An acceptance records who, when, the content hash **and the revision**. It is
  valid only while both equal the statement's current ones
  (`canonical.acceptance_is_valid`, the only copy of the rule). Any recompute
  that changes the content bumps the revision and cancels every acceptance, even
  when a withdrawn dispute returns the bytes to an earlier state (finding C1).
- A statement is **agreed** when both parties hold a valid acceptance. An agreed
  statement is frozen.

### 5. Integrity: a hash per revision and the audit log, nothing chained

Every contract action writes two audit rows inside the business transaction
(`log_audit(commit=False, tenant_code=...)`): one in the factory's tenant and
one in the manufacturer's sentinel tenant. A failed commit leaves neither the
change nor its record.

`verify` reports `consistent` when the stored bytes hash to the statement's hash
and the attribution records say exactly what the bytes say, plus a live
recompute. **It does not prove that nobody with full database access rewrote
the bytes, the hash, the records and the acceptances together.** Only the
parties' own saved copies can show that, which is why each party can download
the exact bytes and re-hash them in the browser ("Check consistency"). Nothing is
chained, by design. Statements are described as consistency-checked and nothing stronger.

### 6. Consent and tenancy

- A factory sees only contracts addressed to it and proposed; an OEM sees only
  its own. Anything else is one 404.
- Statement content, preview, verify, download, compute, acceptance and dispute
  actions need the factory's `SHARE_DOWNTIME` grant for the manufacturer, read
  on every request (`oem_sharing.contract_statement_visible`). Terms, periods,
  disputes list and history stay visible to a party. Without the grant the
  manufacturer sees "sharing withdrawn by factory" and no numbers.
- Engine reads bind the factory explicitly (`oem_sharing.bound_factory_read`,
  which refuses a blank or sentinel tenant) and every query also filters the
  factory tenant explicitly and is bounded at both ends in UTC, half-open.
- The manufacturer never receives production counts, work orders, customers,
  notes, operators or machine names. Evidence is an allowlist: span id, source,
  status and clipped times; downtime log id, time and reason text; dispute id,
  status and bucket; linkage stamp and reason.
- Contract tables carry `oem_code` and `factory_tenant_code`, never `tenant_code`,
  so the generic offboarding purge cannot delete the other party's copy.
  Offboarding a factory closes its contracts (`_close_service_contracts`):
  proposals withdrawn, accepted contracts terminated at the next period
  boundary, statement content, records and disputes removed, hashes and
  acceptances kept.

### 7. Why not reuse `OeeWindow`

`oee_contract.OeeWindow` is a rolling window ending now. Statement periods are
anchored calendar periods in the contract's own timezone, converted to UTC and
half-open. Mixing a rolling window with an anchored one is the defect class
behind a 17-finding audit, so `contract_periods.py` does not import it and a
structural test enforces that.

### 8. Retention

Spans are kept **400 days** by `span_end` (`retention.py`). An operator's
`--days` override can lengthen that window but never shorten it. Past retention a
statement cannot be recomputed (`EvidenceExpired`); acceptance then uses the
stored revision.

## Honest limitations

1. `DowntimeLog` has no start or end, and reasons are free text. A reason is tied
   to an episode by proximity; a typo is unmapped (DISPUTED). The factory can see
   before accepting which of its reasons the terms map (`reason-vocabulary`).
2. Resolution is bounded by the gap tolerance: up to 300 s after the last message
   counts as the last reported status.
3. **Statuses are not authenticated as the manufacturer's.** MQTT, `/iot/telemetry`
   and `/industrial/signals` are provisioned or posted by the factory. Either side
   can distort inputs: the factory through statuses and reasons, and a gateway
   that goes dark turns downtime into No data. The remedies are the contract's
   `trusted_sources` (MQTT only by default), `min_measured_pct`, visible evidence
   and disputes. There is no fraud detection, and AMP cannot check that a PLC maps
   states truthfully; that belongs in commissioning.
4. Evidence is a snapshot. After 400 days a live recompute is impossible. A
   coverage end is dated when AMP observes the unlink; an earlier physical move
   cannot be detected. Withdrawing a dispute past retention cannot revise the
   statement, which then stays DISPUTED for that window.
5. Integrity is one hash per revision plus the audit log. Anyone with full
   database access could rewrite content, hashes and acceptances together.
6. The SLA is pooled; one SLA metric; whole monthly or quarterly periods; only
   the OEM drafts contracts; the draft editor is a JSON terms editor with defaults.
7. An agreed statement is final. Corrections happen outside AMP (credit notes).
8. No invoicing, payments, usage billing or pay-per-output. Pay-per-output billing
   is a later phase and out of scope.
9. **A freedom-to-operate review is required before commercial launch**
   (US10747201B2 in particular). SME adoption is unverified.
10. `tzdata` is a new runtime dependency.
11. A factory can withdraw `SHARE_DOWNTIME`, which withholds every statement from
    the manufacturer and so blocks its acceptance. That is a contractual remedy
    outside AMP. Offboarding removes the grant with the factory.
12. `SHARE_DOWNTIME` covers the whole manufacturer-factory relationship, not one
    machine, and the acceptance disclosure says so.
13. Span writes add at most one UPDATE per 30 s per machine and source.
14. The demo simulator writes spans with source `simulator`, which a real contract
    should never trust.
15. A founder-workspace Admin previewing a customer (X-Tenant) can accept a
    contract on the customer's behalf, consistent with the existing claim and
    sharing routes; this is a decision for the founder.

## Consequences

**Positive.** Both parties read one statement, built by one engine, with every
minute linked to evidence they can inspect, and each accepts an exact revision.
Missing data is visible as No data instead of being argued about. The existing
consent model governs what the manufacturer sees.

**Negative.** Eight new tables (migration `0009_outcome_contracts`), a new
per-message write on ingest, a listener on every flush that touches an
installation, and a product whose inputs a determined party can distort, with
disputes as the only remedy.

## Where the code is

| Concern | File | Tests |
|---|---|---|
| Canonical bytes, hash, acceptance rule | `canonical.py` | `test_canonical.py` |
| Terms, periods, money | `contract_terms.py`, `contract_periods.py`, `contract_money.py` | `test_contract_terms.py`, `test_contract_periods.py`, `test_contract_money.py` |
| Attribution | `attribution_engine.py` | `test_attribution_engine.py` |
| Statements | `contract_statements.py` | `test_contract_statements.py` (SQLite and PostgreSQL) |
| Routes and rules | `service_contracts.py`, `oem_contract_routes.py`, `service_contract_routes.py` | `test_contract_*.py`, `audit_oem_contracts_adversarial.py` |
| Spans | `telemetry_coverage.py` and the MQTT, HTTP ingest and simulator writers | `test_telemetry_coverage.py` |
| Coverage end | `contract_linkage.py` | `test_contract_linkage.py` |
| Consent helpers, audit | `oem_sharing.py`, `platform_routes.log_audit` | `test_oem_sharing_helpers.py`, `test_audit_explicit_tenant.py` |
| Offboarding | `offboard_tenant._close_service_contracts` | `test_contract_offboarding.py` |
| Screens | `frontend/components/contracts/*`, `OemContracts.tsx`, `ServiceContracts.tsx` | `*.test.tsx`, `components/contracts/wiring.test.ts` |
| Mutation harnesses | `mutate_canonical.py`, `mutate_contract_engine.py`, `mutate_service_contracts.py`, `mutate_contract_integration.py`, `frontend/mutate-contracts-ui.mjs` | |
