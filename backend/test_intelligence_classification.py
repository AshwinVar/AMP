"""Every intelligence engine is sold as what it is, and the handbook cannot drift from the code.

The founder's rule for the sprint: distinguish RULE-BASED, STATISTICAL, ML MODEL
and LLM-ASSISTED engines, and do not market one as another. The place a
prospect or a diligence engineer reads that distinction is Chapter 31 of the
founder's handbook ("Every intelligence engine, classified"). The place the
distinction is TRUE is the code: the three native model cards (adopted or not,
synthetic-only caveat), the adopted-LLM record, and the provenance vocabulary
every Copilot figure carries.

This suite reads both and requires them to agree, the same way
test_copilot_evidence_vocabulary holds the backend and frontend vocabularies
together. So: adopt a model, retire one, promote a different LLM, or validate
the failure-risk model on a real plant, and this fails until the handbook says
so too. It also refuses the retired claims -- "no trained ML models exist",
"works only with an API key" -- anywhere in the training docs, because those
were true once and a stale sentence is the easiest way to market one thing
as another.

Run: DATABASE_URL="sqlite://" python backend/test_intelligence_classification.py
"""
import glob
import io
import json
import os
import re
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ai import evidence as ev  # noqa: E402
from amp_ai import registry  # noqa: E402

HANDBOOK = os.path.join(HERE, "..", "docs", "training", "AMP-FOUNDER-TECHNICAL-HANDBOOK.md")
TRAINING_DOCS = glob.glob(os.path.join(HERE, "..", "docs", "training", "*.md"))
COMPLETE = os.path.join(HERE, "..", "docs", "AMP-Complete-Documentation.md")
ADOPTED_LLMS = os.path.join(HERE, "ai", "adopted_models.json")

SECTION = "### Every intelligence engine, classified"
KINDS = ("RULE-BASED", "STATISTICAL", "ML MODEL", "LLM-ASSISTED", "MEASURED / DERIVED")

# Claims that were true once. Each is a way to market one kind of engine as
# another today, so none may appear in the training docs any more.
RETIRED = ("no trained ML models exist", "works only with an API key", "no trained ML yet",
           "**ML** (none yet)", "RULE-BASED** (not ML)")

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def table_rows(text):
    """{first cell: whole row} for the classification table, rows keyed by the
    text of their first cell up to the first parenthesis or colon."""
    start = text.find(SECTION)
    if start < 0:
        return {}
    body = text[start:]
    end = body.find("\n---")
    body = body[:end] if end > 0 else body
    rows = {}
    for line in body.splitlines():
        if not line.startswith("| ") or line.startswith("| Engine") or line.startswith("|---"):
            continue
        first = line[2:].split("|", 1)[0].strip()
        key = re.split(r"[(:]", first)[0].strip()
        rows[key] = line
    return rows


def main():
    text = io.open(HANDBOOK, encoding="utf-8").read()
    rows = table_rows(text)
    check("the handbook has the classification table", len(rows) >= 6, f"{sorted(rows)}")

    # ---- what the code says ------------------------------------------------
    cards = {name: registry.card(name) for name in registry.names()}
    check("the registry holds exactly the three native models",
          set(cards) == {"failure_risk", "telemetry_anomaly", "copilot_intent"}, str(sorted(cards)))
    for name, card in cards.items():
        check(f"{name}: its card verifies", card.get("available") is True, str(card.get("reason")))
        check(f"{name}: its card carries the synthetic-only caveat",
              "synthetic" in (card.get("caveat") or "").lower(), str(card.get("caveat")))
    adopted = {name: bool(card.get("adopted")) for name, card in cards.items()}
    llms = [m for m in json.load(io.open(ADOPTED_LLMS, encoding="utf-8"))["models"] if m.get("passed")]
    # A passed record means a promoted model; none means the rules answer
    # everywhere. Either is a truth the handbook must state (ADR-0035 §10 of
    # the model report: qwen3:8b passed one question set and not the next).
    check("at most one self-hosted LLM record has passed the gate", len(llms) <= 1,
          str([(m.get("provider"), m.get("model")) for m in llms]))
    llm_name = llms[0]["model"] if llms else None
    print(f"        passed self-hosted record: {llm_name or 'none -- no model is promoted on the current question set'}")
    check("the provenance vocabulary still distinguishes a rule from a model estimate",
          ev.RULE == "RULE-BASED ASSESSMENT" and ev.MODEL == "MODEL ESTIMATE")

    # ---- what the handbook says, row by row ---------------------------------
    def row(key):
        return rows.get(key, "")

    r = row("Failure-risk model")
    check("failure risk is classed ML MODEL", "**ML MODEL**" in r, r[:120])
    check("failure risk: the handbook's verdict matches the card",
          ("**Adopted**" in r) == adopted.get("failure_risk", False), f"card adopted={adopted.get('failure_risk')}")
    check("failure risk: synthetic-only, and not proven on real failures, is stated in the row",
          "synthetic" in r and "has not been proven on real factory failure data" in r)

    r = row("Telemetry anomaly check")
    check("the anomaly check is classed STATISTICAL", "**STATISTICAL**" in r, r[:120])
    check("anomaly check: the handbook's verdict matches the card",
          ("**Not adopted**" in r) == (not adopted.get("telemetry_anomaly", True)),
          f"card adopted={adopted.get('telemetry_anomaly')}")
    check("anomaly check: never an alarm, consent named", "never as an alarm" in r and "consent" in r)

    r = row("Copilot intent model")
    check("the intent model is classed ML MODEL", "**ML MODEL**" in r, r[:120])
    check("intent model: the handbook's verdict matches the card",
          ("**Not adopted**" in r) == (not adopted.get("copilot_intent", True)),
          f"card adopted={adopted.get('copilot_intent')}")

    r = row("Copilot planning and wording")
    check("the copilot's model use is classed LLM-ASSISTED", "**LLM-ASSISTED**" in r, r[:120])
    if llm_name:
        check(f"the promoted self-hosted model is named in the row ({llm_name})", f"`{llm_name}`" in r, r[:160])
    else:
        check("with no passed record, the row says no self-hosted model currently passes",
              "No self-hosted model currently passes" in r, r[:160])
    check("the row says production answers from the rules", "production answers from the rules" in r)
    check("the row says the model never invents a figure", "never invents a figure" in r)

    check("the rule-based engines are classed RULE-BASED", "**RULE-BASED**" in row("Rule-based engines"))
    check("the measured/derived arithmetic is classed as such", "**MEASURED / DERIVED**" in row("Connectivity cadence, OEE, losses in good units"))
    check("every kind in the vocabulary is used exactly by its rows",
          all(any(f"**{k}**" in r for r in rows.values()) for k in KINDS), str(KINDS))
    check("the rule is stated: do not market one as another", "**Do not market one as another.**" in text)

    # ---- the Current Reality table and the pitch ---------------------------
    reality = [ln for ln in text.splitlines() if ln.startswith("| Predictive maintenance |")]
    check("Current Reality: predictive maintenance names the rule AND the synthetic-only model",
          len(reality) == 1 and "RULE-BASED on every screen" in reality[0]
          and "has not been proven on real factory failure data" in reality[0])
    llm_row = [ln for ln in text.splitlines() if ln.startswith("| LLM copilot |")]
    check("Current Reality: the LLM copilot row says no hosted key is required and names production's engine",
          len(llm_row) == 1 and "no hosted key required" in llm_row[0]
          and "answers from AMP's own engine" in llm_row[0]
          and ((llm_name in llm_row[0]) if llm_name else ("no model currently promoted" in llm_row[0])),
          llm_row[0][:200] if llm_row else "no row")

    # ---- retired claims, anywhere in the training docs ---------------------
    for path in TRAINING_DOCS + ([COMPLETE] if os.path.exists(COMPLETE) else []):
        doc = io.open(path, encoding="utf-8").read()
        hits = [c for c in RETIRED if c in doc]
        check(f"{os.path.basename(path)}: none of the retired claims", not hits, str(hits))

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL INTELLIGENCE-CLASSIFICATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
