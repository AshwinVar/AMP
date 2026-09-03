# AMP — Chief Engineer State

> Handover file. A new session should be able to read only this and continue.
> Keep it short. Update it at the end of every completed task.

**Updated:** 2026-09-03
**Master SHA examined:** `ce71a6c` (590 commits)
**Production SHA:** `ce71a6c` (verified after merge — status ok / database ok / schema ok)  
**Previous:** `0eb94ca` — `/health` `{"status":"ok","database":"ok","schema":"ok"}`

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

## NEXT 5 ENGINEERING TASKS

1. **`release_installation` lifecycle** — verify, then either route through `transition()` or document why not.
2. **AI Phase 1** — extract an `AIProvider` interface behind the existing `AI_PROVIDER` branching, no behaviour change, with tests.
3. **AI Phase 3 (tools)** — now measurable: `test_ai_evaluation.py` gives routing/grounding/isolation scores to compare a tool-using copilot against. Start READ-ONLY tools.
4. **Row caps on the fleet endpoints** — measured, and the smallest fix available. Every list endpoint has a hard cap except `/machines` (`machines_routes.py:45`, plain `.all()`), and the cap predicts the growth in exact order: none→8.6×, 500→7.1×, 300→2.7×, 200→1.5×, 100→1.3×. **P4, and smaller than it first looked:** the 575 ms figure for `/analytics/executive-oee` is queueing under 8 concurrent clients; one user waits **67.6 ms** at 1000 machines and **24 ms** at 250.
6. **CLOSED — there is no framework tax.** I first measured handler time against loadtest's **p50** and concluded every endpoint carried ~19 ms of framework overhead. Wrong quantity: p50 is queue-inflated (see the row above). Against **service time**, which is throughput-derived and so has queueing cancelled out, the gap at 10 machines is **0.3 ms mean** and negative for two endpoints. Serialisation is not a cost either (`jsonable_encoder` + `json.dumps` = 0.0–2.6 ms on a 47 KB payload). The seven middleware, JWT auth (no DB access — `auth.py:118`) and session setup are collectively ~1 ms. **The handler IS the request.** Do not go looking for overhead here; there is none to find.
5a. **GUARDED.** `test_growing_table_reads.py` walks the AST of every route module and fails on a new unbounded read of an append-only table. Allowlist of 9, each with a reason, and the allowlist itself is checked for staleness (the `test_date_basis_guard` convention). Proven by reverting #531 and by planting the same defect on a different table — both red.
5a2. **A DATE WINDOW IS NOT A BOUND.** `/machine-health` (1633 ms) and `/analytics/predictive-maintenance` (1297 ms) both go through `ai.prediction.assess_from_db`, which read three tables `.filter(created_at >= cutoff)` over 30 days. At 200 machines that window held 63,036 downtime rows and 43,198 machine events — **~106,000 ORM objects per 3-second poll**, of which only 76 ms was SQL. Reduced to per-machine counters in SQL: **1297→21.8 ms** and **1633→261.8 ms**. The #532 guard treats a `created_at` filter as bounded, which it is only when divided by the row rate.
5a3. **OPEN — my #532 guard exempts column projections, and that hides 16 sites.** The worst is `ai/twin.py:69` (`_downtime_by_machine`), which I wrote in #525 to fix an N+1: it reads **every** downtime row (3 columns, no limit) to keep the top 3 per machine. That is the remaining 261 ms of `/machine-health`. I exempted projections because "the row never becomes an ORM object" — but 75,000 projected rows are still 75,000 rows. SQLite here is 3.38, so a `row_number()` window function is portable. NEXT TASK.
5b. **The harness blind spot this exposed.** Every perf harness seeds rows PER MACHINE, so no table that grows with TIME is covered. `downtime_logs` held a 50× defect that all of them missed. `machine_events`, `audit_logs`, `agent_actions`, `production_records`, `inventory_transactions`, `quality_inspections` grow the same way and are UNMEASURED at age. `test_downtime_scan_bounded.py` shows the technique — seed rows independently of machine count and assert SQL shape, not wall time.
5c. **DONE — `/analytics/management` no longer scans.** `build_management_summary` gained `downtime_agg`, mirroring the `production_sums`/`shift_sums` entry points it already had. 818.5 ms → 16.2 ms at 75,000 rows, byte-identical output. **No route hydrates a time-growing table unbounded any more**, and the guard fails the build if one appears.
7. **`analytics_routes.analytics_summary` does the work of three endpoints.** It calls `generate_alerts()` and `oee_summary()` internally, so the machines table is read 3× per request, and the dashboard polls `/alerts` and `/oee/summary` separately as well. Also `logs = db.query(models.DowntimeLog).all()` (`analytics_routes.py:53`) scans the whole table on a 3-second poll — the rule-4 antipattern retired on its siblings, which survived here because `DowntimeLog.duration` is a *string* (`"15 min"`) and cannot be `SUM`ed in SQL.
5. **Sync remaining training-doc drift** (18 of 20 misleading items still unsynced; MQTT/events/twin sections are done).

---

## CONVENTIONS THAT BIND FUTURE SESSIONS

- Failing test first; confirm it fails for the expected reason.
- Mutation-test every security guard; investigate every surviving mutation.
- eslint baseline is **exactly 134**.
- Schema change ⇒ model + Alembic migration + fresh-schema test + upgrade test + PostgreSQL verification.
- Never weaken a test to make a change pass.
