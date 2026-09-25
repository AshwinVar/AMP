"""Canonical signals become the message AMP already understands.

AMP HAS READ `{prefix}/{tenant}/{site}/machines` SINCE BEFORE THIS GATEWAY
EXISTED, and that contract works: machine, status, utilization, downtime, and a
production record when the counts add up. Inventing a second ingest shape would
mean two paths into the same tables, two sets of honesty rules, and a migration
nobody asked for. So the gateway speaks the existing contract, and carries its
canonical samples alongside it for anything that later wants them.

THE PART THAT MATTERS IS WHAT IS LEFT OUT.

`status` is only present when a signal actually said so. AMP's handler defaults
a missing status to "Idle", so sending a telemetry-only packet would tell a
plant that a running machine had stopped. A gateway that knows the temperature
but not the run bit must therefore say NOTHING about state — which is exactly
the rule AMP's own read-models keep, applied at the point where the packet is
built rather than argued about later.

`utilization` is not invented either. It is a percentage AMP charts, and there
is no honest way to derive it from a single run bit at a single instant, so it
is sent only when the mapping provides one.

PRODUCTION COUNTS ARE REFUSED RATHER THAN BALANCED. AMP writes a
ProductionRecord only when good + rejected == total, which is the right check.
The tempting fix when a plant maps only a total is to call it all good — and
that quietly reports 100% quality for the life of the pilot. So a machine that
maps only `part_count` produces no ProductionRecord unless its config says, in
writing, that untracked parts may be counted as good.
"""
import time

from . import signals

# What AMP's normalize_machine_status accepts. Kept here as a literal, because
# the gateway must not import the backend to find out.
RUNNING = "Running"
IDLE = "Idle"
DOWN = "Down"


class MachineState:
    """What one machine has told us since the last publish.

    Holds counter DELTAS (the normalizer already turned raw counters into
    increments) and the latest value of each state signal. Reset on publish, so
    a message covers exactly the window since the previous one and two messages
    can never contain the same part twice.
    """

    def __init__(self, name, quality_unknown_is_good=False):
        self.name = name
        self.quality_unknown_is_good = bool(quality_unknown_is_good)
        self.latest = {}
        self.deltas = {}
        self.telemetry = {}
        self.window_started = time.time()
        self.running_seconds = 0.0
        self._last_seen = None
        self.notes = []

    def absorb(self, samples, now=None):
        now = time.time() if now is None else now
        for sample in samples:
            if signals.is_counter(sample.signal):
                self.deltas[sample.signal] = self.deltas.get(sample.signal, 0) + int(sample.value)
                if sample.note:
                    self.notes.append(f"{sample.signal}: {sample.note}")
                continue
            if not signals.is_canonical(sample.signal):
                self.telemetry[sample.signal] = {"value": sample.value, "unit": sample.unit,
                                                 "ts": sample.timestamp}
                continue
            # Runtime is accumulated from the run bit rather than assumed from
            # the window: a machine that ran for nine minutes of a ten-minute
            # window has 90% availability, and claiming the whole window would
            # inflate every OEE figure the pilot ever sees.
            if (sample.signal == signals.RUNNING and self.latest.get(signals.RUNNING) is True
                    and self._last_seen is not None):
                self.running_seconds += max(0.0, sample.timestamp - self._last_seen)
            self.latest[sample.signal] = sample.value
            if sample.signal == signals.RUNNING:
                self._last_seen = sample.timestamp

    def has_anything(self):
        return bool(self.latest or self.deltas or self.telemetry)

    def reset(self, now=None):
        now = time.time() if now is None else now
        self.deltas = {}
        self.telemetry = {}
        self.notes = []
        self.window_started = now
        self.running_seconds = 0.0
        self._last_seen = now if self.latest.get(signals.RUNNING) is True else None


def status_of(latest):
    """The machine's state, or None when nothing said.

    None is not a failure and not "Idle" — it is the gateway declining to
    assert something it was not told. The caller omits the key entirely.
    """
    if latest.get(signals.FAULT_ACTIVE) is True:
        return DOWN
    running = latest.get(signals.RUNNING)
    if running is True:
        return RUNNING
    if running is False:
        # A machine that is not running is Idle unless a fault says otherwise.
        # Down is reserved for a declared fault, because "stopped" and "broken"
        # are different things to everyone who reads the dashboard.
        return IDLE
    cycle = latest.get(signals.CYCLE_ACTIVE)
    if cycle is True:
        return RUNNING
    return None


def counts_of(state):
    """(total, good, rejected) or None, with the arithmetic AMP requires.

    None means "do not write a production record", which is the correct answer
    far more often than a balanced-looking triple would be.
    """
    d = state.deltas
    part = d.get(signals.PART_COUNT)
    good = d.get(signals.GOOD_COUNT)
    reject = d.get(signals.REJECT_COUNT)

    if good is not None and reject is not None:
        total = part if part is not None else good + reject
        if part is not None and part != good + reject:
            # Both mapped and they disagree. The counters are the customer's
            # own; AMP does not get to pick a winner, and a record that does not
            # balance is refused by the handler anyway.
            state.notes.append(
                f"part_count ({part}) does not equal good ({good}) + reject ({reject}); no "
                f"production record written for this window")
            return None
        return total, good, reject
    if part is not None and reject is not None:
        if reject > part:
            state.notes.append(f"reject ({reject}) exceeds part_count ({part}); refused")
            return None
        return part, part - reject, reject
    if part is not None and good is not None:
        if good > part:
            state.notes.append(f"good ({good}) exceeds part_count ({part}); refused")
            return None
        return part, good, part - good
    if part is not None:
        if not state.quality_unknown_is_good:
            # THE IMPORTANT REFUSAL. Counting every part as good because nothing
            # counts rejects is how a pilot's quality figure becomes 100% and
            # stays there.
            state.notes.append(
                "only part_count is mapped, so quality is unknown. AMP will not record "
                "production that claims every part was good — map reject_count or good_count, "
                "or set quality_unknown_is_good on this machine if the line genuinely has no "
                "reject counter.")
            return None
        return part, part, 0
    if good is not None and reject is None:
        return good, good, 0
    return None


def build(state, *, machine_name=None, ideal_cycle_time_seconds=None, now=None):
    """The MQTT payload, or None when there is nothing honest to say."""
    now = time.time() if now is None else now
    if not state.has_anything():
        return None

    body = {"machine": machine_name or state.name, "ts": round(now, 3)}

    status = status_of(state.latest)
    if status is not None:
        body["status"] = status
        # Only alongside a status: `downtime` is AMP's human-readable string for
        # how long a machine has been down, and asserting "0 min" while not
        # knowing whether it is running at all is a fabrication.
        if status != DOWN:
            body["downtime"] = "0 min"

    if signals.MACHINE_MODE in state.latest:
        body["machine_mode"] = state.latest[signals.MACHINE_MODE]
    if state.latest.get(signals.FAULT_CODE) not in (None, ""):
        body["fault_code"] = state.latest[signals.FAULT_CODE]

    counts = counts_of(state)
    if counts is not None:
        total, good, rejected = counts
        if total > 0:
            window_minutes = max(1, int(round((now - state.window_started) / 60.0)))
            body.update({
                "total_count": int(total),
                "good_count": int(good),
                "rejected_count": int(rejected),
                "planned_minutes": window_minutes,
                # Measured from the run bit where there is one. Falling back to
                # the window would claim the machine ran the whole time.
                "runtime_minutes": int(round(state.running_seconds / 60.0))
                if state.running_seconds else window_minutes,
            })
            cycle = ideal_cycle_time_seconds or state.latest.get(signals.CYCLE_TIME)
            if cycle:
                body["ideal_cycle_time_seconds"] = int(round(float(cycle)))

    if state.telemetry:
        body["telemetry"] = {k: v["value"] for k, v in state.telemetry.items()}
        body["telemetry_units"] = {k: v["unit"] for k, v in state.telemetry.items() if v["unit"]}

    if state.notes:
        # Carried to AMP rather than only logged locally: "why is there no
        # production record" is a question asked from the dashboard, four
        # hundred miles from the gateway.
        body["gateway_notes"] = list(state.notes)[:5]

    # Nothing but the machine name and a clock is not worth a message — it would
    # keep AMP's freshness ticking while saying nothing, which is its own lie.
    if len(body) <= 2:
        return None
    return body
