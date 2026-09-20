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
| Day | 2 (Copilot foundation merged; self-hosted provider in review) |
| Master SHA at start | `eaf66c8` |
| Production SHA at start | `eaf66c8`. Checked 2026-09-19 00:00 UTC: `/health` ok, `/readiness` at migration `0010_outcome_contracts`, frontend returned 200. |
| Merged | #645 (`d0d50aa`): typed Copilot tools, evidence, grounding gate, evaluation. Production verified at `d0d50aa` on 2026-09-19 01:50 UTC: `/health` ok (database, schema), `/readiness` 200, frontend 200, `/copilot/ask` refuses an unauthenticated call (401). |
| Open PRs | the self-hosted provider, the adoption record, `/ai/ask` through the orchestrator (ADR-0023) |
| Production status | healthy. Current SHA: see CHIEF-ENGINEER-STATE.md after each merge. |

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
| 2 | AMP Native Copilot (self-hosted, provider abstraction) | BUILT, model BLOCKED | ADR-0023: `LocalOpenAIProvider` and `ai/llm.py` adapter, with an earned switch (`ai/adopted_models.json`); tested end to end over HTTP against a stub model; a real model is blocked on the download permission |
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
| 15 | OEM intelligence from consented data | NOT STARTED | — |
| 16 | Honest data states | DONE (Copilot) | `ai/evidence.DATA_STATES`; empty stock is no longer called "healthy" and an empty plant is no longer "All 0 machines running" |

## AI benchmark (copilot_eval, three factories, 37 questions + 15 adversarial prompts × 3 factories × 3 roles)

AMP's own engine, measured when this PR was written (`python -m copilot_eval`):

| Measure | Result |
|---|---|
| Tool selection, core questions | 69/69 |
| Tool selection, unseen questions | 36/42 (CI floor 27) |
| Factual accuracy against the oracle | 93/93 |
| Answers grounded in their own evidence | 246/246 |
| Honest data states | 14/14 |
| Money fabrications | 0 |
| **Unauthorized disclosures** | **0** |
| Latency p50 / p95 | ~6 ms / ~45 ms (in-memory SQLite, AMP side only) |

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
| Local model runtime and weights (benchmark and default switch) | founder permission to download | founder |
| Production local-model server | an infrastructure and cost decision: Railway has no GPU | founder |
| GMATS credential rotation | a production credential change | founder |

## Next tasks

1. ~~**Provider adapter.**~~ Done in ADR-0023. `/ai/ask` now goes through the orchestrator, which also fixed its tenant. The model is used only once it is adopted. The adapter is tested over HTTP against a stub.
   - **Still open:** per-company consent before factory data goes to an **external** provider (Anthropic or Gemini). That is the next change.
2. **Command Centre and Daily Brief** (Day 5), built from the tools and evidence.
3. **Root-Cause Explorer and Risk Radar** (Day 6), with CAUSE labels.
4. **Machine Health, anomaly and failure-risk surfaced honestly** (Day 7).
5. **Smart inventory, closed loop with a measured result, proactive alerts, OEM intelligence** (Day 8).
6. **Adversarial campaign, the three-factory and OEM journeys, release candidate** (Days 9–10).
