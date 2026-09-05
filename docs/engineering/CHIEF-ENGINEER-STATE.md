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

- **Production has no MQTT broker — and three things about that were wrong.**
  Investigated properly (2026-09-04):
  - **The error is harmless, not a fault.** It is logged exactly ONCE per boot;
    there is no retry loop and no CPU burn (measured 0.0156 CPU-seconds, thread
    then dead). `/health` does NOT lie — `monitoring.py` already reports
    `ok / not_configured` when `MQTT_BROKER` is unset.
  - **HTTP ingest already works in production.** `POST /iot/telemetry` and
    `POST /industrial/signals` need no broker and are documented honestly as the
    secondary path. BUT they are **not feature-equivalent**: they write no
    `ProductionRecord` (so they feed **no OEE**), no `DowntimeLog`, publish no
    `DowntimeStarted`, trigger no WebSocket broadcast, and do not auto-create
    machines. "We have HTTP, MQTT is optional" understates the gap.
  - **No broker could have been connected to anyway.** Until this session the
    backend had ZERO occurrences of `username_pw_set` / `tls_set` /
    `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_TLS`. Setting `MQTT_BROKER` on
    Railway would not have been enough. That is now fixed and under test.

  **What is still yours to do, in order:** provision a broker (Railway has no
  managed MQTT — either an `eclipse-mosquitto` service in the project, as
  `docker-compose.yml` already uses locally, or HiveMQ/EMQX Cloud); set
  `MQTT_BROKER`, `MQTT_PORT`, and now `MQTT_USERNAME` / `MQTT_PASSWORD` /
  `MQTT_TLS=1`; **configure per-tenant broker ACLs** — the whole multi-tenant
  isolation model rests on the topic's tenant segment being broker-enforced, and
  `check_payload_agrees` only stops the payload contradicting the topic, not a
  publisher choosing another tenant's topic; provision each tenant in AMP first
  (ingest rejects unprovisioned tenants by design); then confirm the `mqtt` block
  in monitoring flips from `not_configured` to `listener_running`.
- `RESEED_FACTORY` is consumed at `274946` and can be deleted from Railway.
- ~~`docs/training/` is untracked — 159 KB~~ — **wrong twice over, and now
  resolved.** 159 KB was the size of `AMP-FOUNDER-TECHNICAL-HANDBOOK.md` ALONE,
  and that file was already tracked (#525) — so the most valuable file was never
  at risk, and one file's size had been generalised to a whole directory while
  inverting its status. `docs/training/` is 768 KB.
  The real problem was different: `build_training_pdf.py` needs SIX markdown
  sources and git held TWO, so nobody who cloned the repo could run its own
  tracked build. All six are tracked now; the rendered PDF/HTML are gitignored
  because the script rebuilds them (and not byte-identically off Windows, so the
  markdown is the artefact of record). Same class: `.coveragerc:42` named
  `reseed_inventory.py`, which was not in the repo. Now it is.
  Also newly ignored: `.claude/` (15 MB of throwaway agent worktrees) and
  `tree.txt` (1.6 MB generated dump) — 17 MB that sat in `git status`
  permanently, one `git add -A` from being committed.
  **Left uncommitted on purpose, because THE REPO IS PUBLIC:**
  `docs/engineering/QA-FRAMEWORK-AUDIT.md`, whose headline finding — that
  `auth.py` ships a fail-open `SECRET_KEY` — has been false since #326, and
  `privacy-policy.html`, which is GMATS Field Service's, a different product.
  Neither contains a secret; both are misplaced. Owner's call.

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

2. **AI Phase 3 — the SAFETY HALF is built and under CI; the model call is not.**
   `assistant.answer(..., chosen_route=NAME)` is the seam: a model may propose
   WHICH pillar answers, AMP looks the name up in a fixed allowlist derived from
   `_ROUTES`, and executes it with the tenant the request already carries.
   Proven in `test_ai_route_allowlist.py` (28 checks): a nonexistent function, a
   PRIVATE function, a real-but-not-routable function, a module name, a dunder,
   a SQL fragment, a path, an empty string, a number, a dict, a list and `True`
   all fall back to keyword routing — none raises, none executes. Tenant
   isolation holds in both directions. 4/5 mutations red; the one that matters
   most catches swapping the allowlist for `getattr`, which would make
   `_machines` reachable. The survivor is documented in the code: dropping the
   `isinstance(str)` check opens nothing, because a dict fails the name
   comparison anyway.
   **NOTHING PASSES `chosen_route` YET, and `/ai/ask` is untouched.** That is
   deliberate: routing quality cannot be measured with no AI key, and shipping
   an unmeasurable behaviour change is what §1c of the evaluation exists to warn
   against.
   **THE BAR IS NO LONGER ~42%, AND THE INSTRUMENT NOW EXISTS (#547).**
   `test_ai_routing_holdout.py` is the fresh held-out set §1c asked for: 52
   questions, four per pillar, written before any was scored, split mechanically
   even → tune / odd → held-out. **It refuses to print which held-out questions
   missed** — only the total — so a held-out miss cannot be fixed by name.
   Current keyword routing: **held-out 15/26 (58%)**, §1c 6/12 (50%). That is
   the number a model router must beat, on that harness, and nothing else counts.
   The first thing it caught was **my own change**: thirteen words of real
   factory vocabulary took the tune half 13 → 22 and left held-out at exactly
   15. Nine fixes, zero generalisation. It also re-runs the historical disaster
   as a mutation — longest-matched-key-wins scores tune **23 (better than
   shipped)** and held-out 14, and is caught *only* by the invisible half.
   Read that as the standing warning: **a rise in a score you can see is
   evidence of nothing.**
   The `/ai/ask` missing-`view` defect noted here is **fixed (#546)** — and it
   was worse than the note said, see below.

3. **OPEN, MEASURED, NOT FIXED: two `has_data` rules, so an unrun week reads as
   0% OEE.** #556 closed the *window* half of the OEE split-brain (twin and the
   contract selected different rows; 21 points apart). This is the other half —
   the same records, two ideas of what "no data" means:

   | case | `oee_contract` | `analytics_engine` |
   |---|---|---|
   | a row that recorded nothing | `has_data=False`, OEE unmeasured | `has_data=True`, **OEE 0%** |
   | unplanned only (a shutdown week) | availability undefined | **OEE 0%** |
   | a normal shift | 58% | 58% (agree) |

   The contract names both as the reason it exists: *"an empty shift rendered as
   a measured 0% OEE"* and *"Reporting 0% for an unscheduled weekend is a
   fabricated loss."*

   **Why it was not fixed in one go, and what the next session needs to know:**
   `has_data` is a PARAMETER of `pooled_oee_from_sums`, so the rule lives at four
   call sites, all of them "a row exists" rather than the contract's "something
   measurable":

   * `analytics_routes.py:112` — `record_count > 0` (`/analytics/summary`)
   * `analytics_routes.py:868` — `bool(production_by_machine)` (`/analytics/executive-oee`)
   * `analytics_engine.py:221` — `len(records) > 0` (`pooled_oee`, feeding twin / cost / losses / recovery)
   * `analytics_engine.py:370` — `record_count > 0` (`build_management_summary`)

   Two ways to close it, and they are not equivalent. (a) Narrow: change the four
   call sites to `planned > 0 or total > 0` and keep the integer return, so
   `has_data` flips only for windows that are entirely unmeasurable — bounded,
   and every surface that already branches on `has_data` then says "no
   production" instead of showing 0%. (b) Wide: migrate callers onto
   `oee_contract` and its `None`-for-undefined return — more honest, but it is an
   interface change (ints to `None`) at every consumer, and whether a given
   screen shows "—" or "0%" is partly a product call. **(a) is the one to do
   first**; it is where the fabricated 0% actually reaches a customer.

4. **Training-doc drift** — P8, explicitly the lowest. 18 of 20 misleading items
   unsynced (MQTT / events / twin are done).

### Removed from this list, with why

| was | why it is gone |
|---|---|
| `release_installation` lifecycle | **Done (#533).** Verified: the release now preserves `Decommissioned` via a SQL `CASE`, and `oem_claims` refuses a terminal installation in the WHERE clause. |
| AI Phase 1 — `AIProvider` interface | **Done (#526).** Verified: `ai_copilot.PROVIDERS` is a registry. |
| "six tables UNMEASURED at age" | **Measured.** All eight, 455,000 rows: 0 endpoints over 100 ms. |
| "write load not established" | **Measured.** At 122 writes/s — one message per machine per second on a 200-machine plant — reads are within noise of idle. Above ~1,000/s something shows, but the measurement contradicts itself (1 writer at 1000/s → +40%, 4 writers at 1434/s → +10%), so only the realistic-rate result is claimed. |
| `analytics_summary` "scans the whole table on a 3-second poll" | **Wrong twice, and stale.** It is not polled — the frontend never calls it; it backs `/reports/daily-summary.txt`. And the scan was removed in #531. Verified both. |
| success/fallback response-shape divergence | **Swept, EMPTY.** #546 was one instance of a class worth checking — a degraded path returning keys the success path does not, which a UI branches on. An AST scan of every route handler with a try/except found exactly two divergences, both the deliberate `note` ("AI model temporarily unavailable"). No other endpoint has it. Do not re-scan. |
| copilot LLM prompt grows with machine count | **Measured, NO ACTION, and the measurement argues against one.** `_build_factory_context` bounds every section (downtime `.limit(8)`, shifts `.limit(5)`, low stock `[:15]`) except machines, which emits one line each. At 600 machines the prompt is 37 KB / ~9,300 tokens and **98% of it is the machine list**. But `claude-haiku-4-5` holds 200k, so it would take ~13,000 machines to threaten the window — and summarising the list changes what the model sees, which is an ANSWER-QUALITY change and unmeasurable with no key. Same trap as §1c. Revisit only when a key exists. |
| `/ai/ask` returns no `view` | **Done (#546), and the note UNDERSTATED it.** There was a second, independent hole: `AICopilot.tsx` kept its own ten-entry `VIEW_LABEL` table while the assistant had grown to thirteen views, so `shifts`, `workorders` and `documents` answers named a real screen and the button was suppressed anyway — on the rules path too, not just with AI on. The label now derives from `NAV_ITEMS`. The obvious backend fix (call `answer()` and keep its view) was **measured and rejected**: it costs 2-6 queries for most routes and **22 for `_briefing`** — the FALLBACK route — which is +116% on this endpoint. `route_view()` resolves it from `@_drills_into` declarations with zero queries; the endpoint's query count is asserted in CI. |

| an `Offline` machine was counted by nothing | **Done (#549).** `Offline` is the fifth `VALID_MACHINE_STATUS` and `normalize_machine_status("offline")` accepts it, but every rollup bucketed four — so the census surfaces published `machines` beside status counts that summed to LESS than it, and the state-summary bars came up short against `total_events`. Buckets now DERIVE from `VALID_MACHINE_STATUSES`. **Still open and deliberately not decided: nothing raises an ALERT for an offline machine.** That is a product call about severity ("we have lost sight of this asset" is not self-evidently Critical or Warning) and wants the founder, not a guess. |
| nullable columns summed in the UI without a NULL guard | **Swept, EMPTY — and the reachability check is the point.** The shape: a column the BACKEND guards for None (`utilization` is averaged over `is not None` so "an unset machine doesn't drag the mean toward 0") while the frontend sums it raw, where JS turns `null` into `0` and still counts the row. Nine columns fit the profile — `utilization`, `actual_quantity`, `dispatched_quantity`, `received_quantity`, `amount`, `confidence`, `x_position`, `y_position`, `height`. **None is reachable as NULL.** All nine have ORM `default=`; all three ingest paths guard (`if util is not None`, `clamped if ... is not None else 0/old`); no Alembic migration adds any of them to an existing table; and no CSV import writes them. So the frontend's missing guards protect against a state the product cannot produce. Do NOT "harden" these nine call sites — it would be nine no-ops. Re-open only if a raw-SQL or restore path starts writing them. |
| `test_ai_copilot_context.py` "is flaky" | **It was never flaky — it was a real bug (#550).** `OeeWindow`'s default end was `datetime.utcnow()` against a filter of `created_at < end`, and a `ProductionRecord`'s `created_at` defaults to `utcnow()` too, so a record written in the same clock tick was excluded from its own window. Fixed by making the DEFAULT end the next representable instant; the half-open rule is untouched (widening to `<=` would break window tiling, and that mutation is now caught). |

## CONVENTIONS THAT BIND FUTURE SESSIONS

- Failing test first; confirm it fails for the expected reason.
- Mutation-test every security guard; investigate every surviving mutation.
- **A test that fails ~35% of the time is a bug with a probability attached, not
  "flaky CI".** #550 was found only because eight consecutive runs of an
  UNMODIFIED tree were measured. Before that, stashing files one at a time and
  reading pass/fail had confidently blamed an unrelated change — every one of
  those readings was a coin flip. If a suite is intermittent, establish the base
  rate on a clean tree BEFORE attributing it to anything.
- **The local-vs-CI split is a clue, not an excuse.** `utcnow()` has ~15.6 ms
  granularity on Windows and microseconds on Linux CI, which is exactly why a
  real defect stayed green in CI for as long as it did. When something fails
  locally and passes in CI, the environment difference is usually pointing AT
  the bug rather than explaining it away.
- **A poller that cannot tell "nothing failed" from "I got no answer" reports
  green forever.** The CI loop used here did
  `bad = [c for c in d.get('check_runs', []) if ...]` and printed GREEN when
  `bad` was empty — so an unauthenticated GitHub **rate-limit** response, whose
  body has no `check_runs` at all, parsed as a clean pass. It announced ALL
  GREEN while the backend job was still running. Any check over a remote
  response must assert the response ARRIVED — a minimum expected count, or an
  explicit error branch — before interpreting its emptiness. The same mistake in
  a health check or an alerting rule is how an outage goes unnoticed.
- eslint baseline is **exactly 134**.
- Schema change ⇒ model + Alembic migration + fresh-schema test + upgrade test + PostgreSQL verification.
- Never weaken a test to make a change pass.
