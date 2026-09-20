"""Mutation harness for the grounding gate (ADR-0022).

This module is the only thing standing between a model's sentence and a reader.
If it says a text is grounded, AMP shows that text; if it is wrong, AMP shows a
figure nobody measured. It had no mutation harness at all until this one.

What is fragile here is not the arithmetic -- it is the DEFINITION of "a number
the evidence states". Every mutation below widens or narrows that definition in
a way that changes no visible behaviour on a passing case:

  * the evidence side reading an identifier's digits as a figure, so a machine
    called WELD-07 licenses "OEE was 7%" (this was a real defect);
  * the evidence side reading a date's digits as figures, so 2026-09-18
    licenses 2026, 9 and 18;
  * the two sides disagreeing about dates, which turns the brief's own
    "ending 2026-09-20" into six ungrounded numbers (273/279 in evaluation);
  * a LABEL's number grounding an answer ("Alert 2" is a position, not a count);
  * the rounding tolerance widening, so 84 passes for 83.47;
  * a citation, a link or the user's own number slipping through;
  * a rejection quoting the tokens it rejected, which delivers exactly what it
    refused.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_grounding.py
"""
import io
import os
import subprocess
import sys

SUITES = ["test_copilot_grounding.py", "test_copilot_eval.py", "test_ai_evaluation.py",
          "test_copilot_orchestrator.py"]

G = os.path.join("ai", "grounding.py")

MUTATIONS = [
    # --- what counts as a figure, on the EVIDENCE side ----------------------
    ("an identifier's digits become a figure the evidence states", G,
     "    cleaned = _DATE.sub(\" \", text or \"\")\n"
     "    for tok in _identifiers(cleaned):\n"
     "        cleaned = cleaned.replace(tok, \" \", 1)\n"
     "    return _numbers_in(cleaned)",
     "    return _numbers_in(text or \"\")"),
    ("a date's digits become figures the evidence states", G,
     "    cleaned = _DATE.sub(\" \", text or \"\")",
     "    cleaned = (text or \"\")"),
    ("only the identifiers are stripped, not the date", G,
     '_DATE = re.compile(r"\\d{4}-\\d{2}-\\d{2}(?:[T ][\\d:.]+)?")',
     '_DATE = re.compile(r"(?!x)x")'),

    # --- the two sides disagreeing -------------------------------------------
    ("the answer side reads a date as figures while the evidence side does not", G,
     "    shown = _numbers_in(_DATE.sub(\" \", scrubbed))",
     "    shown = _numbers_in(scrubbed)"),

    # --- ordinals ------------------------------------------------------------
    ("an ordinal is checked as a figure", G,
     "        if _ORDINAL.match(text, m.end()):\n            continue",
     "        if False:\n            continue"),
    # The first version of this harness also planted "the ordinal rule swallows
    # a real decimal beside it", against a `not frac and not suffix` guard. It
    # SURVIVED -- because no text puts st/nd/rd/th straight after a decimal or a
    # k-suffix, and if one did ("the 1.5th percentile") skipping it would be
    # right. The conditions were over-specification, not a guard, so they were
    # deleted rather than given a contrived test. What DOES need pinning is the
    # word boundary: without it, the "th" of a following word swallows a figure.
    ("the ordinal rule stops requiring a word boundary", G,
     '_ORDINAL = re.compile(r"(?i)(st|nd|rd|th)\\b")',
     '_ORDINAL = re.compile(r"(?i)\\s*(st|nd|rd|th|ings)")'),

    # --- what the evidence contributes ---------------------------------------
    ("a LABEL's number grounds an answer", G,
     '        for text in (v if isinstance(v, str) else "", f.get("detail") or "", f.get("window") or "",\n'
     '                     f.get("unit") or ""):',
     '        for text in (v if isinstance(v, str) else "", f.get("detail") or "", f.get("window") or "",\n'
     '                     f.get("unit") or "", f.get("label") or ""):'),
    ("a fact's own value stops grounding anything", G,
     "        if isinstance(v, (int, float)) and not isinstance(v, bool):",
     "        if False:"),

    # --- the tolerance -------------------------------------------------------
    ("the rounding tolerance widens by a factor of ten", G,
     "    tol = Decimal(5).scaleb(-(decimals + 1))",
     "    tol = Decimal(5).scaleb(-decimals)"),
    ("a negative evidence number no longer grounds its own size", G,
     "        for cand in (e, -e):", "        for cand in (e,):"),

    # --- the other refusals --------------------------------------------------
    ("a citation of a fact that does not exist is accepted", G,
     "    bad_citations = sorted({c for c in _CITATION.findall(text) if c not in fact_ids})",
     "    bad_citations = []"),
    ("a link the evidence does not hold is accepted", G,
     "    links = [u for u in _URL.findall(body) if u.lower() not in evidence_text]",
     "    links = []"),
    ("an identifier nobody typed and no evidence holds is accepted", G,
     "            unknown.append(tok)", "            pass"),
    # The first version of this mutation appended the question to
    # `evidence_text`, and SURVIVED -- because `evidence_text` feeds identifiers
    # and links, never the NUMBER set. It planted no defect at all. This one
    # puts the question's numbers where they would actually do harm: "is OEE
    # 95%?" answered "yes, OEE is 95%" is the fabrication this gate exists for.
    ("a NUMBER the user typed grounds the answer", G,
     "    evidence = _evidence_numbers(facts)",
     "    evidence = _evidence_numbers(facts) | {v for _s, v, _d, _sc in _numbers_in(question or \"\")}"),

    # --- the rejection itself -------------------------------------------------
    ("the gate passes a text that failed one rule", G,
     "    return Grounding(passed=not (ungrounded or unknown or bad_citations or links),",
     "    return Grounding(passed=not (unknown or bad_citations or links),"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        if not os.path.exists(os.path.join(here, suite)):
            continue
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=here)
        if proc.returncode != 0:
            failed.append(suite)
    return failed


EXPECTED_SURVIVORS = {}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path), encoding="utf-8").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(SUITES)} suites green\n")
    print(f"{'mutation':<66} {'verdict':<10} caught by")
    print("-" * 108)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<66} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8",
                newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:26] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label]
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<66} {verdict:<10} {note}")
        if verdict == "SURVIVED":
            survived.append(label)

    dirty = [p for p, original in originals.items()
             if io.open(os.path.join(here, p), encoding="utf-8").read() != original]
    print()
    print(f"source files restored: {'yes' if not dirty else 'NO - DIRTY: ' + str(dirty)}")
    if dirty:
        return 3
    if survived:
        print(f"{len(survived)} MUTATION(S) SURVIVED - investigate each:")
        for s in survived:
            print("   *", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
