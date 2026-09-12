# AMP — Chief Engineer State

> Handover file. A new session should be able to read only this and continue.
> Keep it short. Update it at the end of every completed task.

**Updated:** 2026-09-07 (security closure, the vocabulary line, the census
line, and the routing measurement: #560–#572)
**Master SHA:** `8d1b5a6` (#571)
**Production SHA:** `8d1b5a6` — verified live, not assumed:
`{"status":"ok","database":"ok","schema":"ok","version":"8d1b5a6"}` from
`https://flowmes-production.up.railway.app/health`, read at 02:04 UTC.
Master and production are in step. Railway auto-deploys master, so prod tracks
HEAD; re-check `/health` rather than trusting this line's age.

> This header was twenty PRs stale when it was found (`040d30a`/#539 while master
> was `208f564`/#559). A handover whose own first three lines are wrong teaches a
> new session not to trust the rest of it. **Update these three lines whenever you
> merge**, and take the production SHA from `/health` rather than assuming the
> deploy landed.

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

### 2026-09-05 — the correctness line (#542–#559)

One thread, pulled repeatedly: **the same fact defined in two places.**

| Task | Priority | Status |
|---|---|---|
| **Two definitions of "the last 7 days".** `oee_contract` used `[now-7d, now)`; `ai/twin._recent_production` used `midnight(today-6)` with **no upper bound**, feeding oee/cost/losses/recovery. Same plant, same instant: **66% vs 87%**. `build_oee_summary` used BOTH, so `coverage` — the figure whose job is "measured from N of M machines" — described a different record set than the number it qualified: *"OEE 85%, measured from 0 of 1 machines"* | P1 | fixed #556, 4/4 mutations red |
| **`has_data` meant "a row exists" at four call sites**, against the contract's "something measurable" — so a week the plant did not run rendered as a measured 0%. First attempt was a **no-op at three of the four** (they publish a bare integer and no flag); they now publish `has_data` | P2 | fixed #558, 6/6 mutations red |
| **`Offline` was a valid machine status that no rollup counted.** Census endpoints published `machines` beside status counts that summed short. #552 fixes the per-zone rollup **#549 missed in the same file** | P2 | fixed #549 + #552, 7/7 mutations red |
| **A record written in the same clock tick as the query was excluded from its own window.** `OeeWindow` end was `utcnow()` against `created_at < end`. ~15.6 ms Windows granularity made it a 35% local failure; microsecond Linux CI never saw it | P2 | fixed #550, 6/6 mutations red |
| **A machine hard-down on day one raised nothing** — no alert, no escalation — because the plant had not produced yet. Every tenant starts there | P2 | fixed #554, 5/5 mutations red |
| **Turning the AI copilot ON removed the drill-in button**; separately, `AICopilot.tsx` kept a ten-entry label table while the assistant had thirteen views | P3 | fixed #546, 10/10 mutations red |
| **`DOWN_STATUSES` defined twice**, three consumers across the two copies | P5 | fixed #553, 5/5 mutations red |
| MQTT boot: no broker could have been connected to, and one refusal killed ingest | P2 | fixed #543 |
| **A held-out routing set whose held-out half is unprintable.** Keyword routing is **58%**, not the 42% recorded. First thing it caught was my own vocabulary pass: visible half 13→22, invisible half **15→15** | P4 | added #547 |
| Row cap on `/machines` — investigated and **closed as "must not be done"**, with a CI guard and the reason attached | P4 | #542 |

### 2026-09-07 — the vocabulary line (#560–#566)

The same thread, one turn further: **a status word meaning different things in
different files.** Every one of these was a state somebody had already decided
about — withdrawn, cancelled, in progress — that one surface honoured and
another did not.

| Task | Priority | Status |
|---|---|---|
| **A working client credential in a PUBLIC repo**, seeded on startup, logged, and the default in a script aimed at production | P0 | source closed #561, class closed #564 — **rotation still open, see below** |
| **Rejecting an agent's maintenance task made the agent escalate it.** "Overdue" had two definitions; `!= "Completed"` is true of a task a human declined, so the decision was converted into a High/Critical alert | P1 | fixed #562, 9/9 mutations red |
| **Withdrawing an escalation silenced that alert forever.** Five spellings of "open escalation" across eight sites. `from-smart-alerts` dedups on `!= "Resolved"`, and `"Cancelled"` satisfies it — so one supervisor withdrawing one duplicate meant that machine could never raise that alert again | P1 | fixed #565, 13/13 mutations red |
| **An agent re-proposed work a technician was already doing** — the dedup whitelist had no `"In Progress"`, which is exactly what the UI writes when someone picks the job up | P1 | fixed #565 |
| **Cancelling a purchase order marked the SUPPLIER down for it.** A withdrawn PO past its date read as `late`, and `late` is the reliability denominator, the worst-suppliers sort key, and the chase-list test | P2 | merged #566, 10/10 mutations red |
| **`tree_guard.py` shipped untested, and turned CI's coverage job red.** Testing it found a real defect in it: every DELETED file was also listed as MODIFIED, so the `deleted` branch was dead code | P3 | fixed #564, 9/9 mutations red |
| **23 suites are invisible to pytest**, so what they prove counts as untested. This is what made a well-tested new module lower the coverage floor | P4 | 3 fixed, rest recorded |

### 2026-09-12 — the same thread, now on the screen (#581)

The vocabulary line reached the UI. A `<select>` is a **whitelist of the values
it can display**, and every one of them had drifted from what the backend
writes. A controlled select bound to a value its option list does not contain
renders an **empty box** — no React warning, no error, nothing in a log.

| Task | Priority | Status |
|---|---|---|
| **34% of every seeded row rendered a blank status dropdown** across six screens. Measured, not guessed: seed with `factory_simulator.seed_all`, then count rows whose stored value is absent from the list its own screen offers — **42 of 123**, including **12 of 12 quality inspections**, because nothing in AMP has ever written the four statuses that screen offered (and `statusStyle` coloured only those four, so every row also drew the same yellow pill — the column carried no information at all) | P2 | fixed #581, 7/8 mutations red |
| **The blank box was a live way to break the agent approval chain by looking at it.** `ai/agents.approve_action` transitions only `if item.status == "Proposed"`, and no list offered `"Proposed"` — so an operator clicking the empty box to find out what it was overwrote it, and approving *or* rejecting that task afterwards did nothing, permanently | P1 | fixed #581 |
| The vocabulary now lives once, in `frontend/lib/status-vocab.json`, read by **both** sides: TypeScript renders from it, `backend/test_status_vocabulary_parity.py` walks the backend AST and fails if a value it writes is missing from it | P2 | added, 5 sections |

### 2026-09-12 — AMP was inventing readings for real PLCs (#582)

Found while answering a founder question about connecting a customer's
compressors in India. Not a reporting defect — a **fabrication** defect.

| Task | Priority | Status |
|---|---|---|
| **Registering a real PLC produced invented readings within one tick.** `tick_industrial` advanced every device whose status was `Online`, and `IndustrialDeviceCreate.status` defaulted to `"Online"` — so a device added for a real compressor at a real IP got `random.randint()` pressure/temperature values stamped `quality="Good"` and tagged with its real protocol, over a protocol AMP cannot speak. Reproduced: **60 fabricated readings attributed to `COMP-01` at `192.168.10.22:502`** | **P0** | fixed #582, 10/10 mutations red |
| The screen was the other half of it: `status: "Online"` posted from the browser, a green "connected" badge, *"signals will start flowing"*, "Awaiting first poll…", and the invented numbers in the same green as everything else. The only disclaimer sat under the protocol grid, nowhere near the number | P1 | fixed #582, 7 render tests |
| `GET /industrial/protocols` described itself as *"the connectivity surface AMP speaks"*. It is a catalogue of what an on-site **edge agent** would install — `library` names the package, none of which is a dependency | P2 | fixed #582 |
| **The repo had already written the rule down and applied it one case too narrowly.** `test_adapter_resilience` guards a device *known to be down* — *"would fabricate live signals ... and make the connectivity dashboard claim a dead device is reporting"*. A device never contacted at all is the same lie; only the first was guarded | P1 | second filter added to that suite too |

---

## KNOWN P0 / P1

**ONE OPEN, AND IT NEEDS YOU, NOT CODE. THE ROTATION HAS NOT TAKEN EFFECT.**

A working Supervisor credential for the GMATS tenant was hardcoded in
`main.py`, seeded on every startup, written to the application log, and
repeated as a default in `e2e_sim.py` whose `AMP_URL` defaulted to production.
The repository is PUBLIC.

#561 removed it from source and added a guard, but **that does not undo the
exposure**. It was reported rotated; it is not. Verified against production
before anything else, in a run whose controls prove the test is real:

| probe | result |
|---|---|
| the exposed password | **HTTP 200, token issued, `role=Admin`** |
| a never-valid password for the same user | 401 "Invalid password" |
| an unknown username | 401 "Invalid username" |

The token was discarded unused and neither the old nor any new credential was
printed, logged or committed. The cause is the one #561 documented: the seed's
`if not exists` guard makes `GMATS_PASSWORD` a **no-op** where the user
already exists, so setting the env var changes nothing in the live database.
Rotation has to happen IN THE DATABASE — reset the `gmats` user's password
directly (or delete the row and let the env-driven seed recreate it).

Also for you to decide: the account answered `role=Admin`, while the seed
creates it as `Supervisor`. Somebody promoted it, or it predates the seed.
Confirm which role that login is supposed to hold before rotating.

Items 2 and 3 of the closure are BLOCKED on the same thing: verifying the new
credential works only through the intended mechanism, and reviewing production
audit logs for use during the exposure window, both require authenticating as
that account, which was declined. Note for whoever does it: **failed logins
are not audited at all** (`core_routes.py` calls `log_audit` only on success
and on the two blocked-tenant paths), so the audit trail cannot show attempts,
only successful sign-ins.

Items 4–8 are CLOSED and merged (#561, #564): no production credential
literals in source, tests or the eight startup-reachable seeders (AST-checked);
no credential written to a log line; `e2e_sim` defaults to localhost with no
default password and refuses a remote host without `AMP_ALLOW_REMOTE`
(asserted behaviourally against RFC 5737 TEST-NET-1, because the first
substring-based version survived a `if False:` mutation); `tree_guard.py` +
worktree isolation for read-only investigators; `.githooks/pre-commit`
refusing commits on master/main.

The Admin account three lines below had always done it correctly
(`GMATS_ADMIN_PASSWORD` from env, "password never hardcoded"), so the standard
existed and this was the exception to it.

The downtime-scan defect below was P2 and is fixed.

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
| 7 | Agents' dedup whitelisted `("Proposed","Open")`, so a technician moving an item to "In Progress" made the agent propose it AGAIN | YES | BUG | **FIXED #565** |
| 8 | "Open escalation" had five spellings across eight sites. A **withdrawn** escalation gagged its own alert permanently (`from-smart-alerts` dedup matched it), inflated two dashboard cards, and told operators it "still requires action" | YES | BUG | **FIXED #565** |
| 9 | `ai/supply.py` classified a **Cancelled** PO as `late`, so withdrawing an order lowered that supplier's reliability score, pushed them up the worst-suppliers sort, and put the PO on the chase list | YES | BUG | **FIXED #566** |
| 10 | `/analytics/operator-terminal` publishes `total_jobs` beside `started`/`paused`/`completed`, but the simulator writes `"In Progress"` (`factory_simulator.py:529,1004`) and the column defaults to `"Started"` and is nullable — so the parts do not sum to the total on a live plant | YES | CENSUS GAP | **FIXED #568** |
| 10b | **`/analytics/purchasing` buckets a PO vocabulary nothing writes.** It publishes `purchase_orders` (the total) beside `open`/`partial`/`received`/`cancelled`, but `factory_simulator.py:404` writes `["Open","Open","Partially Received","Received","Overdue"]` and `ai/agents.py:262` writes `"Draft"` (`"Approved"` after the human accepts, per `_draft_po_exists`). So **`partial` reads 0 while a fifth of the book is partially received**, and Overdue/Draft/Approved have no bucket at all — the card's parts fall short of its own total by however many the Reorder agent has proposed. Same PR should settle whether an unapproved `Draft` belongs in `ai/supply.py`'s `late` bucket and therefore in `reliability_rate` (it currently does, once seven days old — nobody sent it, so it is not the supplier's failure) | YES | CENSUS GAP | **FIXED #569** |
| 11 | **23 backend suites are invisible to pytest**, so everything they prove counts as untested in the `coverage` job. pytest collects module-level `test_*` functions and nothing else; these expose only `main()`. That is how adding a well-tested module (`tree_guard.py`) pushed the floor DOWN and turned #564 red. Three-line entry points added to 3 of them (#565, #564); the rest are listed by `grep -rL "^def test_" backend/test_*.py` | YES — measured | MEASUREMENT GAP | **FIXED #571.** All 24 given an entry point: coverage 78.23% → **79.98%**, 1174 → 1200 tests collected. 1.75 points of already-written testing had been counted as untested |

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

**Phase 3 — the plateau is now REPLICATED, on a second set (#570).**
`test_ai_routing_holdout2.py` is a fresh 52, written to the same protocol: the
pillar list came from `route_names()` at runtime, the keyword table was NOT
opened while the questions were written, the split is mechanical, and the
held-out half prints only a total. Scored once, before anything was touched:

|                    | tune       | held out   | gap |
|---|---|---|---|
| set one (tuned against) | 22/26 85% | 15/26 58% | +27 |
| set two (fresh)         | 15/26 58% | 15/26 58% |  +0 |

Two sets written by different processes agree on the held-out number, **58%**.
That corrects the 42% quoted below and in §1c, which now reports 6/12 (50%) on
its own set after the vocabulary work.

**This did not discover the plateau — #547 already recorded it** ("thirteen
words of real factory vocabulary took the tune half 13 → 22 and left held-out
at exactly 15"). What set two adds is INDEPENDENT REPLICATION: a set that has
never been tuned against scores exactly what the tuned set's invisible half
scores. One set showing no movement could be a hard sample; two sets agreeing
is a property of the router.

Read against the code's own advice at `ai/assistant.py` — *"Prefer adding a
deliberate multi-word key over changing how the winner is chosen"* — the two
levers are now both measured and both spent: adding keys does not generalise,
and changing the selection rule regressed held-out 38% → 23% when it was tried.
Counting matched keys instead of first-match-wins was considered and rejected on
inspection rather than tried: it would still pick `machines` for "how well are
the machines actually running?" and `trend` for "how did nights do compared to
days?", because table order is the encoded judgement and a count does not break
those ties.

**So Phase 3 is BLOCKED on an AI key, and only on that.** The safety half is
built and tested (28 checks, hostile inputs, tenant isolation both ways); the
instrument to judge a model router exists and has a recorded baseline. What is
missing is a key to measure a model against 58%.

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

- **ROTATE THE GMATS PASSWORD IN THE DATABASE. It is still live.** Verified by
  probe against production on 2026-09-07: the exposed credential authenticates,
  returns a token and reports `role=Admin`, with a never-valid password and an
  unknown username both correctly refused in the same run. Setting
  `GMATS_PASSWORD` cannot do it — the seed's `if not exists` guard makes it a
  no-op for an existing user. Reset the row's password directly, or delete the
  user and let the env-driven seed recreate it. While you are there, confirm
  whether that login should hold `Admin` (what it answers) or `Supervisor`
  (what the seed creates). See KNOWN P0 / P1 for the full table.
- **Git history still contains the credential.** Not rewritten, deliberately —
  history rewriting on a public repo is your call, not an autonomous one, and
  it does not help anyway once a secret has been published. Rotation is the
  fix; a history rewrite is optional tidying afterwards.

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

3. **DONE (#558), with a product decision left for you: two `has_data` rules,
   so an unrun week read as 0% OEE.** #556 closed the *window* half of the OEE split-brain (twin and the
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

   **(a) IS DONE (#558).** The rule has one home — `oee_contract.is_measurable`
   — and all four sites use it. Two things learned doing it, both worth keeping:

   * The first attempt was a **no-op at three of the four sites**, and only the
     mutations said so. Those endpoints publish a bare integer and no
     `has_data`, so `avg_oee` is 0 whether the plant scored zero or never ran —
     the flag they computed went in the bin. They now **publish `has_data`**,
     which is what makes the fix reach a caller at all.
   * `mutate_oee_contract.py`'s "has_data goes back to 'a row exists'" pattern
     matched the expression that moved, so it **silently stopped applying**. A
     mutation that cannot be applied is a guard switched off; that harness
     reports it as a survivor, which is the only reason it was noticed.

   **CLOSED (#572), and it was NOT a product call after all.** The API already
   distinguished the two cases, so a screen printing a number the API says is
   unmeasured is a defect, not a choice — the only genuine judgement was the
   wording. `ExecutiveOeeSection` now renders three states: `—` when no payload
   has arrived, `Not run` when there was nothing to measure, and the figure
   otherwise, INCLUDING a real 0% which keeps its red border. `has_data` was not
   even declared on the `ExecutiveOee` type, so no consumer could have asked.
   Mutation testing earned two of the seven checks: reverting `highlight` left
   "Not run" inside a red catastrophe card and passed every text assertion.
   Say the word if you want different copy.

   The original note, kept for the reasoning:
   the frontend still renders `0%` for an unrun week, because nothing consumes
   the new `has_data` flag yet. The API can now tell the two apart; the screen
   cannot. Deciding whether that card shows "—", "not run", or "0%" is Ashwin's,
   not mine — and option (b), migrating every consumer onto the contract's
   `None`-for-undefined return, is the larger interface change that only makes
   sense once that rendering decision exists.

4. **CLOSED — all five defects from the 2026-09-06 fan-out are shipped.**
   #565 (open-escalation, five spellings; and the agents' dedup whitelist),
   #566 (cancelled POs), #568 (operator jobs), #569 (purchasing census). Three
   of them turned out to be the SAME shape and each was found while verifying
   the one before it: a total published beside a breakdown that does not
   partition its own vocabulary. The original text is kept below because the
   reproductions in it are what made each one cheap to fix.

   **OPEN — four verified defects from the 2026-09-06 fan-out, all the same
   shape.** A 14-agent hunt across eight defect classes produced 29 raw
   findings; six survived adversarial verification (each verifier had to
   reproduce the failure itself, and none was refuted). Two are shipped (#562
   maintenance-overdue; the credential one was found separately, #561). These
   four are reproduced, unfixed, and each is *one rule with more than one
   implementation* — the pattern this whole line of work has been mining:

   * **`ai/supply.py` is the only PO reader that does not exclude `Cancelled`.**
     A cancelled purchase order is reported as "late", enters the chase list,
     keeps its never-to-arrive units in the receipt-rate denominator, and drives
     supplier reliability to 0%. `/supply-summary` says 1 late and 0%
     reliability where `/supplier-performance` says 0 overdue — **both cards are
     on the same dashboard**. `ai/delivery.py` (the customer-order mirror)
     already carries this exact fix with the failure documented verbatim in its
     docstring; `ai/supply.py` is the same code path, unfixed. Also
     `orders_routes.py:690` and `:750` both exclude it. Reproduced.
   * **`/analytics/purchasing` counts 4 of the 7 statuses the app writes.** The
     `partial` bucket reads `status_counts.get("Partial")` while
     `factory_simulator._purchase_orders` writes `"Partially Received"` — two
     spellings of one state — and the Reorder agent writes `"Draft"`, which no
     bucket has. Seeded book of 10 POs: total 10, buckets sum to 5.
   * **`/analytics/operator-terminal` has the `"Started"` vs `"In Progress"`
     synonym split** that was fixed in its four sibling rollups and missed here
     (`analytics_routes.py:1499`). Every live job is created `"In Progress"`
     (`tick_operator`, `_operator_jobs`, `e2e_sim`), so `started` is always 0
     while `total_jobs` counts them. 8 jobs -> total 8, buckets sum to 6.
   * **"Open escalation" has three implementations giving three counts** for the
     same four rows: 3 (`/analytics/factory-command-center`, `/system-health`),
     1 (`/analytics/escalations`), 2 (the read-model and `ai/handover`). The
     withdrawn (`"Cancelled"`) escalation — again what a human rejection writes —
     is counted as open backlog forever by the analytics endpoints.

   Also found and fixed en route: the **agents' dedup whitelist** is
   `("Proposed","Open")`, so moving an agent's task or escalation to
   `"In Progress"` makes the agent propose a **duplicate while a technician is
   working the first one** (`ai/agents.py:171` and `:279`). Not yet fixed.

5. **Training-doc drift** — P8, explicitly the lowest. 18 of 20 misleading items
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
- **A new suite MUST expose a module-level `test_*` function.** CI's per-file
  runner (`python test_X.py`) finds `main()`; the `coverage` job runs pytest,
  and pytest collects module-level `test_*` functions and NOTHING else. A suite
  that only has `main()` therefore proves nothing to the coverage measurement,
  and adding a well-tested module to the repo pushes the floor DOWN — which is
  what turned #564 red. Three lines are enough:
  `def test_x(): assert main() == 0`. Check with
  `python -m pytest --collect-only -q <file>`; zero collected is the bug.
- **If the scenarios ARE module-level `test_*` functions, they must raise.**
  The `check()` helper used across this repo RECORDS a failure and returns —
  under pytest that reports green. Either keep the scenarios private
  (`_check_*`) behind one asserting entry point, or assert inside each.
- **Mutation harnesses must read AND write with `newline=""`.** These files are
  CRLF. Reading in text mode without it converts to LF, so the "restore" rewrites
  every line ending and `git status` shows a file the harness swears it did not
  touch. Multi-line find-patterns written with `\n` simply never match — and a
  pattern that does not match is a DISABLED mutation, which measures nothing
  while looking identical to a guard that works. Report those as survivors.
- **Escape sequences do not survive Bash heredoc → Python → file.** Use the Edit
  tool for exact strings. This bit twice in one session: an em dash in a
  mutation pattern, and `"\\0"` becoming a NUL byte.
- **Do NOT raise `--cov-fail-under` to sit just under the current number.**
  Considered after #571 lifted coverage to 79.98% and correctly abandoned:
  docs/TESTING.md sizes the gap at THREE points below current, on a measured
  spread of 79.2–81.2% across one afternoon of ordinary churn, and says why —
  *"a check that fails on noise gets deleted"*. Current 79.98% implies a floor
  near 77, so **78 is already tighter than the documented rule** and raising it
  would make CI red on noise. Related and worth knowing: the tree has grown from
  11,025 statements at 81.2% (2026-08-04) to 14,133 at 79.98%, so coverage has
  drifted down ~1.2 points while the codebase grew 28%. That is the number to
  watch, not the floor.
- **Complement, not whitelist — and that includes UI controls.** A `<select>` is
  a whitelist of what it can *display*, so a value nobody listed renders as an
  empty box rather than as an error. `statusOptions()` appends the row's current
  value when the list lacks it: an unknown value degrades to *showing the truth*
  instead of to *showing nothing*. Same rule as the SQL side — an unrecognised
  word must default to the safe direction, never vanish.
- eslint baseline is **exactly 133** (was 134 until #582). The one that went was
  `react/no-unescaped-entities` on `IndustrialConnectivity.tsx` — the old header
  said "AMP's adapter layer", and the sentence that replaced it has no
  apostrophe. Incidental, not a cleanup; the number is a ceiling for NEW
  problems, so it moves down with the code and never back up.
- Schema change ⇒ model + Alembic migration + fresh-schema test + upgrade test + PostgreSQL verification.
- Never weaken a test to make a change pass.
