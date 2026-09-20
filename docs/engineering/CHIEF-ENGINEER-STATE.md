# AMP — Chief Engineer State

> Handover file. A new session should be able to read only this and continue.
> Keep it short. Update it at the end of every completed task.

**Updated:** 2026-09-20. The 10-day differentiation sprint is running, and
**fifteen of its sixteen differentiators are built**. Merged: #645 (ADR-0022,
typed authorized tools + evidence + grounding + the evaluation), #646
(ADR-0023, the self-hosted provider behind an earned switch), #647 (ADR-0024,
the Command Centre), #648 (ADR-0025, the Root-Cause Explorer), #649 (ADR-0026,
the Risk Radar), #650 (ADR-0027, the explained health score and the
failure-risk model's first screen), #651 (ADR-0028, the Daily Factory Brief),
#652 (ADR-0029, the closed loop — the sprint's first schema change, migration
`0011`), #653 (ADR-0030, the shortage-to-production link), #654 (ADR-0031,
proactive restraint), #655 (ADR-0032, the fleet anomaly sweep), #656 (ADR-0033,
the OEM disclosure floor), #657 (the owner's eight questions across four
factory shapes), #659 (ADR-0033 §7, the complementary-suppression floor), #660
(the grounding gate no longer reads an identifier's or a date's digits as
figures) and #661 (ADR-0034, the first REAL self-hosted model: four candidates
measured, **`qwen3:8b` promoted** in `ai/adopted_models.json`, AMP proven to
run with no hosted key at all), #663 (every intelligence engine classified as
RULE-BASED / STATISTICAL / ML MODEL / LLM-ASSISTED in the handbook, with
`test_intelligence_classification.py` failing the build if the handbook and the
model cards disagree) and #662 (the three-factory SIMULATION audit in CI, and
the simulator's tenant guard: a tick with no tenant bound now refuses instead
of filing every tenant's activity under DEFAULT — ADR-0002 postmortem). See
AMP-10-DAY-SPRINT.md, AMP-NATIVE-MODEL-ACCEPTANCE.md and, for the twelve
acceptance items with their evidence, AMP-SPRINT-ACCEPTANCE.md.
**Master SHA:** `5e8b863` (#662).
**Production SHA:** `5e8b863`, verified live, not assumed:
`{"status":"ok","database":"ok","schema":"ok","version":"5e8b863"}` from
`https://flowmes-production.up.railway.app/health`, read 3 s after the deploy
on 2026-09-20; `/readiness` 200. The frontend (`https://flow-mes.vercel.app`)
answers 200; `/ai/status` and `/platform/status` each refuse an unauthenticated
call with 401. Production has no GPU and no `AMP_LLM_BASE_URL`, so the Copilot
there answers from AMP's own engine; the promoted model runs on the founder's
laptop (Ollama on loopback, see the acceptance report §1).
Railway auto-deploys master, so prod tracks HEAD; re-check `/health` rather than
trusting this line's age.
**Sprint closed on Day 10 with a release candidate.** `AMP-SPRINT-ACCEPTANCE.md`
records the founder's twelve final-acceptance items — three-factory
simulation, OEM simulation, owner journey, Copilot journey, AI adversarial
testing, tenant isolation, OEM consent, PostgreSQL, performance, recovery,
model-outage test, production smoke test — each with the script that proves
it, where it runs, the result measured on 2026-09-20 and what it does *not*
prove. All twelve are green; §3 of that record lists what is not proven (the
failure-risk model on real failures, a model in production, k6 baselines, a
production-sized restore). The load driver was re-run the same day and
`backend/loadtest_results.json` is today's baseline (`docs/PERFORMANCE.md`
"Re-run 2026-09-20" explains why its own floor-normalised verdict reads
"regressed" at 50 machines while every raw p50 fell).

**Nothing is awaiting review.** What a next session would do first, in order:
(1) the OEM journey re-check against a real OEM's edge agent is still simulated
(handbook ch. 31); (2) row 7 of the differentiator table (financial loss
intelligence) is PARTIAL by design — money only where a unit value is set —
and stays so until a customer sets one; (3) a production model needs an
infrastructure decision the founder owns (a GPU or a hosted OpenAI-compatible
endpoint, then `AMP_LLM_BASE_URL`/`AMP_LLM_MODEL`; the promotion record does
not need re-earning unless the model, runtime or question set changes).

**The one differentiator that is NOT built is #2's model.** The provider, the
earned switch and the evaluation all exist and are tested end to end over HTTP
against stubs. **No real model has ever been measured**, because downloading
the runtime and the weights needs the founder's permission and that has been
open since 2026-09-19. Local inference does not become the default until a real
model passes the gate: zero unauthorized disclosures, zero ungrounded answers
shown, and tool selection at least equal to AMP's own engine.

**The current benchmark is AMP's own engine** (`python -m copilot_eval`, read
2026-09-20): 135 questions plus 144 adversarial prompts across three factories
and three roles — tool selection 69/69 core and 60/66 unseen, factual accuracy
93/93, answers grounded 279/279, honest data states 14/14, **0 money
fabrications and 0 unauthorized disclosures**, p50 4 ms.

**#657 is worth reading before adding a tenth surface.** It asks the owner's
eight questions across four factory shapes in one process, and it exists for
the rules that only hold BETWEEN surfaces — money appearing nowhere across nine
screens for an unpriced factory, and no factory's words on another's screen
when four tenants share one database. It found two defects on its first run,
both of which every per-surface suite had passed.

**THE 10-DAY DIFFERENTIATION SPRINT started 2026-09-19.** Its own tracker is
[`AMP-10-DAY-SPRINT.md`](AMP-10-DAY-SPRINT.md): day, SHAs, the 16
differentiators with status, the AI benchmark, what is blocked and why. Resume
with: "Resume AMP 10-Day Autonomous Differentiation Sprint from
CHIEF-ENGINEER-STATE.md and AMP-10-DAY-SPRINT.md."

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

### 2026-09-12 — the deferred risk factor, now measured (#583)

| Task | Priority | Status |
|---|---|---|
| **A published risk factor that could only ever score zero.** `predictive_engine.ACTIVE_WORK_ORDER_STATUSES = ("Running", "Delayed")` — AMP writes neither word. Intersection with a seeded plant's vocabulary (Planned / In Progress / Completed / On Hold): **EMPTY**. So work-order pressure was 0 for every machine and *"high active work-order load"* had never appeared as a reason, ever | P2 | fixed #583, 8/8 mutations red |
| **Measured before and after, which is why it waited.** #579 built `work_order_status.py` for this family, fixed the command centre, and named this line as the outstanding caller — deferred deliberately because it moves published scores. It lifts **3 of 8** machines by +10 (pressure 1478 / 1234 / 639 against a 500 threshold) and moves **no risk band**: all three sit deep inside Low, and the one High machine has no open orders | P2 | measured, recorded in the suite |
| The SQL loader (`ai/prediction`) and the Python re-filter are now pinned against each other row-for-row over 11 statuses incl. NULL, `""` and `"  Completed  "` — two implementations of "open" was the defect | P2 | 22 checks |

### 2026-09-12 — "this week vs last week" compared a week against itself (#584)

A 60-agent window audit (3 adversarial verifiers per finding) turned up **17
raw findings** across the analytics read-models, collapsing to **one root
cause**: a headline built from the canonical ROLLING window
(`OeeWindow(7)` = `[utcnow-7d, utcnow)`, which touches EIGHT calendar dates)
compared against, or bucketed into, a MIDNIGHT-anchored calendar window of
seven. This PR closes the worst face of it; the rest are listed below.

| Task | Priority | Status |
|---|---|---|
| **The scorecard's "Plant OEE" arrow and the recovery card's improving/worsening badge compared a week against itself.** Both hand-rolled "last week" as `[midnight(today-13), midnight(today-6))` against a rolling current window, overlapping it by `24h - (time since midnight)` — ~10h at 14:00 UTC, a **full 24h at midnight**. A plant whose only run was on the boundary day published a confident green *"OEE improved N points week on week"* when there was no prior week at all, and the same data gave a **different delta depending on the hour the dashboard was opened** | P1 | fixed #584, 7/7 mutations red |
| `oee_contract` already documented the mechanism — *"an explicit `now=` — what a caller passes to build adjacent windows — is used verbatim"*. It now exposes `prior_window(current)`, and one anchor is threaded through the request so `prior.end is current.start` exactly | P1 | 26 checks |

**All window findings are now closed (#584, #586, #587, #589, #590, #591), and the last recorded follow-up with them (#592).** The audit is fully worked through.

### 2026-09-17/18 — the queue cleared, and what the queue did not know (#596–#605)

Ten merges, each with a failing test first, mutation-tested, and **verified live
on production** (`/health` version read back) rather than assumed. In order:

| PR | What was actually wrong | Evidence it is fixed |
|---|---|---|
| #596 | The Purchasing list 500'd once the reorder agent had fired (`supplier_id` not nullable in the response model) | 2 suites, frontend test |
| #597 | An idle machine topped the OEE ranking on an invented 68% | contract-driven rows, 11/11 mutations |
| #598 | A founder previewing a customer imported their CSV into the **wrong workspace** | `getDownloadHeaders` at both import sites |
| #599 | Cancelling an escalation silenced that alert **forever**, on seven generators | one `open_clause()`, 17/17 mutations |
| #600 | **No tenant that had ever used GMATS could be offboarded** (tenant-less child tables) | 4 tests, ADR-0008 corrected |
| #601 | An admin could promote an account or reset its password with **no audit trail** | 5/5 mutations |
| #602 | A GMATS stock correction, delete or void left **no trace of who did it**; 13 write handlers, 0 audit rows | 33/34 mutations, the survivor investigated and kept |
| #603 | After a void, **two live GMATS tax invoices could carry the same number** (`count()+1` with hard deletes) | moved onto `doc_numbers.allocate`, 18/18 |
| #604 | One deleted inspection **jammed a simulated factory's live activity for good** (same `count()+1` class, third copy) | repo-wide `*_no` guard, 24/24 |
| #605 | The cost-of-losses cards **invented every tenant's money**: £12/downtime-minute, £25/scrapped unit, £8/min in the management summary | one `loss_value` conversion, 27/27 backend + 10/10 frontend |

Two lessons worth keeping:

- **The same defect had three copies.** `count()+1` as a document number was fixed
  once in ADR-0012 for the enterprise routes; GMATS and the simulator still had it.
  The guard is now repo-wide (`test_document_numbers_one_rule.py`) rather than a
  third local fix.
- **A guard can be blind in one syntax.** `test_currency_single` caught `$${` and
  `$<digit>` but not a JSX-text `${m.cost}`, which is exactly how two components
  printed dollars. Both missed lines are now pinned verbatim as a probe.

### 2026-09-18 — verification that had stopped verifying (#631–#644)

Three of these fix what a person sees. The rest fix the proof that everything
else is still true, and put that proof where it runs by itself. The fleet run behind #635 is the thing to remember. Every audit
and mutation harness was run against master, each in its own worktree. It found:
- 13 mutations (11 Python, 2 UI) and 2 audit checks that had stopped testing
  anything, some for six weeks;
- one guard whose only proof a new layer (#631) had absorbed.
Nothing had noticed, because nothing ran them.

| PR | What | Evidence |
|---|---|---|
| #631 | **The founder's company-switcher preview bound whatever `X-Tenant` named, including an OEM's sentinel namespace (`OEM:ACME`).** A crafted request could write where that OEM's sessions read. `effective_tenant` no longer binds a reserved code, and `ReservedPreviewGuardMiddleware` refuses one with a readable 403 on every route. Verified on production: `X-Tenant: OEM:ACME` → 403 | 13 checks |
| #632 | The handover brought up to #630 | docs |
| #633 | **`audit_oem_specialist.py` exited 1 with two FINDINGs while the readiness doc said "NO FINDING".** Both were the audit gone stale within a day: #614's response dicts were counted as writes, and #616 had moved the refusal wording it searched for. The checks now test the properties: they run the refusal, read each grant door, and run `widen_grants` on an in-memory DB. The write detector now also sees constructor keywords, Core `.values()`, raw SQL, and the `ai/` and `amp_ai/` packages. **It is now a CI step** | 160 checks; 12/12 planted defects; 3/3 controls clean |
| #634 | **The Trends card read "OEE up 16 pts to 72% week on week" in green the week the worst machine went silent**, and both `.txt` reports printed a partial plant as the plant. `/oee-trend` now carries `coverage` and `prior_coverage`, and every figure its verdict states is qualified. `/analytics/summary` and `/analytics/management` carry `coverage`, and the reports print "(from 2 of 3 machines)" | 7/7 mutations; 321/321 |
| #635 | **11 mutation tests had silently stopped testing their guards.** Their anchors were stranded by #509, #522, #614, #616 and my #631; `mutate_mqtt_identity` had exited 1 since 2026-08-09. They are retargeted and each shown caught again. `test_mutation_anchors_apply.py` now fails the PR that strands an anchor | 838 anchors in 25 harnesses; its 4 failure modes planted and caught; 322/322 |
| #636 | **Four money figures printed "£49740" where every other card reads "£49,740"** (the enterprise Cost KPI, Costing's Manual Cost, the SaaS page's MRR and each tenant's fee). `lib/money.ts` claimed to be the one place the symbol is written, while 16 lines in 10 files wrote it themselves. Every amount now goes through `money()`, and `lib/currency-literals.test.ts` holds the claim | vitest 619/619; failing first on the 16 lines |
| #637 | **#631's middleware left `service_contracts.for_factory`'s own sentinel refusal untested.** The HTTP check now stops at the middleware, so removing the route's guard went unnoticed; the fleet run caught it as the one real survivor. The guard is tested directly again, by binding the sentinel without the header | harness: all 96 caught or shadowed (exit 1 on master); 321/321 |
| #638 | **The frontend harnesses had the same rot.** #612's `detailOf` → `errorDetail` stranded two of `mutate-oem-ui`'s guards: a refused lookup swallowed into a generic message, and a refused confirmation reported as success. Retargeted, and `frontend/lib/mutation-anchors.test.ts` now checks every UI anchor on each push | 86 anchors; failing first on the 2 |
| #639 | This handover | docs |
| #640 | The two adversarial audits that need no server (`audit_adversarial_final`, `audit_oem_contracts_adversarial`) now run in the backend job, each on its own SQLite file | each shown to exit 1 on a planted defect |
| #641 | The four PostgreSQL audits (`audit_oem_adversarial`, `audit_oem_pilot_journey`, `audit_three_customers`, `audit_isolation`) now run in the migration gate's job, each in a scratch database | each exits 1 on a planted defect; all four passed in CI |
| #642 | **The whole mutation fleet runs every Monday** (`.github/workflows/mutation-fleet.yml`): one job per backend harness, discovered from the tree; `mutate_oem_claim` against a PostgreSQL service; both UI harnesses. It also runs on any PR that edits the workflow. **Its first run found a guard only Windows tested:** the failure-risk generator hash is line-ending-independent, but the only check compared against the committed hash, which an LF checkout (Linux, CI) matches either way. The test now hashes the source both ways | reproduced on Windows with an LF checkout: the old check passes the mutant, the new one fails it |
| #643 | The six PostgreSQL migration verifications (`verify_pg_migration`, `_docnumbers`, `_bom`, `_oem`, `_native_ai`, `_claim`) now run in the migration gate's job. **One had gone stale:** `verify_pg_native_ai` upgraded to "head" and failed when 0010 moved it, on a migration that was still correct; it now names 0009 | each exits 1 on a defect in its own scope (the BOM one's first plant was out of scope: the drift gate owns that constraint) |
| #644 | **The module map named a component #389 deleted** (`IndustrialGatewaySection`). Nothing checked the 728 file names the training docs cite; `test_training_doc_references.py` now requires each to resolve, with the recipe placeholders listed | failing first on the one real reference; placeholder checks fire on their plants |

Lessons worth keeping:

- **Verification nobody runs rots, silently.** The specialist audit went red
  within a day of the changes that broke it. Thirteen mutations applied to
  nothing, some for six weeks. The same fix worked for both: put the cheap part in CI
  (the audit takes seconds, and so does counting anchors). Run the expensive
  part, each harness's own suites, as an occasional fleet, every harness in its
  own worktree.
- **Run the full sweep before opening a PR, even one that only touches a
  harness.** #633's first push failed CI. The audit's new check read
  `OemDataSharingPolicy` with no tenant filter, and
  `test_unscoped_model_reads.py` scans every backend file, audits included. The
  per-file sweep would have caught it; I had run only the audit and its
  mutations.
- **When a planted defect is not caught, check the plant first.** Text appended
  after an anchor leaves the anchor matching: the guard was fine, the plant was
  not.
- **A new guard upstream silently retires the proofs downstream.** #631's
  middleware answers the requests that `test_contract_tenancy.py` used to prove
  the contract routes' own guard with. The test kept passing, and it had
  stopped reaching the guard. Only a mutation run shows that. After adding a
  layer in front of existing checks, give each of those checks a direct test.
- **Take the SHA a poller watches from git** (`git rev-parse HEAD`), never type
  it. A mistyped SHA polls a commit that does not exist, forever.
- **A guard can be tested on one platform only.** Removing the failure-risk
  hash's CRLF normalisation failed a test on Windows (git writes CRLF there) and
  passed it on Linux (the bytes were LF already). The fleet's first Linux run
  found it. A property about platforms needs a test that constructs each
  platform's input, not one that inherits whatever the checkout has.
- **Harnesses restore files through a text-mode write.** On this Windows
  checkout that turns CRLF into LF, with the committed blob's bytes. `git status`
  reports the file as modified; `git diff --ignore-cr-at-eol --name-only` shows
  whether the content really changed.

**Founder decision added:**

12. **The week-on-week OEE verdict now states partial coverage (#634), but it
    still compares each week's whole-plant pooled figure.** Should the change be
    computed only over machines that reported in both weeks (like-for-like, as
    the per-machine movers already are)? That is a methodology choice, not a
    defect.

**Next** (verified in passing unless marked):
- decisions 10 and 12, once chosen;
- the mutation fleet now runs every Monday (#642). After refactoring the OEM,
  tenancy or AI-consent code, or adding a guard upstream of existing ones,
  dispatch it from the Actions tab rather than waiting for Monday.

### 2026-09-18 — figures that claimed more than was measured (#617–#630)

Every one of these published a number or a status the data did not support. Each
was reproduced first (a failing test, or a 20-line probe through the real
read-model), fixed, mutation-tested, swept (per-file, all suites) and merged with
production verified on `/health`.

| PR | What | Evidence |
|---|---|---|
| #617 | The handover brought up to #616 | docs |
| #618 | **InventoryLow came only from the ledger.** Issue slips, cycle counts, PATCH, CSV imports, PO receipt corrections and the BOM subscriber all dropped stock silently, so the Reorder agent never heard. One rule now (`stock_events.stock_dropped`), called by every writer | 10/10 mutations |
| #619 | `/oee-trend`'s "this week" was calendar dates, the headline's is `[now-7d, now)`: **trend 38% beside headline 23%**, one plant, one moment | 7/7 mutations |
| #620 | `/analytics/summary` replaced an unmeasured week's OEE with `utilization × 0.9 × 0.95`: **"OEE 47%" from A 0 × P 0 × Q 0**, printed by `daily-summary.txt`. Fired in every idle week once the summary moved to the 7-day window. The backend helper is deleted | 5/5 mutations |
| #621 | The Costing card printed **"Total lost £110" beside "Losses down £140 to £30 week on week"**: the cost trend's calendar halves. Its series now spans every date of the fortnight, with the oldest flagged `partial`, and the seam date shows both weeks | 12/12 mutations |
| #622 | The scorecard showed **OEE "—" beside "▼72 pts"**. A change was computed from the raw 0 under a None, the prior week counted rows (`bool(records)`), and a plant that did not run had losses of 0 in green. A change now needs two measured weeks | 7/7 mutations |
| #623 | **Every report request read "Generated", and nothing generates a report.** The dashboard sent the status, the API stored it, and the audit trail said so too. The server owns the status now ("Logged") and the view says no file is produced | 5/5 mutations |
| #624 | The downloadable intelligence report printed **all-time** downtime, OEE, top loss reason and worst machine under the 7-day dashboard's labels (330 min vs the week's 30). It now prints the dashboard's own summary, with its window named | 7/7 mutations |
| #625 | **The exec home's Plant OEE rose, with a green arrow, the week the worst machine went silent, and said nothing** (OEE contract §4: every plant OEE states its coverage). The scorecard tile, the briefing headline, the copilot's answer and the weekly report now say "from N of M machines" (`oee_contract.coverage_phrase`, one wording) | 12/12 mutations |
| #626 | The same on the **Executive OEE page's tile** and the **money-story card**, whose upside a silent poor machine also shrinks; `frontend/lib/coverage.ts` mirrors the backend wording | 10/10 mutations |
| #627 | **Every login in the founder's workspace saw the founder-only diagnostics** (`/platform/status`'s sim allowlist, which names other tenants; `/ai/status`'s process-wide last copilot error). Gated on the workspace, not the role. `tenancy.is_founder` is now the one rule | 5/5 mutations |
| #628 | A **machine CSV import could mark a machine Breakdown and leave no trace** in its status history (the timeline, the risk scorer and the failure-risk model all read it). It now writes a `MachineEvent` with source "import", inside the row's savepoint | 5/5 mutations |
| #629 | **A shift nobody planned was scored 0% efficiency** on four surfaces (shift KPIs, the Executive shift chart's 0% bar, `shifts.csv`, `daily-summary.txt`) though `ai/shift.py` already said it has none. `analytics_engine.shift_attainment` is now the one formula | 10/10 mutations |
| #630 | **A goods receipt line could read "Rejected" while its whole quantity went into stock.** The form had no rejected input and a status dropdown; the server stored both as sent. The quantities now decide: rejected = received − accepted, the status follows, and accepting more than arrived is refused | 8/8 mutations |

Lessons worth keeping:

- **A defect found in passing is worth a probe, not a note.** #620, #622 and #623
  were each found while fixing something adjacent. Each was confirmed in minutes
  by driving the real read-model before any code was changed.
- **The count-the-rows rule survives in pairs.** #585 fixed the current week's
  KPIs. The prior week's `has: bool(records)`, and a delta computed from the 0
  under a None, survived until one assertion was stated over the whole payload:
  *no KPI publishes a change without a value*.
- **CI tests each PR on the base it was opened against.** Before merging two PRs
  that CI tested on different bases, merge master and both locally and sweep:
  master + #622 + #623 was 312/312.
- **Parallel CI polls exceed the unauthenticated API limit (60/hour).** One
  poller for every open PR, at 240 s, stays under it.
- **A worktree whose `node_modules` is a junction to the main checkout's** must
  have the junction unlinked (`rmdir` without `/s`) before `git worktree remove`,
  or the recursive delete can reach through it.
- **A harness that pins a payload grows with it.** Adding `coverage` to two
  payloads broke `test_executive_oee_sql_parity.py` (a key-for-key reference)
  and `test_recovery.py` (stubs every DB read). Both designs are deliberate; the
  fix was to extend them (the reference gains the same call, recovery gains a
  `_coverage` seam), not to loosen them. The full sweep found both before CI did.
- **A PR stacked on another** (#626 on #625): after the base squash-merges,
  `git rebase --onto origin/master <old base commit>` moves only the stacked
  commits.
- **GitHub's compare page reflows after it loads**, and "GitHub Community
  Guidelines" sits just under "Create pull request". Two coordinate clicks landed
  on the link. Click the right half of the button, clear of the link's span.

**Founder decisions added** (1–6 are in the #609–#616 section below):

7. **Machine cards' "Estimated OEE"** (`utilization × 0.9 × 0.95`, labelled) is now
   the only OEE estimate in AMP. Keep a labelled estimate for a machine with no
   production, or show "—"?
8. **Report Requests is a log;** nothing generates the file a request names.
   - Keep it as a log, remove the form, or build report generation?
   - Rows already in production still say "Generated": rewrite them to "Logged" (a change to stored data), or leave them?
9. **The production card's week is calendar dates** (`[midnight(today-6), now]`),
   while OEE and cost use the rolling contract window. So the scorecard's
   good-rate change compares a calendar week with a rolling prior week, and a
   record late on date `today-7` is in neither. Move the production card to the
   contract window?
10. **Smart alerts, and "Generate escalations from smart alerts",** judge a
    machine's OEE from its latest record however old: *"critically low at 25%"*
    for a machine that last ran 20 days ago, and a Critical escalation from it.
    Judge over the contract window, or state the run's date? (There are two alert
    generators, `build_smart_alerts` and `generate_alerts`, with different
    thresholds and severities; choosing the basis is a chance to make them one.)
11. **Goods receipt lines already stored** keep the status and rejected count they
    were given, which can contradict their quantities (#630). Recompute them from
    received and accepted (a change to stored data), or leave them?

**Fixed in this handover's own PR:** the founder handbook said twice that an
auto-approved reorder PO "stays a Draft". With `AUTO_APPROVE_AGENTS=reorder` (the
default) `apply_decision` moves it **Draft → Approved**; the handbook now says so.

**Next** (verified in passing unless marked):
- decision 10 above, once chosen;
- ~~the remaining plant-OEE text surfaces (`/oee-trend`, `/analytics/summary`, `/analytics/management`, the `.txt` reports) carry no coverage yet~~ **done, #634**;
- ~~six frontend spots write "£" literally~~ **done, #636**: there were 16 lines in 10 files, and four printed amounts with no thousands separator, so it *was* a visible defect;
- ~~the OEM sentinel namespace via `X-Tenant`~~ **merged, #631**;
- ~~`audit_oem_specialist.py` reports two open findings~~ **triaged: both were the audit's own staleness, not the product's. Fixed and put in CI, #633**.

Checked and not a defect:
- **PLC signal mappings** are stored and never applied, but `docs/sales/REAL-OEM-INPUT-REQUIRED.md` already says so, and no screen claims otherwise.
- **OEM tokens and the founder check.** An OEM token carries no `tenant` claim, but `get_current_user` refuses OEM principals on every factory route before any founder check runs.

### 2026-09-18 — AMP-native AI, service contracts, and the queue's tail (#609–#616)

The three branches that were awaiting review merged, and so did four fixes. Each
merge was verified live on production (`/health` version read back).

| PR | What | Evidence |
|---|---|---|
| #609 | An agent proposal **holds its item** until an approver decides it (queue item 2) | 409 on every PATCH/DELETE bypass, withdraw-on-decide audited, `SELECT … FOR UPDATE` proven on PostgreSQL |
| #610 | **AMP-native AI**: three models in pure Python, no external model. Failure risk was adopted, on synthetic evidence only. Telemetry anomaly and copilot intent were **not** adopted, having failed their own gates. Learning happens only with a company Admin's consent; migration 0009 | 283 suites; PostgreSQL verified |
| #611 | Two documents still printed the **leaked GMATS password** as the login to type; a fresh compose stack had no login; the smoke test could not be pasted (queue item 11) | the guard now reads every tracked file; 5/5 mutations |
| #612 | The workspace **brand colour and logo** were saved, shown back and applied nowhere; the server now validates both (queue item 10) | 15/15 mutations |
| #613 | Which packs a plan includes was written in **five** places, and only apply-plan read `modules.json` (queue item 9) | 8/8 mutations |
| #614 | **Agreed downtime attribution** for OEM service contracts (ADR-0021); migration 0010 | 304 suites; 45 PostgreSQL checks; harnesses 37/101/96/44/53 |
| #615 | An inventory item could be **created or CSV-imported with a negative stock** (a sweep candidate) | 7/7 mutations |
| #616 | A factory was asked to **consent to three kinds of sharing that never happened** (alarms, telemetry, maintenance history: nothing reads them); a factory is now offered only the grants AMP reads | 8/8 mutations; **reverses a documented choice**, see founder decisions |

Lessons worth keeping:

- **CI's `coverage` job runs every suite in ONE pytest process.** Two new suites
  pointed `SessionLocal` at their own in-memory database and never restored it.
  `test_boot_migrations`, which sorts after them, then queried the wrong database
  (#610's first failure). Nine suites on master still leak this way and are safe
  only because of their sort order; a follow-up task was offered.
- **A wall-clock budget under coverage's tracer measures the tracer.** The same
  build took 37 s bare and about 108 s traced. `backend/wallclock.py` is the rule:
  the standalone run always judges the budget, and the pytest entry judges it only
  when untraced.
- **Two parallel branches can each pass while the pair fails.** Native AI pins
  `requirements.txt`, and contracts added `tzdata`. Only the combined CI caught
  it, because my local runs covered only each branch's own suites. Run the full
  sweep on the combined tree before opening the PR.
- **A guard's reach check must inspect what the loop read, not a list beside
  it.** The first version in #611 let the "backend `.py` only" mutant survive.
- **Two UI mutants in the contracts harness could never be killed.**
  - A float formatter prints ≤14-digit money exactly: 402,400 probes found no difference.
  - A hard-coded grant is unobservable behind a disabled Accept.

  Both were replaced with mutations that can happen.

**Branches awaiting review:** none.

**Founder decisions open (nothing below is blocked on code):**

1. **Service contracts, before telling a customer:**
   - the freedom-to-operate review (Rockwell US10747201B2, live to 2038);
   - statuses are not authenticated as the manufacturer's (ADR-0021, limitation 3).
2. **May a founder's company preview give a company's consent anywhere?**
   - Refused today: contract signing and AMP-native AI consent.
   - Still accepted: connected-equipment sharing grants and machine claims.
3. **#616 reverses advance consent to reserved grants.**
   - If you want it back, the honest version is a box marked "not active yet".
   - When a reserved grant gains a reader: do consents stored before then count? ADR-0017 says to ask again.
4. **Native AI:** the failure-risk model's evidence is synthetic. It needs validation on a real plant's history, with that plant's consent, before any accuracy claim.
5. **A work order created as Completed never moves its BOM stock** (`work_orders_routes.create_work_order` says so on purpose). Right for back-filling a job whose stock was already counted, wrong for recording one that just finished. Which should it be, or should the form ask?
6. **Agent approvals (ADR-0015):**
   - Should agents propose for tenants without the Intelligence Pack?
   - Should an expired proposal be cancelled automatically?
   - Should boot sweep orphaned proposals?

**Next from the sweep** (candidates in `tasks/sweep_result.json`, unverified unless marked):
- InventoryLow is published by the ledger only, not by issue slips, cycle counts, PATCH or CSV stock drops, so the Reorder agent misses them;
- a work order created as Completed never moves its BOM stock;
- plant OEE is published without its coverage on most surfaces;
- the handbook says an auto-approved reorder PO "stays a Draft" (the code approves it);
- the OEM sentinel tenant namespace is checked only at registry create;
- founder-only `/platform/status` and `/ai/status` are gated on workspace, not role.

### 2026-09-18 — importing a dev script deleted every tenant's inventory (#606)

`backend/reseed_inventory.py` did its work at module level: three bulk DELETEs
with no filter and no tenant bound. The ADR-0002 hook scopes SELECTs, never a
bulk DELETE. So `import reseed_inventory`, or running it, removed every tenant's
items, stock movements and POs. The reorder agent's PO proposals stayed
`Proposed` in the approval queue, pointing at nothing. Only `backend/.env`
naming localhost kept it off production. It was exempt from both guards built
for exactly this (`test_bulk_write_scoping`, `test_unscoped_model_reads`) as
"local dev reseed".

| | |
|---|---|
| Reproduced | On a throwaway SQLite file with two tenants: a bare import, a run with no `--tenant`, and a run under `RAILWAY_ENVIRONMENT=production` each emptied both tenants, and the runs exited 0 |
| Fix | Work only under `__main__`. Refuses production first via `schema_guard.is_production()`, with no new definition. Requires `--tenant` and `--yes`. Every delete filters `tenant_code`; the seed runs with the tenant bound. PO proposals are deleted with their POs in one transaction. Refuses if a row it keeps (GRN line, issue slip, another tenant's movement) points at an item |
| Guards | Removed from both `SKIP_FILES`: they now pass on it and check it like product code |
| Evidence | 8 tests, all red first; 19/19 mutations; 246 suites; never run against a real database |

Lesson worth keeping: **an exemption comment is a claim nobody re-checks.**
"local dev reseed" was true only because of what one `.env` file contained,
not because of anything in the code. When an allowlisted file changes, test
whether it still needs the exemption before editing the comment.

### 2026-09-16 — the sweep, and the first thing it found (#595)

**The sweep.** Rather than pick off the next known item, a read-only Workflow
searched the whole repo for this family from six angles (env vars; claims about
OTHER code; accepted-but-inert inputs; a rule at N call sites with one missing;
UI copy promising an effect; docs guarantees). 67 raw → 57 deduped; each of the
top 18 was attacked by TWO distinct-lens verifiers (reproduce/find-the-missing-
implementation, and does-it-matter), surviving only if neither refuted. **13
confirmed (11 distinct), 5 refuted, 39 NOT verified** (cap — they are candidates,
not facts). Full result: session scratchpad `tasks/sweep_result.json` (not in git).

**Verified queue, in order** (security/tenancy, then published figures):

| # | Defect | Sev | Status |
|---|---|---|---|
| 1 | **Founder-workspace non-Admins locked to GMATS, not their own tenant** — read GMATS's stock/rates/customers; Supervisor could write it | P2 (cross-tenant WRITE) | **fixed #595** |
| 2 | Agent approval gate bypassed by generic PATCH — an Operator opens/cancels a Proposed maintenance task from the CMMS dropdown; AgentAction stays Proposed, a later decision contradicts reality (`approvals.py:1`, ADR-0005) | P2 | **fixed #609** |
| 3 | `/analytics/executive-oee` ranks machines with **fabricated** OEE (utilization, 90/60, 95) when they produced nothing; `lib/oee.ts readMachineOee` marks every ranking row `measured: true`, so the dashboard shows "OEE 68%" not "Estimated". PRODUCTION-READINESS-FINAL claims closed | **P1 (both lenses)** | **fixed #597** |
| 4 | Inventory CSV import omits `X-Tenant` — founder previewing a client imports into DEFAULT (`EnterpriseInventory.tsx:706`) | P2 | **fixed #598** |
| 5 | Tenant purge fails on PostgreSQL for tenants with GMATS proformas/MINs — `gmats_proforma_lines`/`gmats_min_lines` have no `tenant_code`, and the registry row is already deleted (`offboard_tenant.py:5`, ADR-0008) | P2 | **fixed #600** |
| 6 | Seven escalation generators dedup on `status != "Resolved"` — a Cancelled escalation blocks that alert forever; #565 fixed only smart alerts (`ai/escalations.py:68`) | P2 | **fixed #599** |
| 7 | Admin role changes and password resets are not audit-logged (`users_routes.py:7`) | P2 | **fixed #601** |
| 8 | GMATS corrections and voids leave no audit trail ("full audit trail" promise) | P2 | **fixed #602** |
| 9 | Plan bundles defined twice: `modules.json` vs `PLAN_MODULE_TIERS` (tenant create/plan change) — one rule, two implementations | P3 | **fixed #613** (it was five copies, not two) |
| 10 | Branding colour/logo saved and re-shown, never applied | P3 | **fixed #612** |
| 11 | DOCKER.md says a fresh stack seeds `gmats`; the seed needs `GMATS_PASSWORD`, compose never sets it | P3 | **fixed #611** (and two docs printed the leaked password) |

Item 2 (the approval-gate bypass) is **built and rebased in a worktree**, not yet
merged — see "Branches awaiting review" below. Of the 39 candidates the sweep
could not verify, two were checked and fixed while working nearby (#603, #604);
the rest are still candidates, not facts.

Also still open, lower: `IndustrialDevice.topic` (P3 — the registration form never
sends it; API-only). Refuted by the verifiers (do NOT re-raise without new
evidence): `is_active` unchecked at login/refresh; agent-policy PUT unaudited;
SaaS registry writes unaudited; login rate limit keyed on spoofable XFF; enterprise
CSV `int_cell` bound.

**#595 — the fix.** `gmats_inventory_routes._effective_tenant` returned `"GMATS"` for
every non-Admin DEFAULT login: #500 closed "any customer" by hard-coding one. The
`Gmats*` tables are outside `SCOPED_MODELS`, so this function is their only
boundary. Reproduced: founder Operator/Supervisor/no-role → GMATS's item master;
Supervisor stock-in to GMATS **allowed (40 → 47)** while its OWN item got **403**.
Fix: delegate to `tenancy.effective_tenant` (no private rule); dashboard switcher
now `canSwitchCompany` (DEFAULT **and** Admin), not `isFounder`. 21 backend checks
incl. a workspace×role×request agreement matrix and an every-route-passes-the-
boundary guard; 5 frontend tests incl. a structural guard on the three dashboard
sites. **13/13 mutations red — and B1 (the defect itself) was caught ONLY by the
new suite**: the existing GMATS, tenancy and isolation suites all passed with it.
An OEM-token path looked open in a direct handler call but is NOT reachable —
`auth.get_current_user` 403s `principal=oem` first; kept as a defence-in-depth check.
Browser verification not done: it needs a founder non-Admin login, and Claude does
not enter credentials.

### 2026-09-14 — configuration the code promises and does not keep (#593, #594)

A new family, opened after the honesty line closed. The honesty line was about
numbers AMP published that it had not measured. This one is about **behaviour a
comment promises that the code does not implement** — which is worse to read,
because a careful engineer checks the comment and stops looking.

| Task | Priority | Status |
|---|---|---|
| **`MQTT_TOPIC` was read nowhere.** The comment above the topic constants said it "is still read so an existing deployment … keeps that topic working … rather than **silently going deaf**". Exactly one hit across the backend: the comment. A deployment carrying the pre-upgrade variable subscribed to `flowmes/+/+/machines` while its gateway published to `flowmes/machines`, and **not one line was logged** — being unsubscribed is silent in a way being rejected is not, because `on_message`'s refusal path only runs on a message that arrives. ADR-0011 called this breaking change "deliberately a loud one"; it was silent | P1 | fixed #593, 10/10 mutations red |
| The fix splits the variable: the **prefix** is honoured (reading a variable the operator set is not guessing), the **tenant** is still never invented (`mqtt_identity`'s whole preamble), and the part that was actually missing — **saying so** — logs at WARNING naming the unheard topic and the variable that repairs it, only when something really is not doing what it looks like | P1 | 19 checks |
| **Two mutation survivors were real test weaknesses, not weak mutations.** Asserting on the topic string could not tell the two warnings apart (both quote `MQTT_TOPIC`); and `load_dotenv()` at import meant the local `.env` had already populated the warnings, so a mutation logging the **stale** import-time list passed locally while it would have failed on CI, where there is no `.env` | P2 | both fixed first |
| **Revoking a module left it usable for up to another minute.** `plan_gate` caches licences 60 s; four handlers write one and three dropped the entry. `PATCH /tenant-config` did not — while the comment on `update_any_tenant` said *"the self-service update_tenant_config already does this"*. Reproduced: after withdrawing `intelligence`, the gate still returned it. It also hid from grep — that handler sets `plan` via `setattr` over a tuple, so `grep "\.plan ="` found the three right ones only. ADR-0008 had it as a known limitation | P1 | fixed #594, 8/8 mutations red |
| **Not a fourth copy of the rule.** `commit_tenant_config` commits and invalidates together, on every tenant-config write (branding too — "only licence fields" needs a correct field list, and a wrong one is this bug again). An AST guard fails for a fifth handler that commits on its own, and probes itself: emptying its field set turns it red rather than all-clear | P1 | 11 checks |

**For the user, not code:** `backend/.env` on this machine still reads
`MQTT_TOPIC=flowmes/machines` with no `MQTT_LEGACY_TENANT`. Untouched — it is
untracked local config. Local MQTT ingest now *says* it is deaf instead of
pretending; set `MQTT_LEGACY_TENANT=DEFAULT` or repoint the simulator at
`flowmes/DEFAULT/-/machines` to actually receive.

**Still open in this family** (verified, not yet shipped): `IndustrialDevice.topic`
is settable on create and rendered in the connection drawer beside the device,
but nothing subscribes to it or routes by it — routing is per-(tenant, site) by
design, so a free-text per-device topic cannot route without breaking the
tenant-in-topic security model. Same shape as #582's "Online" default.

### 2026-09-14 — an idle day is not a catastrophic day (#592)

The last recorded item from the window audit, and the second face turned out to
be worse than the one that was recorded.

| Task | Priority | Status |
|---|---|---|
| **A day with no production read `oee: 0`**, so an idle Sunday drew as a full-height RED bar beside a good Monday. Reproduced: a plant that ran one good day at 41% published **seven of eight bars at zero** — a plant that runs five days a week renders as a plant failing twice a week | P2 | fixed #592, 7/7 mutations red |
| **The same zero produced a RANKING, which is worse than a chart.** A machine that ran at 80% last week and did not run at all this week scored `delta = -80` and topped `declining_machines` — the plant's worst decliner, **for not running**. A machine commissioned this week scored `prior_oee = 0` and topped `improving_machines` with an improvement that never happened. Those two lists are what a manager reads to decide where to walk first | P1 | fixed #592 |
| **The convention already existed ten lines below**, at plant level: *"No production recorded this week (OEE was N% last week)"* — precisely the case the per-machine code turned into a -80 point decline. Same shape as #585 | P2 | applied per machine |
| **The siblings are NOT affected, and that was checked rather than assumed.** `ai/cost.py` and `ai/downtime.py` build the same improving/worsening lists from the same halves and are RIGHT to use zero: cost and downtime minutes are **extensive**, so a machine that did not run genuinely incurred no loss and no downtime. Only the **intensive ratio** has no value when its denominator is absent — which is why this touched one file | P3 | scope justified |

**Recorded, not invented:** the trend payload exposes only the two ranked lists, so excluding a stopped machine removes it from the trend entirely. That is strictly better than showing it as a -80 point decline, but *"which machines stopped"* is real information with nowhere to go. A `stopped_machines` surface would need a consumer first.

### 2026-09-13 — three OEE rollups measured all time (#591) — window audit CLOSED

| Task | Priority | Status |
|---|---|---|
| **`analytics_summary`, `/analytics/management` and `/analytics/executive-oee` pooled `production_records` with NO date filter**, publishing a LIFETIME plant OEE under the same name every other surface uses for seven days. Measured: **35% on the Executive ranking against 83% in the cockpit** — 48 points, same machine, two screens a user clicks between | P1 | fixed #591, 6/6 mutations red |
| **The endpoint's own comment claimed the opposite** — *"the single standardised OEE definition … so /analytics/executive-oee agrees with /oee-summary and every other surface"*. It cannot, while measuring a different span: the standard had been applied to the FORMULA and not to the WINDOW | P1 | fixed #591 |
| The same file had already made this exact argument once, 300 lines earlier, bounding `shift_data` to the most recent 50 *"so the two per-shift attainment surfaces reconcile on ONE basis instead of disagreeing"*. Right reasoning, applied to one column of one endpoint | P2 | now applied to all three |
| **What moves:** these three published figures describe the last seven days rather than all history. A real change to a flagship number, hence its own PR and its own measurement — the #583 rule | P1 | measured before shipping |
| **Mutation testing found three gaps in my own fixtures:** no test here had created a `DowntimeLog` or a `QualityInspection` at all, so bounding production alone left the downtime and quality windows unguarded. A year-old inspection could hand a machine that has not run a fabricated OEE through the no-production fallback | P2 | fixtures added |

**The 60-agent window audit is now closed.** 17 raw findings → one root cause → #584, #586, #587, #589, #590, #591. What remains recorded and deliberately unfixed: the daily OEE series still reads `oee: 0` for a day with no production (the #585 rule on a typed frontend series).

### 2026-09-13 — the machine cockpit measured one machine over three weeks (#590)

| Task | Priority | Status |
|---|---|---|
| **One card, every panel labelled "last 7 days", three different bases.** The OEE panel on the rolling `OeeWindow` (eight calendar dates); `production_7d` and `downtime_7d` narrowed to seven; `quality` on seven **plus everything dated in the future**. Measured: the **OEE Quality bar 55% beside a Production good rate of 100%** — the same ratio, good over total, forty-five points apart on one card | P1 | fixed #590, 8/8 mutations red |
| `_machine_quality`'s own docstring claimed the opposite — *"Every other cockpit panel is the same 7 days, so one basis for the whole card"* — while its query had no upper bound at all. A fail rate of 34% was being driven by 500 failures **dated two days in the future** | P1 | fixed #590 |
| The OEE panel does **not** move: same choice and same reason as #586 — the canonical window is the contract, and the panels that disagreed with it are the ones that changed. Pinned by a control, and a mutation implementing the reverse is caught | P2 | 22 checks |
| **Mutation testing found a reachable gap the fixtures missed:** an unbounded downtime query admits a stoppage timestamped LATER TODAY, which carries today's date and so lands in today's bar. Unlike the other upper-bound cases this one needs no clock skew — just a row written a few hours ahead | P2 | fixture added |

### 2026-09-13 — the failure sparkline covered 28 of the 30 days it sat under (#589)

| Task | Priority | Status |
|---|---|---|
| **Four buckets of seven days under a 30-day headline** — and the code said so itself: *"the last 4 whole weeks (28 of the 30 days)"*. A stoppage on day 29 or 30 counted in `failures`, MTBF and `top_modes`, and was drawn in **no bar**. The buckets now derive from the window: 30 = 4×7 + 2, so the oldest is two days and says so | P2 | fixed #589, 8/8 mutations red |
| **`_window_logs` had no upper bound either** (`created_at >= start` and nothing else), so a future-dated stoppage — a bad gateway clock, a manual entry — was in the headline, MTBF and MTTR and outside every bucket. Same shape as the recorded-cost query in #587 | P2 | fixed #589 |
| **The bars were labelled with a bare date but cut at the query's time of day**, so ~half of the labelled day sat in the bar before it, and which half moved with the hour the drawer was opened. Each bucket now publishes `week_end` and `days` | P3 | fixed #589 |
| `test_reliability.py` asserted `len(weekly) == 4` — pinning the defect. Now derives the count, and asserts the property that matters: **the bars account for the headline** | P3 | updated with reasoning |

**Honest note on the one mutation that needed a structural guard:** flipping the bucket comparison to `<=` survives every fixture, because the edges come from a `utcnow()` taken inside the read-model while a row's `created_at` comes from an earlier one — an exact hit needs a microsecond coincidence. Unlike #584, where two windows shared a boundary instant **by construction**, this is not reachable by data, so it is pinned structurally rather than with a fixture that cannot see it.

### 2026-09-12 — a customer's production data was written into the shared platform log (#588)

| Task | Priority | Status |
|---|---|---|
| **`mqtt_service.on_message` logged the whole decoded payload at INFO, for every message.** Machine names, production counts, good/reject splits and every `readings` value — into a **multi-tenant** log stream, at whatever rate the plant publishes. Read by anyone with platform log access and retained by the host's aggregator for ITS retention, not the 14 days `docs/RETENTION.md` promises for `iot_telemetry`. A compressor reporting every few seconds writes its owner's operational data there all day | P1 | fixed #588, 7/7 mutations red |
| The banner carried a **leading newline**, splitting one record across two lines of a stream `JsonFormatter` emits as one JSON object per line — so the following line was not parseable as a record | P3 | fixed #588 |
| **Mutation testing found a second per-message INFO line** naming the same machine (the `/ws/live` broadcast). A mutation that stopped the ACCEPT line naming the machine survived, because that one still was. Two records per message for one fact; now one, asserted **as a count** so a third cannot appear | P2 | fixed in the same PR |
| **Not a blanket redaction.** INFO keeps one low-cardinality accept line — tenant, site, machine, status transition — because that is exactly what the field procedure reads to prove a gateway is talking to AMP. The body moves to DEBUG, and every rejection warning is untouched: a silently dropped message is the failure the whole ingest path is written to avoid | P2 | 16 checks |

### 2026-09-12 — the cost chart could sum to zero under a five-figure headline (#587)

The cost face of #586, and larger, because a cost figure is read as money.
`build_cost_summary` published **three figures on three bases under one
`"days": 7` label**.

| Task | Priority | Status |
|---|---|---|
| **Headline £30,760; every bar £0.** `loss_cost` pools the rolling `OeeWindow(7)`; `daily` bucketed seven calendar dates, so a costly run on the partial eighth date was priced into the headline and drawn in no bar | P2 | fixed #587, 8/8 mutations red |
| **`by_type` had NO UPPER BOUND** — `CostRecord.created_at >= midnight(today-6)` and nothing else. A cost record dated **three days in the future** was published as part of this week's costs. Now bounded at both ends by the same window | P2 | fixed #587 |
| `test_cost.py` asserted `len(daily) == 7` — pinning the defect — while already asserting the bars sum to the headline, which was the half it had right. The length now derives from the contract | P3 | updated with reasoning |

### 2026-09-12 — the OEE trend bars could not add up to the headline (#586)

| Task | Priority | Status |
|---|---|---|
| **The card's headline and its own chart disagreed, with nothing to explain the gap.** `build_oee_summary` pools the rolling `OeeWindow(7)` — which touches **eight** calendar dates — while `daily` bucketed `[today-6 … today]`, only seven. Every record on the partial eighth date was in the headline and in no bar. Measured at 11:48 UTC: **headline 45%, bars pooling to 83%**, a 38-point gap whose size depends on the hour the card is opened | P2 | fixed #586, 8/8 mutations red |
| **Two defensible fixes, and they differ in whether the flagship number moves.** (a) narrow the headline to the seven bar dates — matches `ai/production`, but changes published plant OEE; (b) widen the bars to cover the window the headline already pools. Took **(b)**: `OeeWindow` is the canonical window, the headline is correct as measured, and it was the chart that could not explain it. Section 5 of the suite is a control pinning that plant OEE did **not** move, and a mutation implementing (a) by accident is caught | P2 | measured before choosing |
| The oldest bar is flagged `partial` and rendered at half opacity with a tooltip, because the window opens part-way through it. Drawing it as a whole day would move the lie rather than remove it. A midnight-anchored window draws seven bars and marks none | P3 | 14 checks |

### 2026-09-12 — a plant that had not run was reported as a plant running badly (#585)

| Task | Priority | Status |
|---|---|---|
| **Three of the scorecard's four KPIs fabricated a zero when their pillar had no data.** Reproduced on a plant that dispatched an order and produced nothing: *Plant OEE 0% (red), Good rate 0% (red), Cost of losses £0 (**green**), Delivery reliability 100% (green)*. Read as a customer reads it: the plant ran catastrophically, everything it made was scrap, and it eliminated all its losses. The plant was idle | P1 | fixed #585, 7/7 mutations red |
| **Not hidden by `has_data`** — that is an OR across three pillars, so one pillar with data publishes the whole strip including the two with none. A shutdown, a holiday week or a tenant mid-onboarding is exactly that case | P1 | each KPI now asks its own pillar |
| **Mutation testing found a second defect inside the fix.** `good_rate`'s basis was `runs > 0` — the count-the-rows rule `oee_contract.is_measurable` was written to replace (*"a row that recorded nothing satisfies all four and satisfies none of the definitions"*). A production record with `total_count 0` published a good rate. Now `total > 0` | P2 | fixed in the same PR |
| The tone is the string `"none"`, not Python `None`: the contract `_tone(None, ...)` already returns, in `ScorecardStrip.toneCls` (slate) and in its TS union. **The first version of the test asserted `None`** — inventing a convention beside an existing one, which would have painted the literal class `undefined` | P3 | test corrected to the codebase's contract |

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

**2026-09-20 — ADR-0024, ADR-0025, ADR-0026: the owner's three views.** The
Factory Command Centre ranks problems by what they cost and never totals them
(#647, merged). The Root-Cause Explorer separates what AMP measured from what
has a reason recorded, and states the remainder. The Risk Radar says what is
likely to become a problem, with the rule and the threshold on the card and no
probability anywhere. All three read the same read-models and carry the same
evidence vocabulary as the Copilot.

**2026-09-19 — ADR-0023: a self-hosted model behind an earned switch.** The
local OpenAI-compatible provider, the adapter, and an adoption record: a model
is used only once THAT model has passed the Copilot evaluation. `/ai/ask` moved
onto the orchestrator, which fixed its tenant (it used the token claim, so a
founder previewing a company priced that company's rows at the founder's own
unit value).

**2026-09-19 — ADR-0022: the Copilot answers through typed AMP tools, with
evidence, and a model may only word it.**

- **Tools.** 18 tools in `ai/tools/`, each no wider than the REST route it mirrors (the live app is read to check). `run_tool` checks role, licence, strict arguments and tenant binding on every call.
- **Evidence and states.** `ai/evidence.py` holds the provenance, data-state and root-cause vocabulary, mirrored in `frontend/lib/evidence.ts`.
- **Grounding.** `ai/grounding.py` rejects any model wording whose number or name is not in the evidence.
- **Evaluation.** `copilot_eval/` uses three factories whose identifiers collide, plus an OEM, 32 questions and 15 adversarial prompts. `test_copilot_eval.py` gates on zero disclosures, zero ungrounded answers shown and zero money fabrications, for AMP's engine and for nine scripted model behaviours.
- **Routing unchanged.** `/copilot/ask` routes exactly as before and now returns the evidence.
- **Phase 3 is no longer blocked on a key.** It is blocked only on the founder's permission to download local weights. Whatever model arrives is measured by `copilot_eval`, not by hand.

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

**Superseded 2026-09-17, by founder directive: no dependency on external AI
models.** A key is no longer the missing piece. The router candidate is
`feat/amp-native-ai`'s trained intent classifier (pure Python, no network call;
see "Branches awaiting review"). It enters through a new `proposer` seam in
`assistant.answer`. That seam is narrower than `chosen_route`: it uses the same
`_pillar` allowlist, but only after the machine-name lookup.

**It has not yet beaten the bar.** Per the branch's own
`amp_ai/artifacts/copilot_intent_v1.eval.json` (read, not re-run), on the odd
halves of the two held-out sets:

| Held-out set | Keywords | Hybrid (model + keywords) | Model alone |
|---|---|---|---|
| set one | 15/26 | 17/26 | 15/26 |
| set two | 15/26 | **15/26** | 14/26 |

McNemar gives p=0.5 on set one and p=1.0 on set two. That is a possible +2 on
one set and nothing on the fresh one, which is the same plateau signature
recorded above. Review the branch with that in mind. It proves the model is
safe to wire in; it does not prove it answers better.

Phases 2, 4–6 not started. Note before starting Phase 2: the LLM is already read-only and is handed a pre-built text context (`_build_factory_context`), which is the correct shape — do not rebuild it.

---

## REAL OEM INPUT REQUIRED

- Which of `SHARE_ALARMS` / `SHARE_TELEMETRY` / `SHARE_MAINTENANCE_HISTORY` / `SHARE_DOWNTIME` matters commercially, and what the data must look like. Alarm codes are meaningless without the manufacturer's fault dictionary.
- Whether warranty runs from despatch, delivery, installation or commissioning (`warranty_months` is declared and unused for exactly this reason).
- Any real PLC/controller before touching industrial protocol drivers.

---

## HARNESS DEBT

- **The fleet run (2026-09-18, master `e0baec2` to `7b35568`).** Every
  `mutate_*.py` (25) and the two SQLite audits were run, one worktree per lane,
  because a harness edits source in place. Results:
  - **20 exited 0**, including both audits.
  - **`mutate_amp_ai_integration` and `mutate_mqtt_identity`** exited 1 on
    stranded anchors. **`mutate_oem_auth`, `mutate_oem_lifecycle` and
    `mutate_oem_service`** were not run on master: their anchors were stranded
    too. All 11 stranded entries were fixed and shown caught in #635.
  - **`mutate_service_contracts`** exited 1 on one real survivor, a guard that
    #631's middleware had shadowed. Fixed in #637.
  - **`mutate_oem_claim`** cannot run in a worktree: its baseline includes
    `verify_pg_claim.py`, which borrows PostgreSQL credentials from
    `backend/.env`, and only the main checkout has that file. Run it from the
    main checkout, with `DATABASE_URL` pointing at a scratch SQLite file for the
    SQLite suites. On `c217bcf` it exited 0: all 29 mutations caught or shadowed
    with recorded reasons, including #635's three retargets and the PostgreSQL
    race. It rewrites the files it mutated with LF. Check
    `git diff --ignore-cr-at-eol` is empty, then `git checkout --` them.
  - **The frontend harnesses** (`mutate-oem-ui.mjs`, `mutate-contracts-ui.mjs`)
    had the same rot. #612's `detailOf` → `errorDetail` stranded 2 of
    `mutate-oem-ui`'s 33. They were fixed in #638, and
    `frontend/lib/mutation-anchors.test.ts` now checks all 86 anchors on every
    push. Run in full on #638's branch, both exit 0 (33/33 and 53/53). They
    restore each file's own line endings, so they leave no noise.
- **Both halves run by themselves now.** On every push,
  `test_mutation_anchors_apply.py` and `frontend/lib/mutation-anchors.test.ts`
  check that every anchor still names one line. Every Monday at 04:17 UTC, and
  on dispatch, `.github/workflows/mutation-fleet.yml` runs every harness (#642).
  That fleet run is the only place a guard shadowed by a new upstream layer
  shows up, as #631 did to #637's. A red run is a finding: read the harness's
  own output before touching its expectations. To run it early after
  refactoring guarded code, dispatch it from the Actions tab.
- **Every audit and PostgreSQL verification that can fail runs on every push**
  (#633, #640, #641, #643): the specialist audit and the two SQLite audits in
  the backend job; the four PostgreSQL audits and six migration verifications
  in the migration gate's job. `audit_perf.py` stays out: it
  measures latency and has no pass/fail line.
- **Line endings.** Several harnesses restore a file through a text-mode write,
  which leaves LF where a Windows checkout had CRLF, with the committed blob's
  exact bytes. Harmless (git normalises; Linux CI never sees it), but judge a
  harness's leftovers by `git diff --ignore-cr-at-eol`, not by `git status`.
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
   unsynced (MQTT / events / twin are done). The list of the 20 was never
   written down, so a new pass would have to re-derive it. What IS guarded
   since #644: every file the training docs name exists
   (`test_training_doc_references.py`); claims about behaviour are not checked.

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
