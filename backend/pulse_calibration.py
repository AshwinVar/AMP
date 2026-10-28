"""Per-machine pulse calibration — collapse N edges-per-cycle back to one count.

WHY THIS EXISTS. A cycle-counting gateway reports one edge per machine cycle. If
the signal it taps pulses more than once per cycle (a relay that fires several
times per mould cycle), or the input picks up electrical noise, the raw count
runs a whole multiple high and every figure built on it — output, OEE, the plant
board, the shift scorecard — inflates with it. AMP cannot reach the node to
re-wire it, so it divides the known multiple out at ingestion until the signal is
corrected on site. A no-op at the default of one edge per cycle.

TEMPORARY, AND IT CUTS BOTH WAYS. Each entry is a stopgap for one faulty install.
It MUST be removed — reset to one edge per cycle — the moment the signal is fixed
on site (e.g. an opto-isolator re-added), or it will then UNDER-count a node that
has started reading 1:1 again. The durable home for this is a
``machines.pulses_per_cycle`` column; this map is the bridge until that migration
can take a revision number (0017 is spoken for by an open PR). See
test_pulse_calibration.py.

NOT A TENANT BRANCH. The mechanism is generic — a divisor keyed by machine
identity — and an env override (``PULSE_CALIBRATION="TENANT:SITE:NAME=3,..."``)
extends or corrects it without a deploy. The one built-in entry below is data for
a known-faulty install, not product logic that treats a customer specially.
"""
import os

# (tenant_code, site, machine_name) -> edges the node emits per real cycle.
_DEFAULTS = {
    # Shrinidhi IMM-02: the shot-counter node over-counts ~3x on a noisy,
    # direct-wired GPIO input (the opto-isolator was removed). Measured steady at
    # ~3x for 6+ hours on 2026-10-28. Reset to 1 once the opto is back and the
    # count reads 1:1 against the HMI again.
    ("SHRINIDHI", "", "IMM-02"): 3,
}


def _parse_env(raw):
    """Parse PULSE_CALIBRATION="TENANT:SITE:NAME=N,..." into the same key shape.

    A blank site is written as two adjacent colons (``TENANT::NAME=2``). Anything
    malformed — wrong field count, a non-integer or a divisor below one — is
    skipped rather than raised, so a typo in an env var can never take ingestion
    down; the built-in table simply stands.
    """
    table = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, val = part.partition("=")
        bits = key.split(":")
        if len(bits) != 3:
            continue
        try:
            n = int(val)
        except ValueError:
            continue
        if n >= 1:
            table[(bits[0], bits[1], bits[2])] = n
    return table


def _table():
    merged = dict(_DEFAULTS)
    merged.update(_parse_env(os.environ.get("PULSE_CALIBRATION")))
    return merged


def pulses_per_cycle(machine):
    """Edges this machine's node emits per real cycle (1 = no correction)."""
    key = (getattr(machine, "tenant_code", None),
           getattr(machine, "site", "") or "",
           getattr(machine, "name", None))
    return _table().get(key, 1)


def collapse(total, good, rejected, divisor):
    """Divide a window's counts by ``divisor``, preserving good+rejected==total.

    ``good`` is DERIVED from the corrected total (total - rejected), not rounded
    on its own: three independent round() calls do not generally sum back to the
    total, and the ingest path drops any record whose parts disagree. ``rejected``
    is clamped into [0, total] for the same reason. At divisor 1 it is the
    identity, so a non-calibrated machine is byte-for-byte unchanged.
    """
    if divisor <= 1:
        return total, good, rejected
    new_total = round(total / divisor)
    new_rejected = min(round(rejected / divisor), new_total)
    if new_rejected < 0:
        new_rejected = 0
    new_good = new_total - new_rejected
    return new_total, new_good, new_rejected
