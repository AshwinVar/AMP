# AMP — Chief Engineer State

> Handover file. A new session should be able to read only this and continue.
> Keep it short. Update it at the end of every completed task.

**Updated:** 2026-09-03 (performance line closed; see WHAT IS ACTUALLY OPEN)
**Master SHA:** `040d30a` (#539)
**Production SHA:** `040d30a` — verified `{"status":"ok","database":"ok","schema":"ok"}` at 22:49 UTC. Railway auto-deploys master, so this tracks HEAD; re-check `/health` rather than trusting this line's age.

---

## LAST COMPLETED TASKS

| Task | Priority | Status |
|---|---|---|
| MQTT→WebSocket bridge: `asyncio.run` on a sync callee raised `ValueError` every message; delivery ran on a throwaway event loop | P1 | fixed, tested |
| MQTT ingest published no domain events — machine-reported breakdowns never reached the bus, so the Escalation agent was blind to them | P1 | fixed, tested |
| AI Phase 1 — `AIProvider` registry replacing four if/elif chains | P6 | merged #526 |
| AI Phase 5 — evaluation harness scoring routing / grounding / tenant isolation on deterministic ground truth; **24/24, and proven non-vacuous** | P6 | done |
| Route reconciliation: 143 frontend calls vs 256 registered routes — **0 unmatched**, nothing broken end-to-end | P2 | verified clean |
| Founder handbook verified against master by 10 subsystem specialists: 385 claims confirmed, 20 misleading | P8 | verification done, affected sections synced |
| `/machine-health` N+1: 3 queries per machine on a 3s poll (607 statements at 200 machines) — **measured first**, then batched to a flat 10 | P4 | fixed, tested, measured |
| Static guard against the whole class: no route may hydrate an append-only table without a limit or a date window. AST-based — an earlier regex version flagged a function whose *docstring* quoted the pattern | P4 | added, 11 checks, 2/2 reverts red |
| **Two 3-second-poll endpoints read the whole `downtime_logs` table** (`/analytics/executive-oee`, `/analytics/factory-command-center`). 822 ms and 860 ms of handler time at 75,000 rows — one year of a 200-machine plant. Fixed by GROUP BY on the string duration: **46.8× / 50.6×**, arithmetically identical | P2 | fixed, 18 checks, 7/7 mutations red |
| `loadtest.py`: reported raw ms while its docstring promised floor-normalised figures, and **overwrote a 4-scale results file with a 2-scale one**, destroying the 250/1000 evidence. Now reports `xfloor`, merges scales, and states a verdict | P4 | fixed, 22 checks, 6/6 mutations red |
| First clean four-scale HTTP measurement (2026-09-03 18:18 UTC): 10/50/250/1000 machines, **zero errors in 32 endpoint/scale combinations, no regression vs #508 at any scale** | P4 | measured, documented |
| **Corrected my own reading of it**: those p50s are measured under 8 concurrent clients and are pinned to 8/RPS by Little's Law (ratio 1.01–1.09 on the saturated four). Service time is up to 8.5× smaller — 575 ms → **67.6 ms**. Harness now prints both and names the saturated endpoints | P4 | corrected, 29 checks, 4/4 mutations red |

---

## KNOWN P0 / P1

**None open.** The downtime-scan defect below was P2 and is fixed.

---

## VERIFIED DEFECT BACKLOG

| # | Finding | Verified? | Class | Priority |
|---|---|---|---|---|
| 1 | MQTT→WS `asyncio.run(None)` | YES | BUG | **FIXED** |
| 2 | MQTT publishes no `DowntimeStarted` | YES | BUG | **FIXED** |
| 3 | 4 of 7 `SHARE_*` grants have no enforcement point | YES | INTENTIONAL — no read path exists, so nothing leaks. Consent recorded ahead of the feature | not a defect |
| 4 | `oem_claims.accept` sets `status="Assigned"` by bulk UPDATE, bypassing `oem_service.transition` | YES | INTENTIONAL for atomicity — **but the bypass also skipped TERMINALITY.** A scrapped machine could be released to "Sold" and re-claimed by another factory, with no party seeing it had been condemned | **FIXED** — `is_terminal()` asserted in both WHERE clauses |
| 5b | Poll cycle: 135 queries at 10 machines, 135 at 200 — query count FLAT. **But HTTP latency is not:** `/analytics/executive-oee` 73→575 ms and `/machines` 21→184 ms from 10 to 1000 machines. At 1000 the DB answers `list machines` in 3.3 ms while `/machines` takes 184 ms — **98% of the request is above the query**. Per-ROW cost, not N+1 | MEASURED both ways, one clean run | P4 — see below |
| 5 | Dashboard polls `fetchAll` every 3s; 46 requests/round | **MEASURED** via `dashboard_perf.py` **and** `loadtest.py` | PERFORMANCE — the `/machine-health` N+1 was the real query cost (607→10). At ≤250 machines every endpoint is ≤210 ms and error-free | **largest win taken**; the next one is per-row serialisation cost at 1000 machines, not query count |
| 6 | Copilot provider coupling | YES | Already has `AI_PROVIDER` anthropic/gemini branching — if/else, not a clean interface | P6 |

---

## PRODUCT COMPLETION BACKLOG

- ~~`release_installation` sets `status="Sold"` from any state~~ — **VERIFIED AND FIXED.** From `Decommissioned` it resurrected a terminal state and put a scrapped machine back in sellable stock; `usable()`/`accept()` then let another factory claim it. Integrity defect, not a security one (no cross-tenant read). Both paths now guarded; 19 checks, 8/9 mutations red (the survivor documented: a NULL status is non-terminal by two independent routes).
- **`manage_models` / `manage_branding`** capabilities are declared but no route requires them; there is no API to create a machine model (catalogue is seeded). Gap, not a bug.
- **Load testing: `loadtest.py` HAS been run** — at #508 (four scales, to 1000 machines) and again 2026-09-03. Three files claimed it never had; all three are corrected. The **k6** scripts under `load/` genuinely have never run, and they are the ones that would measure the server rather than a Python client. Nothing in CI runs either.
- **Only 3 of 193 backend suites use a real HTTP client**; the rest call route functions directly. Fast and legitimate, but it is not end-to-end API testing and should not be described as such.

---

## AI ROADMAP PHASE

**Phase 1 — provider abstraction: DONE (2026-09-01).**
`ai_copilot.PROVIDERS` is now a registry of `AIProvider` objects
(`AnthropicProvider`, `GeminiProvider`); `_provider`, `_current_model`,
`_ai_enabled` and `_ask_llm` all consult it instead of each branching on the same
two strings. A third provider is one class + one tuple entry, pinned by
`test_ai_provider_registry.py` with a stub provider. **One deliberate behaviour
change:** an `AI_PROVIDER` naming no registered provider now selects nothing and
logs why — it used to fall through to auto-detect, so a typo while moving OFF the
paid tier kept silently billing Anthropic.

Previously recorded state (now superseded):
`ai_copilot.py` already branches on `AI_PROVIDER` (`anthropic` | `gemini`) with per-provider model defaults and a `urllib` call — no SDK. It is if/else branching rather than an `AIProvider` interface, but it is **not** hard-coupled to one vendor. A clean interface is worthwhile; it is not urgent.

**Phase 3 (tools) — the premise is now MEASURED, and it is not what I assumed.**
`ai/assistant.answer` routes by keyword, first match wins. It scores 9/9 on the
evaluation's original questions — which all contain a word it matches literally,
so that mostly proved a dictionary lookup works. On paraphrases a plant manager
would actually type it scored **10/18**, and on a HELD-OUT set written after the
fact, **5/12 (42%)**.

Two things came out of trying to fix it:

* **Vocabulary was the bulk of it.** 5 of 7 tuned misses matched *nothing* —
  "servicing" is not "service", "efficient" is not "effective", "financially",
  "paperwork", "waiting". Those words are now keys.
* **My first fix was a regression, caught only by the held-out set.** Replacing
  first-match-wins with "longest matched key wins" sounded obviously better and
  took held-out from 38% to **23%**. Table order encodes human judgement about
  collisions ("did the late crew hit target?" is a shift question, but
  delivery's `" late"` is a longer key than `"crew"`). Reverted.

So the case for a model-chosen route is now **evidence, not preference**: ~42% of
unseen phrasings route as a person would. `test_ai_evaluation.py` §1c holds that
as a floor. **That set is burned** — it is in the repo, so measuring a real
improvement needs FRESH questions scored BEFORE the router is touched.

Phases 2, 4–6 not started. Note before starting Phase 2: the LLM is already read-only and is handed a pre-built text context (`_build_factory_context`), which is the correct shape — do not rebuild it.

---

## REAL OEM INPUT REQUIRED

- Which of `SHARE_ALARMS` / `SHARE_TELEMETRY` / `SHARE_MAINTENANCE_HISTORY` / `SHARE_DOWNTIME` matters commercially, and what the data must look like. Alarm codes are meaningless without the manufacturer's fault dictionary.
- Whether warranty runs from despatch, delivery, installation or commissioning (`warranty_months` is declared and unused for exactly this reason).
- Any real PLC/controller before touching industrial protocol drivers.

---

## HARNESS DEBT

- ~~`audit_oem_adversarial.py` reports a false BREACH~~ — **FIXED.** My first diagnosis of it was wrong and is corrected here: I recorded that the fixture "was not updated" to grant `SHARE_OPERATING_HOURS`. It does grant it. The real cause is **ordering** — section G ("revocation takes effect on the next request") sets `pol.grants = ""` to prove revocation works and never restores it, so a control 200 lines later ran with consent switched off. Invisible until #522 made a caller-supplied `service_hours` a 403 without that grant. The control now states its own precondition instead of depending on everything above it, and the other half of #522's rule — the same call **refused** without consent — is asserted too, which nothing did before. 138 → 141 checks, no breaches; verified non-vacuous by removing #522's guard.

---

## MANUAL ACTION REQUIRED

- **Production has no MQTT broker.** `FastAPI MQTT connection error: ConnectionRefusedError(111)` on every boot; `MQTT_BROKER` is unset. The live-telemetry path therefore does not run in production today. The bridge fix above is correct but latent until a broker exists.
- `RESEED_FACTORY` is consumed at `274946` and can be deleted from Railway.
- `docs/training/` is **untracked** — 159 KB of handbook that is not in git and would be lost with the working tree.

---

## WHAT IS ACTUALLY OPEN

Everything below was checked against the code before being written here, on
2026-09-03. Four entries that used to sit in this list are gone because they
were done — and one of them had been describing a defect that no longer existed
in terms that were wrong even when it did. A handover nobody can trust without
re-deriving it is worse than none.

1. ~~Row cap on `/machines`~~ — **CLOSED as "leave it alone", with a guard.**
   Investigated properly and the answer flipped from "P4, do it later" to "must
   not be done". The dashboard reduces the machines array into three headline
   KPIs client-side (`app/dashboard/page.tsx:1755-1762`): `running` and
   `breakdown` are `.filter().length`, `avgUtilization` is a mean over
   `machines.length`. A bare `.limit()` would not shorten a visible list — it
   would print an **understated breakdown count** and a mean over an arbitrary
   id-ordered subset, with nothing on screen indicating it. Demonstrated:
   `.limit(500)` against 600 machines reports **0 machines down when 5 are
   broken**. `test_machines_not_capped.py` now fails CI on any `.limit()`, with
   the reason attached. If it ever must change: pagination *and* moving the KPI
   derivation off the truncated array — not a cap.

2. **AI Phase 3 — a model-chosen route.** BLOCKED for measurement, not for
   building: there is no AI key in this environment, so routing quality cannot
   be scored here. The case for it is measured and recorded
   (`test_ai_evaluation.py` §1c): keyword routing answers ~42% of unseen
   phrasings as a person would. **Write fresh held-out questions and score them
   BEFORE touching the router** — §1c is burned, being in the repo.
   The safety shape is fixed and non-negotiable: the model picks a NAME from a
   fixed allowlist, AMP executes it with the caller's tenant, and the model
   never sees or supplies a tenant.

3. **Training-doc drift** — P8, explicitly the lowest. 18 of 20 misleading items
   unsynced (MQTT / events / twin are done).

### Removed from this list, with why

| was | why it is gone |
|---|---|
| `release_installation` lifecycle | **Done (#533).** Verified: the release now preserves `Decommissioned` via a SQL `CASE`, and `oem_claims` refuses a terminal installation in the WHERE clause. |
| AI Phase 1 — `AIProvider` interface | **Done (#526).** Verified: `ai_copilot.PROVIDERS` is a registry. |
| "six tables UNMEASURED at age" | **Measured.** All eight, 455,000 rows: 0 endpoints over 100 ms. |
| "write load not established" | **Measured.** At 122 writes/s — one message per machine per second on a 200-machine plant — reads are within noise of idle. Above ~1,000/s something shows, but the measurement contradicts itself (1 writer at 1000/s → +40%, 4 writers at 1434/s → +10%), so only the realistic-rate result is claimed. |
| `analytics_summary` "scans the whole table on a 3-second poll" | **Wrong twice, and stale.** It is not polled — the frontend never calls it; it backs `/reports/daily-summary.txt`. And the scan was removed in #531. Verified both. |

## CONVENTIONS THAT BIND FUTURE SESSIONS

- Failing test first; confirm it fails for the expected reason.
- Mutation-test every security guard; investigate every surviving mutation.
- eslint baseline is **exactly 134**.
- Schema change ⇒ model + Alembic migration + fresh-schema test + upgrade test + PostgreSQL verification.
- Never weaken a test to make a change pass.
