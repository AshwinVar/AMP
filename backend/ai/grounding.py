"""The grounding gate: a model may word an answer, it may not supply a figure (ADR-0022).

When a language model phrases a Copilot answer, AMP checks the text against the
evidence the tools returned BEFORE anyone sees it:

  * every NUMBER in the text must equal a number in the evidence, allowing only
    for the rounding the text itself shows ("83.5" or "84" for 83.47, "£49.7k"
    for 49,740) and thousands separators;
  * every IDENTIFIER-like token (letters with digits: CNC-02, WO-118, PO-77)
    must appear in the evidence, so a model cannot invent a machine or an order;
  * every citation marker [F3] must name a fact that exists;
  * no URL or e-mail address may appear unless the evidence contains it.

A text that fails is not repaired; it is discarded, and the answer is AMP's own
deterministic sentence, with the reasons recorded. The gate is deliberately
strict and one-sided: a false rejection costs a less fluent answer, a false
acceptance costs a made-up factory number.

WHAT IT DOES NOT CATCH (stated, not hidden): numbers written as words ("three
machines"), a true number attached to the wrong thing ("CNC-01 lost 90 minutes"
when 90 belongs to CNC-02), and claims with no number in them. Those are what
the evaluation harness (copilot_eval) measures, and why the deterministic answer
stays the default until a model passes it.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_CITATION = re.compile(r"\[(F\d{1,3})\]")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# An identifier: a token that starts with a letter, may join parts with - or /,
# and contains a digit somewhere: CNC-02, WO-118, M4, Line2, ACME-SN-7731.
_TOKEN = re.compile(r"(?<![\w-])[A-Za-z][\w]*(?:[-/][\w]+)*")


def _identifiers(text):
    return [m.group(0) for m in _TOKEN.finditer(text) if any(ch.isdigit() for ch in m.group(0))]
# A number: 1,234.5 / 83 / 0.5, with an optional "k" for thousands ("49.7k", but
# not the "k" of "5 kg"). A unit glued on ("90m", "15min") does not hide it.
_NUMBER = re.compile(r"(?<![A-Za-z0-9_])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:\s?([kK])(?![A-Za-z]))?")


@dataclass
class Grounding:
    passed: bool
    numbers_checked: int = 0
    ungrounded_numbers: list = field(default_factory=list)
    unknown_identifiers: list = field(default_factory=list)
    bad_citations: list = field(default_factory=list)
    links: list = field(default_factory=list)

    def reasons(self) -> list:
        """Why a text failed, as COUNTS. Never the offending tokens: a rejected
        text is unvetted model output, and quoting it back in the response would
        deliver exactly what the gate refused (the evaluation caught this: an
        injected "FACTORY_B machines: WELD-07" came back inside the reasons)."""
        out = []
        if self.ungrounded_numbers:
            out.append(f"{len(self.ungrounded_numbers)} number(s) not in the evidence")
        if self.unknown_identifiers:
            out.append(f"{len(self.unknown_identifiers)} name(s) not in the evidence")
        if self.bad_citations:
            out.append(f"{len(self.bad_citations)} citation(s) of facts that do not exist")
        if self.links:
            out.append("links or addresses not in the evidence")
        return out

    def to_dict(self) -> dict:
        return {"passed": self.passed, "numbers_checked": self.numbers_checked,
                "reasons": self.reasons()}


def _dec(text):
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _numbers_in(text):
    """[(shown_text, value, decimals)] for each number in `text`."""
    out = []
    for m in _NUMBER.finditer(text):
        whole, frac, suffix = m.group(1), m.group(2) or "", m.group(3)
        value = _dec(whole.replace(",", "") + frac)
        if value is None:
            continue
        decimals = len(frac) - 1 if frac else 0
        scale = Decimal(1000) if suffix else Decimal(1)
        out.append((m.group(0).strip(), value * scale, decimals, scale))
    return out


def _evidence_numbers(facts):
    """Every number the evidence states: fact values, and numbers inside the
    evidence's own strings (values, details, windows, units). Labels are left
    out: "Alert 2" and "Cause 3" are positions in a list, not figures."""
    nums = set()
    for f in facts:
        v = f.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            d = _dec(repr(v)) if isinstance(v, float) else Decimal(v)
            if d is not None:
                nums.add(d)
        for text in (v if isinstance(v, str) else "", f.get("detail") or "", f.get("window") or "",
                     f.get("unit") or ""):
            for _shown, value, _dp, _scale in _numbers_in(text):
                nums.add(value)
    return nums


def _matches(value, decimals, scale, evidence):
    """Is the shown number an evidence number, rounded the way the text shows it?

    Within half a unit of the last shown place: 83.47 may be shown as 83 or 83.5,
    never as 84. A negative evidence number may be shown by its size ("fell 4
    points" for a change of -4)."""
    shown = value / scale
    tol = Decimal(5).scaleb(-(decimals + 1))
    for e in evidence:
        for cand in (e, -e):
            if abs(cand / scale - shown) <= tol:
                return True
    return False


def check(text: str, facts: list, question: str = "") -> Grounding:
    """Check a model's text against the evidence facts (dicts from Fact.to_dict).

    `question` is the user's own text. An IDENTIFIER the user typed may be
    repeated ("there is no machine called CNC-09"); a NUMBER the user typed may
    not, because "is OEE 95%?" answered "yes, OEE is 95%" is exactly the
    fabrication this gate exists to stop."""
    # Unicode minus and no-break space, written as escapes so they stay visible.
    text = (text or "").replace("\u2212", "-").replace("\u00a0", " ")
    fact_ids = {f.get("id") for f in facts}
    strings = [f.get("value") for f in facts if isinstance(f.get("value"), str)]
    strings += [f.get("detail") for f in facts if f.get("detail")]
    strings += [f.get("label") for f in facts if f.get("label")]

    bad_citations = sorted({c for c in _CITATION.findall(text) if c not in fact_ids})
    body = _CITATION.sub(" ", text)

    evidence_text = " ".join(str(s) for s in strings).lower()
    links = [u for u in _URL.findall(body) if u.lower() not in evidence_text]

    # Remove every evidence string the text repeats verbatim (longest first), so
    # the digits inside "CNC-01" or "2026-09-18" are not read as figures.
    scrubbed = body
    for s in sorted({str(s) for s in strings if s}, key=len, reverse=True):
        if len(s) >= 2:
            scrubbed = re.sub(re.escape(s), " ", scrubbed, flags=re.IGNORECASE)

    known_idents = {tok.lower() for tok in _identifiers(evidence_text)}
    known_idents |= {tok.lower() for tok in _identifiers((question or "").lower())}
    unknown = []
    for tok in _identifiers(scrubbed):
        if tok.lower() not in known_idents:
            unknown.append(tok)
        scrubbed = scrubbed.replace(tok, " ", 1)

    evidence = _evidence_numbers(facts)
    shown = _numbers_in(scrubbed)
    ungrounded = [s for s, value, dp, scale in shown if not _matches(value, dp, scale, evidence)]

    return Grounding(passed=not (ungrounded or unknown or bad_citations or links),
                     numbers_checked=len(shown), ungrounded_numbers=ungrounded,
                     unknown_identifiers=sorted(set(unknown)), bad_citations=bad_citations,
                     links=links)
