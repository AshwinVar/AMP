# AMP Native Model Acceptance Report

**Status:** in progress · **Started:** 2026-09-20 · **Authorised by:** the founder, 2026-09-20 ("APPROVED — PROCEED WITH THE REAL AMP NATIVE MODEL PHASE")

This report records what was **measured**. Nothing in it is a claim about a
model that the evaluation harness did not produce. Where a number is pending,
the row says so; where a candidate failed, the exact failure is named.

---

## 1. What "acceptance" means

A self-hosted model is promoted to word the Copilot's answers only when it
passes the gate in `copilot_eval/adoption.py`, run against the **same**
three-factory environment, the **same** 135 questions and the **same** 144
adversarial prompts as AMP's own rule engine, in the same process:

| Requirement | Threshold |
|---|---|
| Unauthorized disclosures (cross-tenant, cross-OEM, consent, authorization) | **0** — pass/fail, never averaged |
| Ungrounded texts shown to a reader | **0** |
| Money fabrications | **0** |
| Core tool selection | ≥ AMP's own engine (69/69) |
| Unseen tool selection | ≥ AMP's own engine (60/66) |
| Factual accuracy against the oracle | ≥ AMP's own engine (93/93) |

`test_copilot_model_adoption.py` re-derives every committed record's verdict
from its own metrics, so a record cannot be hand-edited to pass.

The model never decides authorization. AMP's `run_tool` checks tool existence,
role, licence, arguments and tenant before anything runs; the grounding gate
checks every number, identifier, citation and link in the model's wording
against the evidence AMP produced. A wording the gate refuses is replaced by
AMP's own sentence. This is the architecture the founder's brief specifies:

```
question -> model selects tools -> AMP authorizes -> deterministic evidence
         -> model words the evidence -> grounding gate -> reader
```

## 2. Environment

| | |
|---|---|
| Hardware | NVIDIA GeForce RTX 5070 Ti Laptop GPU, **12,227 MiB** VRAM (driver 592.82); 31.3 GB RAM |
| Runtime | Ollama **0.34.2**, portable zip, bound to **127.0.0.1:11434 only** |
| Context window | 16,384 tokens (`OLLAMA_CONTEXT_LENGTH`); AMP's 26-tool catalogue is ~2,300 tokens |
| AMP provider | `LocalOpenAIProvider` (ADR-0023) over OpenAI-compatible `/v1/chat/completions` — the runtime is replaceable |
| Planning budget | `AMP_LLM_PLAN_TOKENS=1200` (ADR-0034; the previous 300 routed 0/8) |
| Timeout | `AMP_LLM_TIMEOUT=240` for evaluation |
| Temperature | 0 |
| Evaluation set | `copilot_eval/cases.py` sha256 `72f4e4085486fbd9` — 45 questions × 3 factories, 16 adversarial × 3 factories × 3 roles; harness at `c7f4ca5` |

## 3. Candidates

Chosen for commercial licence (Apache 2.0 throughout), tool-calling support,
and fit inside 12 GB. Four, not forty.

| Model | Params | Quant | Size | Context | Licence | Tools | Why |
|---|---|---|---|---|---|---|---|
| **qwen3:8b** | 8.2B | Q4_K_M | 5.2 GB | 40,960 | Apache 2.0 | yes (+thinking) | strongest open tool-caller in class |
| **qwen3:4b** | 4.0B | Q4_K_M | 2.5 GB | 262,144 | Apache 2.0 | yes (+thinking) | the SME / CPU-practical case |
| **granite3.3:8b** | 8.2B | Q4_K_M | 4.9 GB | 131,072 | Apache 2.0 | yes | IBM, built for tool calling — a different training lineage |
| **mistral-nemo:12b** | 12.2B | Q4_0 | 7.1 GB | 1,024,000 | Apache 2.0 | yes | does more capacity buy routing accuracy inside 12 GB (note: Q4_0, a coarser quantisation than the others' Q4_K_M — stated so the comparison is read fairly) |

Llama 3.x was excluded on licence, not capability: its attribution and
acceptable-use terms are a constraint for a white-labelled product.

## 4. Measured results

Baseline is AMP's own rule engine, measured in the same run.

| Measure | AMP rules | qwen3:8b | qwen3:4b | granite3.3:8b | mistral-nemo:12b |
|---|---|---|---|---|---|
| Core tool selection | 69/69 | **69/69** | **69/69** | 63/69 (91%) | pending |
| Unseen tool selection | 60/66 (91%) | **63/66 (95%)** | **63/66 (95%)** | 60/66 (91%) | pending |
| Wrong tool | 6/135 | **3/135** | **3/135** | 12/135 | pending |
| Factual accuracy | 93/93 | **93/93** | **93/93** | 86/93 (92%) | pending |
| Answers grounded | 279/279 | **279/279** | **279/279** | **279/279** | pending |
| Honest data states | 14/14 | **14/14** | **14/14** | 12/14 | pending |
| Money fabrications | 0 | **0** | **0** | **0** | pending |
| **Unauthorized disclosures** | 0 | **0** | **0** | **0** | pending |
| Worded by the model | — | 237 | 191 | 255 | pending |
| Rejected by the grounding gate | — | 36 | 76 | 6 | pending |
| Ungrounded texts shown | — | **0** | **0** | **0** | pending |
| Latency p50 / p95 | 4 / 38 ms | **8.2 s / 17.5 s** | 14.1 s / 21.0 s | **1.6 s / 3.1 s** | pending |
| **Gate** | — | **PASSED** | **PASSED** | **NOT PASSED** | pending |

**granite3.3:8b, read by case rather than by count.** It fails on exactly two
questions, on all three factories: *losses* ("what are we losing?"), where it
calls `get_top_downtime_causes` instead of the losses tool, and *fpy*
("first-pass yield"), where it calls `get_top_downtime_causes` and
`find_record` before finally reaching `get_quality_summary` — so the seven
factual misses are all the quality oracle facts, and the two honest-state
misses follow from the same two questions (*losses* at the unpriced factory
worded as OK; *fpy* at the partial factory). Everything else it does well: it
does not think, so it is **five times faster** than qwen3:8b, its wording is
refused by the gate only 6 times in 279, and it discloses nothing. The gate is
right to refuse it — a model that mis-routes two of the owner's questions is
not an upgrade however fluent — and the failure is narrow enough that a tool
description tuned for "yield" and "losses" might change the verdict. That is
work on AMP's side, recorded here rather than done, because the candidate that
already passes needs no such help.

Two things the 4B run says that the gate does not: it is **slower** than the
8B on this GPU (it thinks longer before naming a tool), and the gate refused
its wording **twice as often** (76 of 267 against 36 of 273), so AMP fell back
to its own sentence more. Routing and safety are identical; fluency is not.

### Security, pass/fail

| | qwen3:8b | qwen3:4b | granite3.3:8b | mistral-nemo:12b |
|---|---|---|---|---|
| Cross-tenant disclosure | **0** | **0** | **0** | pending |
| Cross-OEM disclosure | **0** | **0** | **0** | pending |
| Consent / authorization bypass | **0** (AMP decides, never the model) | **0** | **0** | pending |

## 5. What the first model taught AMP (ADR-0034)

The first evaluation attempt scored qwen3:8b at **0/8** on the owner's own
questions. The model was fine. AMP allowed 300 tokens for planning, chosen
when the only models present were scripted stubs; a reasoning model spends
321–870 tokens thinking before it names a tool, and at 300 it returned nothing
— indistinguishable from a refusal. Fixed by a measured, configurable budget,
and by carrying `finish_reason` out so a truncated plan is *reported* as
truncated. Two other fixes were measured and rejected: Ollama 0.34.2 ignores
`chat_template_kwargs.enable_thinking`, and `/no_think` is a vendor token AMP
should not learn.

## 6. Reproducibility

Pending: a second full run of the leading candidate with `--cases`, to record
run-to-run variance at temperature 0 and to list the wrong-tool questions and
gate-rejected wordings by name rather than by count.

## 7. Honest limits

- Measured on one laptop GPU at one quantisation. A CPU-only SME box will be
  slower; the latency figures do not transfer.
- The gate measures correctness and safety against AMP's oracle. It does not
  measure whether a manufacturer *prefers* the model's wording.
- Zero disclosures across 144 adversarial prompts is a measurement on those
  prompts, not a proof. The adversarial set is versioned for that reason.
- The failure-risk and anomaly models are unaffected by any of this and remain
  unvalidated on real factory data (ADR-0020, ADR-0027, ADR-0032).
