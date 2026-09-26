"""The arithmetic that can invent production, and the queue that can lose it.

Everything here is on the sprint's mandatory-deep-test list: production-counter
correctness, data-corruption risk, and the honesty rules that separate "the
machine is stopped" from "AMP does not know". None of it needs a PLC — the whole
point of the canonical layer is that it can be driven with plain values.

Run: python edge/test_edge_pipeline.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ampedge import buffer as buffer_mod          # noqa: E402
from ampedge import mapping as mapping_mod        # noqa: E402
from ampedge import normalizer as normalizer_mod  # noqa: E402
from ampedge import payload as payload_mod        # noqa: E402
from ampedge.adapters import base                 # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def counter(mode, **extra):
    spec = {"tag": "c", "address": "c", "signal": "part_count", "datatype": "int",
            "counter_mode": mode}
    spec.update(extra)
    return normalizer_mod.Normalizer(mapping_mod.validate([spec]))


def feed(norm, values, start=None, step=1.0):
    """Readings at increasing times, returning the deltas produced."""
    start = start or time.time() - 100
    out = []
    for i, value in enumerate(values):
        samples = norm.absorb([base.Reading(tag="c", value=value, timestamp=start + i * step)],
                              now=start + i * step + 0.1)
        out.append(samples[0].value if samples else None)
    return out


# ── 1. counters ─────────────────────────────────────────────────────
section("1. A COUNTER BECOMES PRODUCTION, OR IT BECOMES NOTHING")
deltas = feed(counter("cumulative"), [1000, 1007, 1009])
check("the first reading is a baseline, not 1000 parts", deltas[0] is None, str(deltas[0]))
check("then the increments are the production", deltas[1:] == [7, 2], str(deltas))

# A 16-bit counter wrapping. Only explicable because counter_max was DECLARED.
deltas = feed(counter("cumulative", counter_max=65535), [65530, 3])
check("a declared rollover counts the 9 parts across the wrap", deltas[1] == 9, str(deltas))

# The same numbers with NO declared maximum. Guessing rollover here is how a
# reset becomes 65,000 phantom parts.
norm = counter("cumulative")
deltas = feed(norm, [65530, 3])
check("an UNdeclared backwards step counts ZERO rather than guessing", deltas[1] == 0,
      str(deltas))
check("...and says so, with the fix", any("counter_mode" in n for n in norm.counter_notes),
      str(norm.counter_notes))
check("...and is recorded as a rejection an engineer will see",
      any("backwards" in r.reason for r in norm.rejections), str(norm.rejections))

# A counter far from its ceiling dropping to near zero is a reset, not a wrap —
# even with counter_max declared.
deltas = feed(counter("cumulative", counter_max=65535), [412, 0])
check("a drop nowhere near the ceiling is NOT treated as a rollover", deltas[1] == 0,
      f"{deltas[1]} parts invented")

# A counter DECLARED to reset: the new value is genuine production.
deltas = feed(counter("resets"), [412, 5])
check("a declared reset counts the 5 parts made since it", deltas[1] == 5, str(deltas))

# per_cycle: the same number sitting there is one cycle, not six.
deltas = feed(counter("per_cycle"), [4, 4, 4, 6])
check("a repeated per-cycle value is counted once", deltas == [4, None, None, 6], str(deltas))

# A counter with no mode at all must never reach the normalizer.
refused = None
try:
    mapping_mod.validate([{"tag": "c", "address": "c", "signal": "part_count", "datatype": "int"}])
except mapping_mod.MappingError as e:
    refused = str(e)
check("a counter with no counter_mode is refused at load", refused is not None, "it was accepted")
check("...naming the consequence rather than just the rule",
      refused and "multiplies a shift" in refused, str(refused))

# ── 2. absence is not zero ──────────────────────────────────────────
section("2. NO DATA IS NOT ZERO, AND UNKNOWN IS NOT FALSE")
run_map = mapping_mod.validate([{"tag": "r", "address": "r", "signal": "running",
                                 "datatype": "bool"}])
norm = normalizer_mod.Normalizer(run_map)
samples = norm.absorb([base.no_data("r", "the PLC stopped answering")])
check("an unreachable tag produces NO sample at all", samples == [], str(samples))
check("...and is recorded as a rejection, not silently dropped",
      len(norm.rejections) == 1, str(norm.rejections))

state = payload_mod.MachineState("M1")
state.absorb(norm.absorb([base.no_data("r", "gone")]))
body = payload_mod.build(state, machine_name="M1")
check("a machine AMP cannot read produces no message rather than an Idle one",
      body is None, str(body))

# The dangerous version: SOME data, but not the run bit. AMP's handler defaults
# a missing status to Idle, so the gateway must not send one it does not know.
temp_map = mapping_mod.validate([{"tag": "t", "address": "t", "signal": "temperature",
                                  "datatype": "float", "unit": "degC"}])
state = payload_mod.MachineState("M1")
state.absorb(normalizer_mod.Normalizer(temp_map).absorb(
    [base.Reading(tag="t", value=61.2)]))
body = payload_mod.build(state, machine_name="M1")
check("telemetry alone is published under `readings`, which is the key AMP reads",
      body is not None and "readings" in body, str(body))
check("...WITHOUT a status, because nothing said whether it was running",
      "status" not in body, str(body))
check("...and without a downtime figure either", "downtime" not in body, str(body))

# A genuine False is not absence and must survive.
state = payload_mod.MachineState("M1")
state.absorb(normalizer_mod.Normalizer(run_map).absorb([base.Reading(tag="r", value=False)]))
body = payload_mod.build(state, machine_name="M1")
check("a PLC that says STOPPED is reported as Idle, which is different from silence",
      body and body.get("status") == "Idle", str(body))

# ── 3. clocks ───────────────────────────────────────────────────────
section("3. A CLOCK THAT IS WRONG IS NOT A VALUE")
norm = normalizer_mod.Normalizer(run_map)
now = time.time()
samples = norm.absorb([base.Reading(tag="r", value=True, timestamp=now + 3600)], now=now)
check("a reading stamped an hour in the future is refused", samples == [], str(samples))
check("...naming the PLC clock as the thing to check",
      any("clock" in r.reason for r in norm.rejections), str(norm.rejections))

norm = normalizer_mod.Normalizer(run_map)
norm.absorb([base.Reading(tag="r", value=True, timestamp=now)], now=now)
samples = norm.absorb([base.Reading(tag="r", value=False, timestamp=now - 30)], now=now)
check("an out-of-order reading does not overwrite a newer one", samples == [], str(samples))

# ── 4. quality ──────────────────────────────────────────────────────
section("4. A VALUE THE SERVER DOUBTS IS NOT A VALUE")
norm = normalizer_mod.Normalizer(run_map)
samples = norm.absorb([base.Reading(tag="r", value=True, quality=base.UNCERTAIN,
                                    detail="Uncertain_LastUsableValue")])
check("an UNCERTAIN reading is not used, even though it carries a value",
      samples == [], str(samples))
check("...and the reason reaches the engineer",
      any("uncertain" in r.reason for r in norm.rejections), str(norm.rejections))

# ── 5. quality figures are refused, not invented ────────────────────
section("5. AMP WILL NOT REPORT 100% QUALITY BECAUSE NOBODY COUNTS SCRAP")
state = payload_mod.MachineState("M1")
state.deltas = {"part_count": 40}
counts = payload_mod.counts_of(state)
check("part_count alone produces NO production record", counts is None, str(counts))
check("...and explains what to map instead",
      any("reject_count" in n for n in state.notes), str(state.notes))

state = payload_mod.MachineState("M1", quality_unknown_is_good=True)
state.deltas = {"part_count": 40}
check("...unless the config says in writing that there is no reject counter",
      payload_mod.counts_of(state) == (40, 40, 0), str(payload_mod.counts_of(state)))

state = payload_mod.MachineState("M1")
state.deltas = {"part_count": 40, "good_count": 30, "reject_count": 5}
check("counters that do not balance are refused rather than reconciled",
      payload_mod.counts_of(state) is None, str(payload_mod.counts_of(state)))
state = payload_mod.MachineState("M1")
state.deltas = {"part_count": 40, "reject_count": 5}
check("part + reject gives good by subtraction", payload_mod.counts_of(state) == (40, 35, 5),
      str(payload_mod.counts_of(state)))

# ── 6. the buffer ───────────────────────────────────────────────────
section("6. THE QUEUE SURVIVES A RESTART, AND NEVER LOSES QUIETLY")
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "queue.db")
buf = buffer_mod.Buffer(path, max_records=5)
for i in range(3):
    buf.put({"machine": "M1", "total_count": i})
check("three records queued", buf.depth() == 3, str(buf.depth()))
buf.close()

# A power cut. The gateway comes back and the shift's production is still there.
buf = buffer_mod.Buffer(path, max_records=5)
check("they are still there after a restart", buf.depth() == 3, str(buf.depth()))
batch = buf.peek(10)
check("...oldest first", [b[3]["total_count"] for b in batch] == [0, 1, 2],
      str([b[3]["total_count"] for b in batch]))

# A failed publish must NOT remove anything.
check("peek does not remove", buf.depth() == 3, str(buf.depth()))
buf.ack([batch[0][0]])
check("ack removes exactly what was acknowledged", buf.depth() == 2, str(buf.depth()))

# Overflow. Something must go, and it must be visible that it did.
for i in range(10):
    buf.put({"machine": "M1", "total_count": 100 + i})
stats = buf.stats()
check("the queue stays bounded", stats["queued"] == 5, str(stats["queued"]))
check("...and counts what it dropped", stats["dropped"] == 7, str(stats["dropped"]))
check("the next record carries a gap marker so AMP knows the stream has a hole",
      any(b[3].get("gap_before") for b in buf.peek(10)),
      str([b[3].get("gap_before") for b in buf.peek(10)]))

# A buffered record keeps its own time and says it waited.
queued_at = time.time() - 7200
stamped = buffer_mod.stamp_for_publish({"machine": "M1", "ts": queued_at}, queued_at)
check("a record uploaded two hours late is marked buffered", stamped.get("buffered") is True,
      str(stamped))
check("...with how long it waited", stamped["buffered_for"] > 7000, str(stamped["buffered_for"]))
check("...and its ORIGINAL timestamp untouched", stamped["ts"] == queued_at, str(stamped["ts"]))
fresh = buffer_mod.stamp_for_publish({"machine": "M1"}, time.time())
check("a record published immediately is NOT marked buffered", "buffered" not in fresh,
      str(fresh))

check("every record carries an id AMP can deduplicate on",
      all(b[3].get("record_id") for b in buf.peek(10)), "a record had no id")
ids = [b[3]["record_id"] for b in buf.peek(10)]
check("...and they are distinct", len(set(ids)) == len(ids), str(ids))
buf.close()

# ── 7. a refusal is COUNTED, not just listed ────────────────────────
section("7. WHAT IS REFUSED STAYS VISIBLE AFTER THE POLL THAT REFUSED IT")
# `rejections` is cleared on every absorb, so a health report built from it
# would go quiet the moment one poll happened to be clean -- and a tag refused
# nine polls in ten is exactly as broken as one refused every poll.
norm = normalizer_mod.Normalizer(run_map)
for _ in range(3):
    norm.absorb([base.Reading(tag="r", value="SPINNING")])
check("this poll's list holds one", len(norm.rejections) == 1, str(len(norm.rejections)))
check("...while the running tally holds all three",
      norm.refusals.get("running", (0, ""))[0] == 3, str(norm.refusals))
check("...with the reason kept", "true_values" in norm.refusals["running"][1],
      norm.refusals["running"][1])

check("a signal that has never produced a reading is reported as not arriving",
      [sig for sig, _, _ in norm.not_arriving()] == ["running"], str(norm.not_arriving()))
check("...with the refusal as the reason, not a generic 'nothing yet'",
      "true_values" in norm.not_arriving()[0][1], str(norm.not_arriving()))

# The distinction the whole fix rests on: a reading that ARRIVED and produced no
# sample is not a failure. A counter's first reading is exactly that.
counter_map = mapping_mod.validate([{"tag": "c", "address": "c", "signal": "part_count",
                                     "datatype": "int", "counter_mode": "cumulative"}])
baseline = normalizer_mod.Normalizer(counter_map)
samples = baseline.absorb([base.Reading(tag="c", value=1000)])
check("a counter baseline emits no sample", samples == [], str(samples))
check("...and is NOT reported as not arriving — the reading did arrive",
      baseline.not_arriving() == [], str(baseline.not_arriving()))

# A signal that arrived once and then stopped is not arriving either.
stale = normalizer_mod.Normalizer(run_map)
old_now = time.time() - 10_000
stale.absorb([base.Reading(tag="r", value=True, timestamp=old_now)], now=old_now)
check("a signal that stopped hours ago is reported as not arriving",
      [sig for sig, _, _ in stale.not_arriving()] == ["running"], str(stale.not_arriving()))

# ── 8. the clock is measured, including on the reading it refuses ───
section("8. THE PLC'S CLOCK IS MEASURED FROM THE READING IT SPOILS")
norm = normalizer_mod.Normalizer(run_map)
now = time.time()
norm.absorb([base.Reading(tag="r", value=True, timestamp=now + 4000, source_time=True)],
            now=now)
check("the reading was refused", norm.not_arriving() != [], str(norm.not_arriving()))
# Measured BEFORE the refusal: the reading that proves the clock is wrong is the
# one thrown away, so measuring only what survives measures nothing on the
# machine that needs it most.
check("...and the skew was still measured from it", norm.clock_skew is not None,
      str(norm.clock_skew))
check("...as roughly the real offset", 3900 < norm.clock_skew < 4100, str(norm.clock_skew))

behind = normalizer_mod.Normalizer(run_map)
behind.absorb([base.Reading(tag="r", value=True, timestamp=now - 600, source_time=True)],
              now=now)
check("a PLC running BEHIND is measured as negative", behind.clock_skew < -500,
      str(behind.clock_skew))
# THIS FOUND A REAL DEFECT. Staleness was judged on the PLC's own timestamp, so
# a controller whose clock runs ten minutes behind -- delivering perfectly, just
# mislabelled -- had every reading counted as stale and the machine reported as
# not arriving. Arrival and source time are different questions and are now
# tracked separately.
check("...and a lagging clock does NOT make a delivering machine look dead",
      behind.not_arriving() == [], str(behind.not_arriving()))
check("...because freshness answers 'is data flowing', by OUR clock",
      behind.freshness(now)["running"] < 1, str(behind.freshness(now)))
check("...while the out-of-order guard still uses the PLC's own clock",
      behind.absorb([base.Reading(tag="r", value=False, timestamp=now - 900,
                                  source_time=True)], now=now) == [],
      "an older source timestamp was accepted as current")

# Modbus has no clock to report, so there is nothing to measure and nothing to
# claim: None, never 0, which would read as "perfectly synchronised".
no_clock = normalizer_mod.Normalizer(run_map)
no_clock.absorb([base.Reading(tag="r", value=True)])
check("a protocol with no clock reports None, not a confident zero",
      no_clock.clock_skew is None, str(no_clock.clock_skew))

print()
print("=" * 74)
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
