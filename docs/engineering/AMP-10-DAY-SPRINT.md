# AMP 10-Day Autonomous Differentiation Sprint

**To resume:** "Resume AMP 10-Day Autonomous Differentiation Sprint from CHIEF-ENGINEER-STATE.md and AMP-10-DAY-SPRINT.md."

The sprint's rule is that every claim needs matching proof:

- a claim about what AMP does needs running code and tests;
- a claim about AI reliability needs an evaluation;
- a claim about production needs a production check.

A feature that can't be built honestly is marked **BLOCKED** with the reason, and the sprint carries on.

---

## Tracking

| Field | Value |
|---|---|
| Sprint start | 2026-09-19 |
| Day | 10 (fifteen of the sixteen differentiators built, row 7 partial; the twelve acceptance items and their evidence are in `AMP-SPRINT-ACCEPTANCE.md`) |
| Master SHA at start | `eaf66c8` |
| Production SHA at start | `eaf66c8`. Checked 2026-09-19 00:00 UTC: `/health` ok, `/readiness` at migration `0010_outcome_contracts`, frontend returned 200. |
| Merged | #645 `d0d50aa` (typed tools, evidence, grounding gate, evaluation) · #646–#648 the provider, the adoption record and the orchestrator · #649 `04f9c02` Risk Radar · #650 `a1071d1` machine health explained · #651 `3e15d9c` Daily Brief · #652 `82ac27f` closed loop (migration `0011`) · #653 `e0671ca` shortage impact · #654 `ed0e226` proactive restraint · #655 `d7a93e3` anomaly sweep · #656 `564afac` OEM disclosure floor · #657 `baa35a5` the owner's eight questions across four factory shapes · #658 `feb7615` handover docs · #659 `f0da7e6` the complementary-suppression floor (ADR-0033 §7) · #660 `c7f4ca5` the grounding gate stops reading identifiers and dates as figures · #661 `b8cffca` the first real self-hosted model, `qwen3:8b` promoted (ADR-0034) · #663 `c355457` every intelligence engine classified, with a drift guard · #662 `5e8b863` the three-factory simulation audit and the simulator's tenant guard. |
| Open PRs | none — the sprint's last PR is this acceptance record. |
| Production status | healthy at `c355457`, verified 2026-09-20 within a minute of the #663 deploy: `/health` ok (database, schema, version), `/readiness` 200, frontend 200, `/ai/status` and `/ai/models/*` refuse an unauthenticated call (401). Earlier the same day at `baa35a5` (06:39 UTC) and `b8cffca`: the same, plus `/ai/native/anomaly/sweep`, `/oem/intelligence`, `/shortage-impact`, `/proactive` and `/action-outcomes` each refuse an unauthenticated call (401). |

## Day 1: baseline

- **Tests on master:** the backend sweep passed 323/323 files and the frontend vitest run 622/622 tests.
- **P0/P1:** one item is open, the GMATS credential rotation. It is a production credential change, so only the founder can do it; see the state doc. Nothing else is open.
- **AI stack map:** CHIEF-ENGINEER-STATE and ADR-0022 give the full picture. In summary:
  - There are two question paths. `/copilot/ask` uses keyword rules; `/ai/ask` sends a text snapshot to an external LLM.
  - There was no tool calling, no check on LLM output and no LLM evaluation.
  - The native intent model is built but not adopted.
  - The failure-risk model is adopted but has no UI.
  - The anomaly check is experimental and needs consent.
  - Agents go through a strong approval gate, but nothing measures whether an action helped.
- **Current Copilot benchmark (rule engine):**
  - 42/42 evaluation checks pass.
  - Unseen routing is 6/12.
  - Held-out routing is 15/26 on both holdout sets (58%).
  - LLM answers are not evaluated at all.
- **Hardware for local models:** RTX 5070 Ti Laptop (12 GB VRAM), 31 GB RAM. No LLM runtime is installed.

## Self-hosting choice

The runtime is any **OpenAI-compatible local endpoint**: Ollama, llama.cpp server or vLLM. AMP calls it through the provider registry that already exists in `ai_copilot.py`, which uses plain HTTP and needs no SDK.

| Candidate | Size | Licence | Why |
|---|---|---|---|
| Qwen3-8B | 5.2 GB (Q4) | Apache 2.0 | tool calling, JSON output, fits 12 GB VRAM |
| Qwen3.5-4B | 3.4 GB (Q4) | Apache 2.0 | smaller and faster; small models need strict schemas, which the typed tools provide |

**BLOCKED:** downloading the runtime and model weights needs the founder's permission. That is about 1 GB for the runtime and 3.4 GB plus 5.2 GB for the weights. It was asked on 2026-09-19 and has not been answered.

Until then:

- the orchestrator, the grounding gate and the evaluation are built and tested with scripted stand-ins;
- no claim is made about any real model;
- local inference does **not** become the default until a real model passes the evaluation gate: zero unauthorized disclosures, zero ungrounded answers shown, and tool selection at least equal to AMP's own engine.

## Differentiators

| # | Differentiator | Status | Evidence |
|---|---|---|---|
| 1 | Factory Command Centre | BUILT | ADR-0024: `GET /command-centre` and `CommandCentreSection`; problems ranked by good units not made, money only where a rate is set, `why` labelled, live stoppages first |
| 2 | AMP Native Copilot (self-hosted, provider abstraction) | BUILT · real model **MEASURED; promoted on the 135-question set, not re-earned on the 150-question set** (ADR-0035; model report §10) | ADR-0023 + ADR-0034: the founder approved the download on 2026-09-20. Four Apache-2.0 candidates through the unchanged `LocalOpenAIProvider` on Ollama 0.34.2 (loopback). **`qwen3:8b` passed the gate and is promoted** (`ai/adopted_models.json`): 69/69 core, 63/66 unseen (AMP's own: 60/66), 93/93 factual, 279/279 grounded, 0 money fabrications, 0 unauthorized disclosures, p50 8.2 s — reproduced exactly on a second run. qwen3:4b passed but is slower and refused twice as often; granite3.3:8b and mistral-nemo:12b did not pass (routing below AMP's own). Every figure and every failure by case: `AMP-NATIVE-MODEL-ACCEPTANCE.md`. Production has no GPU, so production still answers from AMP's own engine until an infrastructure decision. First lesson: AMP's 300-token planning budget scored a working model 0/8 |
| 3 | Tool-using AI, typed and authorized | DONE (26 tools) | `ai/tools/`, `test_copilot_tools.py`, `test_copilot_tools_no_wider_than_routes.py` |
| 4 | Evidence-backed answers with provenance labels | DONE (API and UI) | `ai/evidence.py`, `CopilotEvidence.tsx` |
| 5 | Daily Factory Brief | BUILT | ADR-0028: `GET /daily-brief`, the `get_daily_brief` tool and `DailyBriefSection` — seven sections quoting the engines that already answer each question, the window always stated, and a closing section listing what AMP could NOT see (coverage gaps, no unit value, no plan, unlogged stoppage reasons, no measured rate) |
| 6 | Root-Cause Explorer | BUILT | ADR-0025: `GET /root-cause`, the `explain_production_gap` tool and a card on Executive; measured mechanisms labelled CAUSE CONFIRMED / LIKELY CONTRIBUTOR / CORRELATED EVENT / INSUFFICIENT EVIDENCE, with the unexplained remainder stated |
| 7 | Financial loss intelligence (never fabricated) | PARTIAL | `get_financial_losses`, the Command Centre and the Root-Cause Explorer: every loss sized in good units, money only with a unit value, UNKNOWN otherwise, and figures never totalled across overlapping problems |
| 8 | Production Risk Radar | BUILT | ADR-0026: `GET /risk-radar`, the `get_production_risks` tool and a card on the Overview; every risk states its rule and threshold, likelihood is LIKELY/POSSIBLE/WATCH, and the tests fail on the word "probability" or an unqualified "machine learning" |
| 9 | Machine Health, every score explained | BUILT | ADR-0027: the scorer records every rule it runs; `/machine-health/{id}` carries `health_explanation` and `HealthExplanation.tsx` shows the arithmetic — points, reading and threshold per rule, the rules that passed, and the cap when it bites |
| 10 | Anomaly engine end-to-end | BUILT | ADR-0032: `GET /ai/native/anomaly/sweep` runs the same check over every machine and gives a REASON for each one it could not score (INSUFFICIENT HISTORY / NOT MEASURED / NOT CONFIGURED), never a zero. Consent asked once for the fleet; a refusal is the answer, not an empty list. Every score is MODEL ESTIMATE under MODEL NOT VALIDATED, and it deliberately does NOT feed the proactive bar — an unadopted model may not interrupt anyone |
| 11 | Failure-risk hardening; synthetic separate from real validation | BUILT | ADR-0027: the adopted model finally has a screen — per-machine estimates under its own card and the role-restricted `get_failure_risk` tool, always MODEL NOT VALIDATED, MODEL ESTIMATE provenance, the synthetic-only caveat on every row, the rule score beside it, and no number at all when the artifact does not verify |
| 12 | Smart inventory linked to production | BUILT | ADR-0030: `GET /shortage-impact`, the `get_shortage_risk` tool and a card on Inventory. Open work orders through the tenant's own BOM, stock allocated in due-date order, and the units that cannot be made. An item in no recipe gets NO figure and is listed with the reason. The Risk Radar adopts it, supplying the link ADR-0026 said was missing. Nothing is ordered: the demand figure sits beside the Reorder agent's draft |
| 13 | Closed-loop actions, with the result measured | BUILT | ADR-0029: migration `0011` and `action_outcomes`; an approval freezes the metric it was meant to move, the window is left to elapse before anything is judged, the reading is frozen once, and every change is labelled CORRELATION with the caveat attached. `GET /action-outcomes`, the `get_action_outcomes` tool and a card on the agent view |
| 14 | Proactive intelligence without alert spam | BUILT | ADR-0031: `GET /proactive` computes, `POST /proactive/send` is the only write. Three things interrupt — a machine stopped now, a LIKELY risk, an approved action whose metric got WORSE — and everything held back is reported with its reason (SAID RECENTLY / BELOW THE BAR / OVER THE CAP). Stable signatures, a 24h cooldown and a cap of 5, with no new table |
| 15 | OEM intelligence from consented data | BUILT | ADR-0033: `GET /oem/intelligence` and a card on the OEM portal. Counts come from the manufacturer's own shipment records; every cross-customer figure needs at least 2 CUSTOMERS (not machines) contributing, because an average over one customer is that customer's reading with a new label. A withheld figure is UNKNOWN with its reason, never a zero, and the per-model slice obeys the same floor |
| 16 | Honest data states | DONE (Copilot) | `ai/evidence.DATA_STATES`; empty stock is no longer called "healthy" and an empty plant is no longer "All 0 machines running" |

## AI benchmark (copilot_eval, three factories, 37 questions + 15 adversarial prompts × 3 factories × 3 roles)

AMP's own engine, re-measured on master at `baa35a5` (`python -m copilot_eval`,
2026-09-20 — 135 questions plus 144 adversarial prompts):

| Measure | Result |
|---|---|
| Tool selection, core questions | 69/69 |
| Tool selection, unseen questions | 60/66 (CI floor 27) |
| Factual accuracy against the oracle | 93/93 |
| Answers grounded in their own evidence | 279/279 |
| Honest data states | 14/14 |
| Money fabrications | 0 |
| **Unauthorized disclosures** | **0** |
| Latency p50 / p95 | 4 ms / 37 ms (in-memory SQLite, AMP side only) |

The six unseen questions AMP's own router misses are routed correctly by every
scripted model behaviour (66/66), which is the case for the model this sprint
has not been allowed to measure — and is stated here rather than averaged away.

The scripted model behaviours test what AMP does with a model; they are not a model. Every behaviour had zero disclosures and zero ungrounded text shown:

- faithful;
- hallucinating: all 246 texts rejected;
- wrong tool and scope injection: fell back to AMP's plan;
- cross-factory names: nothing echoed;
- timeout and garbage: AMP's answer exactly;
- injection-follower: all 246 rejected;
- flood: at most 4 tools ran.

**Real model: not measured.** It is blocked on the download permission above.

## Findings made by the evaluation, and fixed in this PR

1. **Rejection reasons quoted the rejected text.** The gate's reasons echoed the model's rejected tokens, including an injected "FACTORY_B machines: WELD-07". Reasons are now counts only.
2. **A model-chosen tool argument could reach AMP's own sentence.** It came through "no machine called X" and past the gate. Such sentences are now replaced, and plan echoes withhold values the user did not type.
3. **Empty stock was reported as healthy.** A workspace with no stock records was told "Stock is healthy". It now says there are no stock items set up.
4. **An empty plant was "all running".** It was told "All 0 machines are running"; it now says no machines are registered.
5. **The briefing's copilot sentence broke the coverage rule.** It stated plant OEE without coverage (OEE contract §4). It now says "measured from N of M machines" when coverage is partial.

## Blocked

| Item | Blocked on | Owner |
|---|---|---|
| ~~Local model runtime and weights (benchmark and default switch)~~ | **resolved 2026-09-20** — the founder approved the download; the runtime and the first candidates are installed and the evaluation is running (`AMP-NATIVE-MODEL-ACCEPTANCE.md`) | — |
| Production local-model server | an infrastructure and cost decision: Railway has no GPU | founder |
| GMATS credential rotation | a production credential change | founder |

## Next tasks

1. ~~**Provider adapter.**~~ Done in ADR-0023. `/ai/ask` now goes through the orchestrator, which also fixed its tenant. The model is used only once it is adopted. The adapter is tested over HTTP against a stub.
   - **Still open:** per-company consent before factory data goes to an **external** provider (Anthropic or Gemini). That is the next change.
2. ~~**Command Centre and Daily Brief**~~ (Day 5). ADR-0024 and ADR-0028.
3. ~~**Root-Cause Explorer and Risk Radar**~~ (Day 6). ADR-0025 and ADR-0026.
4. ~~**Machine Health, anomaly and failure-risk surfaced honestly**~~ (Day 7). ADR-0027 and ADR-0032.
5. ~~**Smart inventory, closed loop with a measured result, proactive alerts, OEM intelligence**~~ (Day 8). ADR-0030, ADR-0029, ADR-0031 and ADR-0033.
6. **Adversarial campaign, the three-factory and OEM journeys, release candidate** (Days 9–10). ← done: the twelve acceptance items, each with the script that proves it, where it runs, today's measured result and what it does not prove, are `AMP-SPRINT-ACCEPTANCE.md`.
   - **The real-model campaign (2026-09-20, founder-authorised).** Runtime
     installed on loopback; four Apache-2.0 candidates chosen for licence,
     tool calling and 12 GB VRAM. All four measured through the unchanged
     harness: **`qwen3:8b` passed and is promoted** (`ai/adopted_models.json`,
     reproduced exactly on a second run); qwen3:4b passed but is slower and
     refused twice as often; granite3.3:8b and mistral-nemo:12b did not pass
     on routing. Four models, 576 adversarial prompts, zero disclosures. The
     measured table and every failure by case: `AMP-NATIVE-MODEL-ACCEPTANCE.md`.
     What the real runtime found in AMP rather than in the models — a
     300-token planning ceiling, an ungated report path, a redactor blanking
     token counts, silent prompt truncation — is ADR-0034. Production has no
     GPU, so it still answers from AMP's own engine.
   - **Done: the owner's eight questions, across four factory shapes.**
     `audit_owner_questions.py` asks all eight of the questions this sprint
     promised an owner could answer, on four shapes in ONE process — healthy
     and priced, in trouble and UNPRICED, partially covered, and brand new —
     through the same functions the routes call. It runs in CI on every push.
     It exists for the rules that only hold BETWEEN surfaces and that no
     per-surface suite can hold: money appearing nowhere across nine screens
     for a factory with no unit value, and no factory's words on another's
     screen when four tenants share one database and the same machine names.
   - **Done: the three-factory simulation.** `audit_three_factory_simulation.py`
     runs main's exact tick sequence — work orders, telemetry, PLC poll,
     production, status, stock, quality, shifts, operators, the heartbeat and
     the escalation agent — for FACTORY_A, B and C from one loop, twenty
     rounds each, and proves it against the raw tables: every row a tick wrote
     belongs to the tenant it ran for (52 tenant-carrying tables), no row
     points at another tenant's machine, order, item or plan (40 parent
     links), each factory advanced and only where it had something to advance,
     nothing landed in DEFAULT or the OEM namespace, A and B issued the same
     document numbers from their own sequences, and each owner's surfaces
     carry only their own factory's words afterwards. It runs in CI on every
     push. It found one defect on the way: with **no** tenant bound, every
     tick read every tenant's rows and filed what it wrote under DEFAULT — 41
     rows in twelve unbound rounds, telemetry and inspections for FACTORY_B's
     machines under the demo tenant. The loop never runs unbound; the CLI and
     any direct caller did. Every tick now refuses with none bound
     (`factory_simulator._bound_tenant`, the heartbeat's ADR-0021 rule made
     universal), the CLI binds DEFAULT itself, and the guard is pinned per
     tick by `test_sim_ticks_need_a_tenant.py` and 16 mutations.
   - **It found two defects on its first run, both now fixed:** the shortage
     card reported a workspace with no stock at all as OK with zero units at
     risk (the "empty stock is healthy" defect, in a surface written after
     that lesson), and the proactive plan told a brand-new workspace it had
     "no problems and no risks" while its own state said NO DATA. Both have a
     test and a mutation; `mutate_shortage_impact` is now 19/19 and
     `mutate_proactive` 17/17.
   - Fifteen of the sixteen differentiators are BUILT. The one that is not is
     #2's *model*: the provider, the switch and the evaluation exist and are
     tested against stubs, and no real model has been measured because the
     download is still blocked on the founder.
   - #7 stays PARTIAL on purpose: money appears only where a unit value is set,
     and the sprint has added no way to invent one.
