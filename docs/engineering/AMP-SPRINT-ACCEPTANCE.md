# AMP — 10-day sprint acceptance record (release candidate)

**Question:** does the platform the sprint built pass the founder's twelve
final-acceptance items (brief §18), with evidence that can be re-run rather
than recalled?

**Answer: YES for the sprint's scope, with the limits in §3 stated rather than
hidden.** Every item below names the file that proves it, where it runs, the
measured result and its date, and what it does *not* prove.

Date: 2026-09-20 · master `5e8b863` (#662, the last code change of the sprint)
· production verified at the same SHA (§2, item 12) · local re-runs on
PostgreSQL 18.3 and SQLite the same day.

---

## 1. What "acceptance" means here

An item is accepted when a script AMP ships exercises it end to end and that
script is **green in CI on every push** (or, for the two that need a GPU or a
long wall-clock, was run by hand today and its output recorded). A number in
this document is either from a run today or carries the date of the run it
came from. Nothing is inferred from an old commit message.

The backend suite itself: 344 standalone `test_*.py` files, run one process
each by the CI backend job (all green on master today), and the same files
under one pytest process by the CI coverage job (branch-coverage floor 78%).
Reproduced locally today under one pytest process: **1,542 passed**, one
failure that is a Windows-only temporary-file lock in `test_schema_guard`
(CI's Linux runner does not hit it). Mutation harnesses: 35, whose 1,014
anchors `test_mutation_anchors_apply.py` proves still name one line each.

## 2. The twelve items

| # | Item | What runs | Where | Result (date) | What it does not prove |
|---|---|---|---|---|---|
| 1 | **Three-factory simulation** | `audit_three_factory_simulation.py`: main's exact tick sequence for FACTORY_A, B and C from one loop, 20 rounds each, checked against the raw tables (52 tenant-carrying tables, 40 parent links) | CI backend job, every push; SQLite | **ALL 98 CHECKS PASSED** (2026-09-20, local and CI). Found and fixed on the way: a tick with no tenant bound filed every tenant's activity under DEFAULT (41 rows in 12 unbound rounds); every tick now refuses (ADR-0002 postmortem) | Runs on SQLite; the storage layer on PostgreSQL is covered by items 6 and 8, not by this audit |
| 2 | **OEM simulation** | `audit_oem_demo_journey.py` (the ten-minute demo through the real API), `audit_oem_pilot_journey.py` (a manufacturer onboarded and its machine accepted, no developer), `audit_oem_adversarial.py` (two OEMs against three factories over HTTP, WebSocket, MQTT and CSV), `audit_oem_specialist.py` | CI backend job (SQLite) and CI migration-gate job (PostgreSQL), every push | Demo journey **64 steps, end to end** · specialist **161 checks** · on PostgreSQL 18.3 today: pilot journey **41 steps, all through the real API — completes without a developer**; adversarial **141 checks — no breach found: 2 OEMs, 3 factories, every boundary held** (all 2026-09-20) | The OEM's own edge agent and real PLC drivers are simulated (handbook ch. 31) |
| 3 | **Owner journey** | `audit_owner_questions.py`: the owner's eight questions on four factory shapes (healthy priced, in trouble unpriced, partial, brand new) through the same functions the routes call | CI backend job, every push | **ALL 165 CHECKS PASSED** (2026-09-20) | A human's reading of the screens; the frontend is covered by Vitest and the e2e job, not here |
| 4 | **Copilot journey** | `copilot_eval` (135 questions + 144 adversarial prompts, three factories × three roles), `test_copilot_*` (orchestrator, tools, evidence, grounding, local provider, plan budget, model adoption) | Evaluation by hand (GPU); suites in CI every push | AMP's own engine: core routing 69/69, unseen 60/66, factual 93/93, grounded 279/279, refusals 14/14, money fabrications 0, disclosures 0, p50 4 ms. `qwen3:8b` (self-hosted): 69/69, 63/66, 93/93, 279/279, 14/14, 0, 0, p50 8.2 s — promoted on that set, reproduced exactly on a second run (2026-09-20, `AMP-NATIVE-MODEL-ACCEPTANCE.md`). On the 150-question set that added follow-ups (ADR-0035, later the same day): 75/78, 69/72, 96/96, 339/339, 0 disclosures — **not re-promoted**; the committed record is the failing one and the rules answer everywhere (model report §10) | Production has no GPU and no `AMP_LLM_BASE_URL`: the promoted model runs on the founder's laptop; production answers from the rules |
| 5 | **AI adversarial testing** | The 144 adversarial prompts against AMP's engine and four real models (576 model prompts + 144 rules prompts = 720); `audit_adversarial_final.py`; `audit_oem_contracts_adversarial.py` | Evaluation by hand; the two audits in CI every push | **Zero unauthorized disclosures across all 720 prompts**; "NO BYPASS FOUND ACROSS ALL SIX FIXES"; **ALL 45 ATTACKS REFUSED** (2026-09-20) | The question set is AMP-authored; a customer's own phrasing has not been tried |
| 6 | **Tenant isolation** | `audit_isolation.py` (every scoped model, read and written across tenants, NULL-tenant rows), `audit_three_customers.py --pg` (three tenants sharing every identifier), item 1, and 35 mutation harnesses whose 1,014 anchors are checked by `test_mutation_anchors_apply.py` | CI migration-gate job (PostgreSQL) and backend job, every push | PostgreSQL 18.3 today: `audit_isolation` — **no cross-tenant exposure found across reads, writes, every scoped model and NULL-tenant rows**; `audit_three_customers --pg` — **three customers, identical identifiers, no crossover**; anchors **1,014 in 35 harnesses, all apply** (2026-09-20) | Postgres row-level security is not layered on top (ADR-0002 lists it as later work) |
| 7 | **OEM consent** | `test_oem_service_consent.py`, `audit_oem_contracts_adversarial.py`, the ADR-0033 disclosure floor incl. §7 complementary suppression (`mutate_oem_intelligence` 20/20), `mutate_oem_sharing` 28/28 | CI backend job, every push | All green (2026-09-20); 45 attacks refused | Consent is enforced by AMP code; no legal review of the consent text is claimed |
| 8 | **PostgreSQL** | CI job "Migration gate (PostgreSQL)": migrate-before-serve, `verify_pg_*` scripts, and the four audits above on a real server | CI, every push; local PostgreSQL 18.3 today | Green on master; local re-runs today (items 2 and 6) | Production's PostgreSQL version and size differ from the CI service; `/readiness` reports the migration head in production (item 12) |
| 9 | **Performance** | `backend/loadtest.py` (real uvicorn, disposable PostgreSQL, 10/50/250/1000 machines), `backend/dashboard_perf.py` (statements per endpoint), `backend/audit_perf.py` (read-model latency) | By hand; recorded in `backend/loadtest_results.json` and `docs/PERFORMANCE.md` | **Re-run today (2026-09-20 13:12 UTC, PostgreSQL 18.3, 8 requests in flight).** At 1,000 machines: `/analytics/executive-oee` p50 436 ms (was 575 on 2026-09-03), p95/p99 622/678 ms; `/analytics/summary` p50 306 (was 466); `/inventory/items` 68 (was 163); `/machines` 178 (was 184); `/oee/summary` 42 (was 40); every other endpoint p50 under 51 ms; **zero errors at every scale**; WebSocket fan-out 1,306 frames/s to 1,000 sockets; MQTT ingest 44,527 msg/s. Absolute p50 equal or lower than the 2026-09-03 run at every endpoint and scale; p95 at 50 machines is 20–50% higher (e.g. `/machines` 90 → 127 ms) while p50 fell — the driver's floor-normalised comparison flags this because its own client floor fell 22–37% (9.1 → 5.7 ms), not because service time rose | **The k6 baseline tables in `docs/PERFORMANCE.md` remain UNMEASURED** (k6 is not installed; the Python driver reports its own floor beside every number and the p50s above are queueing under 8 concurrent callers, not one user's latency — one user waits ~54 ms on the slowest endpoint). No concurrent-user p95 on production hardware |
| 10 | **Recovery** | Nightly off-box `pg_dump` and a restore into a throwaway PostgreSQL (`.github/workflows/backup.yml`, job `restore-drill`) | GitHub Actions schedule, 02:17 UTC daily | Last run **2026-09-20 07:46 UTC, success**; measured RTO **7.47 s** for a small dataset with the dump already local (2026-08-09), re-measured 2026-09-21 with `restore_drill.py --scale`: **7.73 s** at demo size and **9.23 s** at 2,200 machines / 66,000 production records / a 5 MB dump (restore 1.25 s, migrate 1.47 s, boot 3.83 s), and **11.06 s** for a year of production on the same plant (803,000 records, a 57 MB dump; restore 3.20 s) | RTO on a production-sized dump has not been timed; the backup interval (daily) bounds the data loss, and nothing shorter is claimed |
| 11 | **Model-outage test** | `test_copilot_local_provider.py` and `test_llm_plan_budget.py`, against the real provider over HTTP: runtime stopped, model missing, timeout, invalid JSON, planning budget exhausted, malformed tool request, hallucinated wording, wrong tool, context too large | CI backend job, every push | All nine degrade to AMP's own engine with the failure named in `/ai/status`; the core MES is untouched (the Copilot is a read path). Context too large: declined in **4 ms** before sending (was 36 s of silent truncation) — ADR-0034 §7 | Behaviour on a CPU-only SME box has not been timed |
| 12 | **Production smoke test** | After every merge: `/health` version equals the merge SHA, `/readiness` 200, frontend 200, protected endpoints refuse an unauthenticated call | By hand, every merge (`CHIEF-ENGINEER-STATE.md`) | Master `5e8b863`, 2026-09-20: `/health` `{"status":"ok","database":"ok","schema":"ok","version":"5e8b863"}` three seconds after the deploy, `/readiness` 200, frontend 200, `/ai/status` and `/platform/status` 401 unauthenticated. The same check passed at `b8cffca` (#661) and `c355457` (#663) earlier today | A smoke test is not a customer's day; the load and recovery items say what else is known |

## 2b. The brief's other requirements, and where each is pinned

| Brief | Requirement | Where it is true, and the test that keeps it so |
|---|---|---|
| §7 | A real Copilot conversation | `POST /ai/ask` and `/copilot/ask` through one orchestrator (ADR-0022/0023); the 135-question evaluation is a conversation's worth of real questions across three factories and three roles |
| §8 | Conversation memory must not bypass authorization | There is no conversation memory: the request is `{question}` alone (`AICopilot.tsx` sends nothing else; `ai_ask(payload)` reads nothing else), so every question is authorized from scratch — role, tenant, licence, arguments — by `run_tool`, and nothing a previous turn established survives into the next. Pinned by `test_copilot_tools_no_wider_than_routes.py` and the tenant/OEM isolation cases of the evaluation |
| §9 | PROPOSE → VALIDATE → AUTHORIZE → HUMAN APPROVAL → EXECUTE → AUDIT; never a silent purchase | Agents propose `AgentAction` rows into a pending state; `approvals.py` re-checks the database, not the token, before anything executes (ADR-0015, `test_approval_gate.py`, `mutate_approval_gate.py`); only Reorder auto-approves by default and only into a **Draft** purchase order, never a placed one; every decision is an audit row (ADR-0029 measures what the action then changed) |
| §10 | Local model failure tests | Item 11 above — nine failure modes, all against the real provider |
| §11 | Observability without secrets | One structured `copilot` log line per answer — engine, provider, model, tools with state and timing, gate verdict and reasons, token counts — and never the question, the answer, the evidence or the tenant; the redactor still blanks any key containing `token`, `password`, `secret` or `authorization` (`test_copilot_orchestrator.py` §8 through the real `JsonFormatter`; ADR-0034 §5) |
| §12 | Model versioning; a change requires re-evaluation | The adoption record names the runtime (host and `/v1/models` entry), the configuration (planning budget, timeout, temperature) and the question set (`cases.py` digest, counts, harness commit); `test_copilot_model_adoption.py` re-derives every committed record's verdict from its own numbers on every CI run, so a record whose numbers no longer pass the gate fails the build (ADR-0034 §6) |
| §13 | Promotion gate; no default merely because it works | `qwen3:8b` was promoted by the gate, not by hand, on the 135-question set — and was **not** re-promoted when follow-ups enlarged the set (one core case three times; model report §10). The committed record is the failing one, so no model is adopted and the rules answer; a model is used only where `AMP_LLM_BASE_URL` names a runtime AND the record passes, and production has neither |
| §14 | AMP must run with no Gemini or other hosted key | `test_copilot_local_provider.py` §4b: with no hosted key anywhere the resolved provider is the self-hosted one, `/ai/ask` and `/ai/report` answer, and a hallucinated brief is refused with a note. The hosted providers' code remains; nothing requires it |
| §16 | RULE-BASED / STATISTICAL / ML MODEL / LLM-ASSISTED, never marketed as another | Handbook ch. 31 "Every intelligence engine, classified", held to the model cards by `test_intelligence_classification.py` (#663) |
| §17 | The failure-risk model is unproven on real failure data — say so | Said in the model card's caveat, the handbook table, the Current Reality row, and §3 below; no screen, brief or agent uses the model |

## 3. What is not proven, in one place

- **The failure-risk model has not been proven on real factory failure data.**
  It is adopted against the rule scorer on synthetic machines only, shown on
  its own card with that caveat, and used by no screen, brief or agent. Its
  accuracy on a real plant is unknown until a real plant's failures are held
  out and scored, which no customer has yet consented to (handbook ch. 31,
  "Every intelligence engine, classified").
- **No model is adopted, and production runs none.** Railway has no GPU and
  no `AMP_LLM_BASE_URL`; `qwen3:8b` passed the first question set and did not
  re-earn the gate on the set that includes follow-ups (model report §10),
  so AMP's engine answers everywhere, and every acceptance number for the
  rules path was measured in that configuration.
- **Direct PLC protocols are simulated** behind a ready adapter interface;
  telemetry arrives over MQTT/HTTP from publishers, not from a driver.
- **The k6 load baselines are unmeasured**; the Python load driver's numbers
  are recorded with their date and their own client floor.
- **Recovery has been timed at demo size, at a month of production and at a
  year of production** for 2,200 machines (803,000 production records, a 57 MB
  dump: RTO 11.06 s, against 9.23 s for a month and 7.73 s at demo size, all
  locally with the dump on disk — closure document §8). Not timed: the
  artifact download, which depends on the recovering machine's network.
- **The evaluation questions are AMP-authored.** Zero disclosures across 720
  adversarial prompts is a property of the architecture (the model never sees
  a tenant and never decides authorization), confirmed by four models; it is
  not a claim about every sentence a human could type.

## 4. Verdict

**Release candidate: YES** for the sixteen differentiators the sprint set out
to build, on the evidence above, with §3 attached to any customer conversation.
What would change the verdict: a red run of any CI audit named in §2; a
disclosure in any future evaluation run (the gate is pass/fail, never averaged);
or a production `/health` that does not match master.

## 5. Re-running everything

```bash
cd backend
DATABASE_URL="sqlite:///./audit.db" python audit_three_factory_simulation.py
DATABASE_URL="sqlite:///./audit.db" python audit_owner_questions.py
DATABASE_URL="sqlite:///./audit.db" python audit_oem_demo_journey.py
python audit_three_customers.py --pg            # borrows local PostgreSQL credentials
python audit_oem_adversarial.py 5432
python audit_oem_pilot_journey.py 5432
python -m copilot_eval --provider local --record F --cases G   # needs AMP_LLM_BASE_URL
python test_mutation_anchors_apply.py
```

The CI workflow (`.github/workflows/ci.yml`) runs every audit that needs no GPU
on every push; the nightly backup workflow runs the restore drill.
