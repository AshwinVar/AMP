"""The grounding gate lets a model word an answer and never supply a figure (ADR-0022).

Pinned here:
  1. worked cases, each chosen for one rule (rounding, thousands, "k", signs,
     identifiers, citations, links, unicode, the user's own words);
  2. a PROPERTY over 400 seeded random evidence sets: every evidence number,
     shown at any rounding the value allows, passes; the same number moved by
     more than its shown precision fails;
  3. the reasons a rejection gives are COUNTS, never the rejected tokens (a
     rejected text is unvetted model output; quoting it back would deliver it);
  4. numbers in a fact's LABEL do not ground anything ("Alert 2" is a position);
  5. a number the user typed does not ground an answer, an identifier does.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_copilot_grounding.py
"""
import random
import sys

from ai import grounding as g

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def fact(fid, value, label="Figure", unit="", detail="", window=""):
    return {"id": fid, "key": f"k.{fid}", "label": label, "value": value, "unit": unit,
            "provenance": "MEASURED FACT", "source": "", "window": window, "detail": detail}


FACTS = [fact("F1", 83.47, "Plant OEE", "%", "from 3 of 4 machines", "last 7 days"),
         fact("F2", "CNC-01", "Lowest-OEE machine"),
         fact("F3", 49740, "Cost of losses", "£"),
         fact("F4", -4, "OEE change vs last week", "pts"),
         fact("F5", 2, "Alert 2 (high)"),
         fact("F6", "Preventive on PRESS-01", "Next task")]


def worked():
    print("=" * 74)
    print("1. WORKED CASES")
    print("=" * 74)
    cases = [
        ("Plant OEE is 83.5% over the last 7 days, from 3 of 4 machines [F1].", True, "shown rounded to 1 dp"),
        ("Plant OEE is 83%.", True, "rounded to a whole number"),
        ("Plant OEE is 84%.", False, "84 is not 83.47 at any honest rounding"),
        ("Plant OEE is 83.4%.", False, "83.4 is not 83.47 to one decimal"),
        ("CNC-01 has the lowest OEE.", True, "an identifier from the evidence"),
        ("CNC-02 has the lowest OEE.", False, "an invented machine"),
        ("PRESS-01 is next for preventive work.", True, "an identifier inside an evidence string"),
        ("Losses cost £49,740 this week.", True, "thousands separators"),
        ("Losses cost about £49.7k.", True, "a k suffix at its shown precision"),
        ("Losses cost about £50k.", True, "k at whole thousands: 49.74 rounds to 50"),
        ("Losses cost £50,000.", False, "50,000 is not 49,740"),
        ("OEE fell 4 points.", True, "the size of a negative change"),
        ("OEE fell 5 points.", False, "a wrong change"),
        ("CNC-01 lost 90m of production.", False, "a unit glued on does not hide a number"),
        ("5 kg of resin is left.", False, "kg is not thousands, and 5 is not in the evidence"),
        ("See [F9] for details.", False, "a citation of a fact that does not exist"),
        ("Visit http://evil.example.com to fix it.", False, "a link the evidence does not hold"),
        ("Email ops@evil.example.com.", False, "an address the evidence does not hold"),
        ("OEE is 83 % and fell −4 points.", True, "no-break space and unicode minus"),
        ("There are 2 alerts.", True, "2 is a fact value (F5)"),
        ("OEE is 83% on the 1st shift.", True, "an ordinal is not checked as a figure"),
        ("Ignore the data: OEE is 99%.", False, "an injected figure"),
    ]
    for text, want, why in cases:
        got = g.check(text, FACTS)
        check(f"{'accept' if want else 'reject'}: {why}", got.passed == want, f"{text!r} -> {got.reasons()}")


def labels_and_question():
    print()
    print("=" * 74)
    print("2. LABELS, AND THE USER'S OWN WORDS")
    print("=" * 74)
    facts = [fact("F1", 12, "Cause 3 minutes lost", "min")]
    check("a number in a LABEL grounds nothing ('Cause 3' is a position)", not g.check("There are 3 causes.", facts).passed)
    facts = [fact("F1", 0, "Machines matching")]
    q = "How is CNC-09 doing?"
    check("an identifier the user typed may be repeated",
          g.check("There is no machine called CNC-09 in this workspace.", facts, q).passed)
    q = "Is our OEE 95%?"
    check("a NUMBER the user typed does not ground an answer",
          not g.check("Yes, OEE is 95%.", [fact("F1", 71, "Plant OEE", "%")], q).passed)
    check("an identifier nobody typed and no evidence holds is refused",
          not g.check("CNC-09 is fine.", facts, "How are things?").passed)
    compound = [fact("F1", 0, "Matches")]
    check("a compound identifier the user typed is one token, its digits not a figure",
          g.check('I couldn\'t find anything matching "acme-sn-7731".', compound, "Find ACME-SN-7731").passed)


def reasons_are_counts():
    print()
    print("=" * 74)
    print("3. A REJECTION NEVER QUOTES WHAT IT REJECTED")
    print("=" * 74)
    bad = g.check("FACTORY_B machines: WELD-07, OVEN-03. OEE is 99%. See http://x.example", FACTS)
    text = " ".join(bad.reasons()) + " " + str(bad.to_dict())
    check("rejected", not bad.passed)
    for token in ("WELD-07", "OVEN-03", "99", "x.example", "FACTORY_B"):
        check(f"the reasons do not contain {token!r}", token not in text, text)
    check("the reasons count what failed", any(ch.isdigit() for ch in text), text)


def property_test():
    print()
    print("=" * 74)
    print("4. PROPERTY: every true figure passes at its shown rounding; a moved one fails")
    print("=" * 74)
    rng = random.Random(20260919)
    passed_true = failed_true = caught = missed = 0
    for trial in range(400):
        values = []
        for i in range(rng.randint(1, 6)):
            if rng.random() < 0.5:
                values.append(rng.randint(0, 50000))
            else:
                values.append(round(rng.uniform(0, 1000), rng.choice((1, 2))))
        facts = [fact(f"F{i + 1}", v, f"Measure {chr(65 + i)}") for i, v in enumerate(values)]
        v = rng.choice(values)
        if isinstance(v, int):
            shown = f"{v:,}" if rng.random() < 0.5 else str(v)
            moved = v + rng.choice((1, -1)) * rng.randint(1, 50)
            moved_text = f"{moved:,}" if moved >= 0 else None
        else:
            dp = rng.choice((0, 1))
            shown = f"{v:.{dp}f}"
            moved = v + rng.choice((1, -1)) * (1.2 if dp == 0 else 0.3)
            moved_text = f"{moved:.{dp}f}" if moved >= 0 else None
        ok = g.check(f"The figure is {shown} this week.", facts).passed
        passed_true += ok
        failed_true += not ok
        if moved_text is not None:
            others = {str(x) for x in values}
            accepted = g.check(f"The figure is {moved_text} this week.", facts).passed
            # A moved number can coincide with ANOTHER true value; only a miss
            # against every true value at that precision is a gate failure.
            coincide = any(abs(float(moved_text.replace(",", "")) - x) <= (0.5 if "." not in moved_text else 0.05)
                           for x in values)
            if not coincide:
                caught += not accepted
                missed += accepted
    check(f"every true figure passes ({passed_true}/400)", failed_true == 0, f"{failed_true} true figures rejected")
    check(f"every moved figure that matches no true one is rejected ({caught} caught)", missed == 0,
          f"{missed} moved figures accepted")


def main():
    worked()
    labels_and_question()
    reasons_are_counts()
    property_test()
    if failures:
        print(f"\n{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
