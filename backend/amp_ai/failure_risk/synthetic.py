"""The synthetic machine fleet the AMP-native failure-risk base model is trained on.

THIS DOCSTRING IS THE SPECIFICATION. Change a constant and you change the data
every shipped number was measured on; the build records this file's SHA-256 as
``generator_sha256`` and the evaluation ledger counts test-set runs against it.

WHAT IT IS FOR, AND WHAT IT IS NOT
----------------------------------
AMP never trains its base failure-risk model on customer data. It trains on
machines invented here. A model fitted to this fleet learns THE ASSUMPTIONS
WRITTEN BELOW, so its held-out accuracy is evidence that the learning pipeline
works on data shaped like this - not evidence of accuracy on a real plant. The
misspecification variants (bottom) exist to show how quickly that evidence
degrades when reality differs.

INDEPENDENCE
------------
Standard library only. NEVER predictive_engine, ai, models, database or
work_order_status: the labels here are breakdowns produced by a hazard model,
never by the rule-based scorer the model is compared with. No constant below was
chosen by looking at that scorer's thresholds; each is justified from
reliability engineering or shop-floor practice.

The only constants TUNED against generated data were ``eta`` and
``base_hazard`` (target: 4-8% of eligible machine-weeks contain a new
breakdown) and ``SHOCK_DRIVEN_RANDOM_HAZARD`` (to keep that variant's
prevalence comparable). The tuning looked at prevalence and at the breakdown
rate by hidden-wear quartile on 200 machines, never at a model or at the rule
scorer. First draft (eta 1.0): 1.6% prevalence and wear almost irrelevant,
because a maintained machine never got near eta. Grid tried (eta, base x):
(0.35, 1.0) 3.7%, (0.35, 1.5) 5.1%, (0.4, 1.5) 4.4%, (0.4, 2.0) 5.7%,
(0.5, 2.0) 4.6%, (0.5, 2.5) 5.8%. Chosen: eta 0.4 with base doubled, where the
highest wear quartile breaks down about 3.7x as often as the lowest.
shock_driven's random hazard: 0.004 -> 4.3%, 0.006 -> 5.6%, 0.008 -> 7.2%,
0.010 -> 8.4%; chosen 0.006 to match main's 5.7%.

TIME
----
Day ``d`` starts at ``EPOCH + d days`` (EPOCH is a Monday). Shifts are 8 hours
from 06:00; a 2-shift day runs 06:00-22:00, a 3-shift day to 06:00 next day.
Sundays are off; Saturdays run ``saturday_shifts``. ``as_of_for_day(d)`` is
10:00 on day d, inside the first shift, when a supervisor reads the dashboard:
it gives the rule scorer's utilisation input its most meaningful reading.

THE DAILY LOOP (per machine)
----------------------------
load      = clamp(base_load * machine_load_factor * plant_demand + 0.04 N, 0.2, 1.1)
            plant_demand is an AR(1) around 1.0 (phi 0.9, step sd 0.03, clamped
            to [0.75, 1.2]) shared by the ~40 machines of a plant: order books
            move plants, not single machines.
wear     += wear_rate * wear_mult * load * max(1 + 0.3 N, 0) * run_hours / 16
            Wear accrues with WORK done (run hours at load, excluding breakdown
            and PM time), not calendar time; the 0.3 N term is day-to-day
            variation in material and duty.
hazard    = base_hazard * hazard_mult * (1 + (wear / eta) ** k)
            * (1 + LOAD_STRESS_GAMMA * max(load - 0.85, 0)) * shock
            ``(wear/eta)**k`` is a Weibull wear-out term: k (shape) 2.5-3.5 is the
            range reported for mechanical fatigue and bearing wear; eta (0.4)
            is the wear at which the wear-out hazard equals the random-failure
            hazard. A machine maintained on schedule peaks near 0.3 before its
            PM, so wear-out stays below random failure - which is what a PM
            interval is for - while one or two skipped PMs push it past eta.
            Above ~85% of rated load thermal and mechanical stress rise quickly
            (bearing L10 life falls with the cube of load), hence the knee and
            gamma = 4 (a machine run at 100% carries 1.6x the hazard).
            ``shock`` is 3.0 on plant-wide disturbance days (power quality, heat),
            which start with probability 1/90 per day and last 1-3 days.
P(breakdown today) = 1 - exp(-hazard * run_hours / 16): at most one per day,
            at a uniformly random running minute.

REPAIRS AND MAINTENANCE
-----------------------
Corrective repair: downtime lognormal, median 150 min, sigma 0.7, clamped to
    [20 min, 72 h] (typical mean time to repair for discrete machinery is
    2-4 h with a long tail waiting for parts). Repair is IMPERFECT: wear is
    multiplied by U[0.3, 0.7] (a Kijima-type "better than old, not as good as
    new" repair). Logged as DowntimeLog reason "Breakdown" (80%) or a specific
    failure text, plus a MachineEvent Running->Breakdown and a reactive
    maintenance task completed that day.
Preventive maintenance (PM): a task is planned every ``pm_interval_days``
    +/- 3 days. 15% are skipped (the task stays open; production pressure is
    the usual reason). A performed PM starts at 06:00 on the first operating
    day the machine is up, takes U[90, 210] min (so it is finished before the
    10:00 as-of), multiplies wear by U[0.05, 0.3], and is logged as a
    Maintenance status spell and a "Preventive maintenance" downtime row. 10%
    of performed PMs are never closed in the system (the task stays open
    although the work was done) - the paperwork gap every CMMS has.

OBSERVABLE SYMPTOMS (what a model may learn from)
-------------------------------------------------
Logged stoppages   Poisson per 8 running hours with rate
                   stop_rate + STOP_RATE_PER_WEAR * sens * wear/eta (+1.5 for a
                   nuisance stopper). Durations lognormal, median 9 min. Worn
                   mechanisms jam and trip more; changeovers and shortages do
                   not care about wear. 15% of machines are nuisance stoppers
                   (feeder jams, sensor chatter) unrelated to wear.
Reject rate        reject_base * machine factor + REJECT_PER_WEAR2 * sens *
                   (wear/eta)**2 + drift, where drift is a slow random walk
                   (material lots, tooling) unrelated to wear. Quadratic because
                   loss of precision is negligible until clearances open up.
Performance        (perf_base + machine offset) * (1 - PERF_LOSS_PER_WEAR * sens
                   * wear/eta) * (1 + 0.02 N): worn machines are run slower.
Production records one per operating day, stamped at the end of the last shift.
                   20% of machines have 1-2 gaps of 14-35 days with no records
                   (manual entry lapsed); the machine keeps running.
Inspections        weekly (Wednesday 14:00) sample of 50 on machines that are
                   inspected (inspected_share per archetype); failures binomial
                   at 1.25x the reject rate (inspection is stricter than the line).
Status timeline    MachineEvent rows only when status CHANGES, as AMP's writers
                   do: Idle->Running at shift start (utilisation = load %),
                   Running->Idle at shift end (0), Breakdown and Maintenance
                   spells. A 3-shift machine followed by another operating day
                   runs through without an event.

``sens``, ``wear_mult``, ``hazard_mult``: per-machine lognormal factors
(sigma 0.30, 0.35, 0.40) - no two machines of a type age or fail alike.

ARCHETYPES
----------
Five archetypes train the model (table ``ARCHETYPES``); a sixth,
``HELD_OUT_ARCHETYPE`` (an old manual-era lathe: one shift, faster wear,
shallower wear-out shape k=2, long PM interval), is used only by the
``novel_archetype`` variant.

MISSPECIFICATION VARIANTS (reported next to the main result, never gated)
-------------------------------------------------------------------------
main             the model above
weak_symptoms    STOP_RATE_PER_WEAR, REJECT_PER_WEAR2, PERF_LOSS_PER_WEAR / 3:
                 wear is still there but shows far less in the records
shock_driven     hazard = 0.25 * (the wear-driven hazard) + a per-machine
                 constant random hazard: most failures are unrelated to wear
logging_gaps     each DowntimeLog row is independently dropped with p = 0.4;
                 the status timeline (and so the labels) is unchanged
novel_archetype  every machine is the held-out archetype

DETERMINISM
-----------
Every random draw comes from ``make_rng(seed, stream, machine)`` with separate
streams for traits, load, failures, stoppages, quality and maintenance, so a
machine is identical whether generated alone or in a fleet, and
``weak_symptoms`` / ``logging_gaps`` share ``main``'s failures exactly (common
random numbers). The hidden truth (wear trace, PM and repair records) is
returned separately and is read only by tests.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.rng import make_rng
from .history import BREAKDOWN, MachineHistory

__all__ = [
    "GENERATOR_ID", "VARIANTS", "EPOCH", "AS_OF_HOUR", "INITIAL_STATUS", "Archetype", "ARCHETYPES",
    "HELD_OUT_ARCHETYPE", "MachineTruth", "Fleet", "generate_fleet", "daily_hazard", "format_duration",
    "day_start", "as_of_for_day", "LOAD_STRESS_GAMMA",
]

GENERATOR_ID = "amp_ai.failure_risk.synthetic@v1"
VARIANTS = ("main", "weak_symptoms", "shock_driven", "logging_gaps", "novel_archetype")

EPOCH = datetime(2025, 1, 6)          # a Monday
SHIFT_START_HOUR = 6
SHIFT_MINUTES = 480
AS_OF_HOUR = 10
INITIAL_STATUS = "Idle"
RUNNING, IDLE, MAINTENANCE = "Running", "Idle", "Maintenance"
REFERENCE_RUN_HOURS = 16.0            # two shifts: the unit the daily rates are quoted in

MACHINES_PER_PLANT = 40
DEMAND_PHI, DEMAND_STEP_SD, DEMAND_MIN, DEMAND_MAX = 0.9, 0.03, 0.75, 1.2
SHOCK_START_P, SHOCK_MULTIPLIER = 1.0 / 90.0, 3.0

LOAD_STRESS_KNEE = 0.85
LOAD_STRESS_GAMMA = 4.0

REPAIR_MEDIAN_MIN, REPAIR_SIGMA, REPAIR_MIN, REPAIR_MAX = 150.0, 0.7, 20, 72 * 60
CORRECTIVE_RESET = (0.3, 0.7)
PM_SKIP_P, PM_UNRECORDED_P = 0.15, 0.10
PM_MINUTES = (90.0, 210.0)
PM_RESET = (0.05, 0.30)
PM_JITTER_DAYS = 3

STOP_RATE_PER_WEAR = 1.2              # extra logged stops per 8 running hours per unit wear/eta
NUISANCE_SHARE, NUISANCE_STOP_RATE = 0.15, 1.5
STOP_MEDIAN_MIN, STOP_SIGMA, STOP_MIN, STOP_MAX = 9.0, 0.6, 2, 90
REJECT_PER_WEAR2 = 0.04
PERF_LOSS_PER_WEAR = 0.08
DRIFT_PHI, DRIFT_STEP_SD, DRIFT_MAX = 0.95, 0.002, 0.03
RECORD_GAP_SHARE = 0.20
INSPECTION_WEEKDAY, INSPECTION_HOUR, INSPECTION_SAMPLE, INSPECTION_STRICTNESS = 2, 14, 50, 1.25

WEAK_SYMPTOM_DIVISOR = 3.0
SHOCK_DRIVEN_WEAR_SHARE = 0.25
SHOCK_DRIVEN_RANDOM_HAZARD = 0.006
LOGGING_GAP_DROP_P = 0.4

BREAKDOWN_REASON = "Breakdown"
SPECIFIC_FAILURE_REASONS = ("Spindle motor failure", "Hydraulic leak", "Bearing failure", "Electrical fault")
WEAR_STOP_REASONS = ("Jam", "Sensor fault", "Overheating", "Tool wear")
OTHER_STOP_REASONS = ("Changeover", "Material shortage", "Waiting for operator", "Cleaning")
PM_REASON = "Preventive maintenance"


@dataclass(frozen=True)
class Archetype:
    name: str
    prefix: str
    weekday_shifts: int
    saturday_shifts: int
    base_load: float
    wear_rate: float          # wear per 16 run hours at load 1.0
    shape_k: float
    eta: float
    base_hazard: float        # breakdowns per 16 run hours when wear is 0
    pm_interval_days: int
    ideal_cycle_s: int
    perf_base: float
    reject_base: float
    stop_rate: float          # logged stops per 8 running hours, wear-independent part
    inspected_share: float


# Wear rates are set so an un-maintained machine reaches eta in 2-4 months of
# typical duty; PM intervals follow common OEM guidance for each class
# (injection moulding 3 weeks, machining monthly, SMT 6 weeks).
ARCHETYPES = (
    Archetype("cnc_machining_centre", "CNC", 2, 1, 0.78, 1 / 70, 3.0, 0.4, 0.0040, 30, 45, 0.90, 0.015, 0.6, 0.9),
    Archetype("injection_moulding", "IMM", 3, 2, 0.85, 1 / 55, 3.0, 0.4, 0.0040, 21, 30, 0.92, 0.025, 0.5, 0.8),
    Archetype("smt_pick_and_place", "SMT", 2, 0, 0.70, 1 / 90, 2.5, 0.4, 0.0030, 45, 12, 0.93, 0.008, 1.0, 0.7),
    Archetype("hydraulic_press", "PRS", 2, 1, 0.80, 1 / 60, 3.5, 0.4, 0.0050, 28, 20, 0.88, 0.012, 0.5, 0.6),
    Archetype("packaging_line", "PKG", 3, 0, 0.75, 1 / 80, 3.0, 0.4, 0.0040, 35, 4, 0.85, 0.005, 1.2, 0.5),
)
HELD_OUT_ARCHETYPE = Archetype("legacy_lathe", "LTH", 1, 1, 0.65, 1 / 40, 2.0, 0.4, 0.0060, 60, 90, 0.80, 0.030, 0.7, 0.4)


@dataclass
class MachineTruth:
    """Hidden ground truth. Tests only: nothing a model or a report may read."""
    machine_id: int
    archetype: str
    plant: int
    nuisance: bool
    has_inspections: bool
    wear: list = field(default_factory=list)                 # end-of-day wear, one per day
    pm_days: list = field(default_factory=list)
    skipped_pm_days: list = field(default_factory=list)      # planned day of each skipped PM
    unrecorded_pm_days: list = field(default_factory=list)
    pending_pm_days: list = field(default_factory=list)      # planned, not yet done at the end
    pm_resets: list = field(default_factory=list)            # (day, wear_before, wear_after)
    breakdown_days: list = field(default_factory=list)
    corrective_resets: list = field(default_factory=list)


@dataclass
class Fleet:
    histories: list
    truth: dict
    n_machines: int
    days: int
    seed: int
    variant: str


def day_start(day):
    return EPOCH + timedelta(days=day)


def as_of_for_day(day):
    """The as-of instant used for day ``day``: 10:00, inside the first shift."""
    return EPOCH + timedelta(days=day, hours=AS_OF_HOUR)


def daily_hazard(wear, *, eta, k, base, load, gamma, shock):
    """Breakdowns per 16 running hours at this wear, load and plant condition."""
    return base * (1.0 + (wear / eta) ** k) * (1.0 + gamma * max(load - LOAD_STRESS_KNEE, 0.0)) * shock


def format_duration(minutes):
    """Free text as an operator types it; parses back exactly with duration.parse_duration_to_minutes."""
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    unit = "hr" if hours == 1 else "hrs"
    return f"{hours} {unit}" if rest == 0 else f"{hours} {unit} {rest} min"


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _poisson(rng, lam):
    if lam <= 0.0:
        return 0
    limit = math.exp(-lam)
    count, product = 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def _shifts(arch, day):
    weekday = day % 7
    if weekday < 5:
        return arch.weekday_shifts
    if weekday == 5:
        return arch.saturday_shifts
    return 0


def _plant_series(seed, plant, days):
    rng = make_rng(seed, "plant", str(plant))
    demand, shock = [], []
    x, shock_left = 1.0, 0
    for _ in range(days):
        x = _clamp(1.0 + DEMAND_PHI * (x - 1.0) + DEMAND_STEP_SD * rng.gauss(0.0, 1.0), DEMAND_MIN, DEMAND_MAX)
        start = rng.random()
        length = 1 + rng.randrange(3)
        if shock_left == 0 and start < SHOCK_START_P:
            shock_left = length
        demand.append(x)
        shock.append(SHOCK_MULTIPLIER if shock_left > 0 else 1.0)
        if shock_left > 0:
            shock_left -= 1
    return demand, shock


def _simulate(mid, days, seed, variant, plant_cache):
    traits = make_rng(seed, "traits", str(mid))
    archetype_index = traits.randrange(len(ARCHETYPES))
    arch = HELD_OUT_ARCHETYPE if variant == "novel_archetype" else ARCHETYPES[archetype_index]
    wear_mult = traits.lognormvariate(0.0, 0.35)
    hazard_mult = traits.lognormvariate(0.0, 0.40)
    sens = traits.lognormvariate(0.0, 0.30)
    load_factor = traits.lognormvariate(0.0, 0.08)
    nuisance = traits.random() < NUISANCE_SHARE
    has_inspections = traits.random() < arch.inspected_share
    wear = traits.uniform(0.0, 0.6) * arch.eta
    first_due = 1 + traits.randrange(arch.pm_interval_days)
    perf_offset = traits.uniform(-0.03, 0.03)
    reject_factor = traits.lognormvariate(0.0, 0.3)
    random_hazard = SHOCK_DRIVEN_RANDOM_HAZARD * traits.lognormvariate(0.0, 0.4)
    has_gaps = traits.random() < RECORD_GAP_SHARE
    n_gaps = 1 + traits.randrange(2)
    gaps = []
    for _ in range(2):
        start = traits.randrange(max(days - 35, 1))
        gaps.append((start, start + 14 + traits.randrange(22)))
    gaps = gaps[:n_gaps] if has_gaps else []

    stop_per_wear, reject_per_wear2, perf_per_wear = STOP_RATE_PER_WEAR, REJECT_PER_WEAR2, PERF_LOSS_PER_WEAR
    if variant == "weak_symptoms":
        stop_per_wear /= WEAK_SYMPTOM_DIVISOR
        reject_per_wear2 /= WEAK_SYMPTOM_DIVISOR
        perf_per_wear /= WEAK_SYMPTOM_DIVISOR

    plant = (mid - 1) // MACHINES_PER_PLANT
    if plant not in plant_cache:
        plant_cache[plant] = _plant_series(seed, plant, days)
    demand, shock = plant_cache[plant]

    load_rng = make_rng(seed, "load", str(mid))
    fail_rng = make_rng(seed, "failure", str(mid))
    stop_rng = make_rng(seed, "stops", str(mid))
    qual_rng = make_rng(seed, "quality", str(mid))
    maint_rng = make_rng(seed, "maintenance", str(mid))

    truth = MachineTruth(mid, arch.name, plant, nuisance, has_inspections)
    events, downtime, production, inspections, maintenance = [], [], [], [], []
    status = INITIAL_STATUS
    down_until = None
    drift = 0.0
    pending_pm = None
    next_due = first_due
    minute = timedelta(minutes=1)

    def emit(ts, new, util):
        nonlocal status
        events.append((ts, status, new, util))
        status = new

    for d in range(days):
        shifts = _shifts(arch, d)
        next_shifts = _shifts(arch, d + 1)
        period_start = EPOCH + timedelta(days=d, hours=SHIFT_START_HOUR)
        period_end = period_start + timedelta(days=1)
        window_end = period_start + SHIFT_MINUTES * shifts * minute

        load = _clamp(arch.base_load * load_factor * demand[d] + 0.04 * load_rng.gauss(0.0, 1.0), 0.2, 1.1)
        wear_noise = load_rng.gauss(0.0, 1.0)
        util = int(round(100.0 * min(load, 1.0)))
        drift = _clamp(DRIFT_PHI * drift + DRIFT_STEP_SD * qual_rng.gauss(0.0, 1.0), -DRIFT_MAX, DRIFT_MAX)
        perf_noise = qual_rng.gauss(0.0, 1.0)
        reject_noise = qual_rng.gauss(0.0, 1.0)

        # PM tasks are created on their due day whatever the machine is doing.
        if pending_pm is None and d >= next_due:
            planned_day = next_due
            skip = maint_rng.random() < PM_SKIP_P
            next_due = planned_day + arch.pm_interval_days + maint_rng.randrange(-PM_JITTER_DAYS, PM_JITTER_DAYS + 1)
            if skip:
                maintenance.append((day_start(planned_day).date(), None, False, True))
                truth.skipped_pm_days.append(planned_day)
            else:
                pending_pm = planned_day

        running_from = None
        repaired_today = False
        if shifts > 0 and status == BREAKDOWN and down_until < window_end:
            emit(max(down_until, period_start), RUNNING, util)
            running_from = max(down_until, period_start)
            repaired_today = True
        if shifts > 0 and status != BREAKDOWN and not repaired_today:
            if pending_pm is not None:
                pm_minutes = int(round(maint_rng.uniform(*PM_MINUTES)))
                reset = maint_rng.uniform(*PM_RESET)
                recorded = maint_rng.random() >= PM_UNRECORDED_P
                emit(period_start, MAINTENANCE, 0)
                running_from = period_start + pm_minutes * minute
                emit(running_from, RUNNING, util)
                downtime.append((period_start, PM_REASON, format_duration(pm_minutes)))
                truth.pm_resets.append((d, wear, wear * reset))
                wear *= reset
                truth.pm_days.append(d)
                planned_date = day_start(pending_pm).date()
                if recorded:
                    maintenance.append((planned_date, day_start(d).date(), False, False))
                else:
                    maintenance.append((planned_date, None, False, True))
                    truth.unrecorded_pm_days.append(d)
                pending_pm = None
            else:
                if status != RUNNING:
                    emit(period_start, RUNNING, util)
                running_from = period_start

        breakdown_minutes = 0
        stop_minutes = 0
        run_span = 0
        if running_from is not None and running_from < window_end:
            available = int((window_end - running_from) / minute)
            u_fail, u_time, z_repair, u_reset, u_reason = (fail_rng.random(), fail_rng.random(),
                                                           fail_rng.gauss(0.0, 1.0), fail_rng.random(),
                                                           fail_rng.random())
            hazard = daily_hazard(wear, eta=arch.eta, k=arch.shape_k, base=arch.base_hazard * hazard_mult,
                                  load=load, gamma=LOAD_STRESS_GAMMA, shock=shock[d])
            if variant == "shock_driven":
                hazard = SHOCK_DRIVEN_WEAR_SHARE * hazard + random_hazard
            if u_fail < 1.0 - math.exp(-hazard * available / 60.0 / REFERENCE_RUN_HOURS):
                t_break = running_from + int(u_time * available) * minute
                repair = int(_clamp(round(math.exp(math.log(REPAIR_MEDIAN_MIN) + REPAIR_SIGMA * z_repair)),
                                    REPAIR_MIN, REPAIR_MAX))
                emit(t_break, BREAKDOWN, 0)
                down_until = t_break + repair * minute
                reason = (BREAKDOWN_REASON if u_reason < 0.8 else
                          SPECIFIC_FAILURE_REASONS[int((u_reason - 0.8) / 0.2 * len(SPECIFIC_FAILURE_REASONS))
                                                   % len(SPECIFIC_FAILURE_REASONS)])
                downtime.append((t_break, reason, format_duration(repair)))
                maintenance.append((t_break.date(), t_break.date(), True, False))
                share = CORRECTIVE_RESET[0] + (CORRECTIVE_RESET[1] - CORRECTIVE_RESET[0]) * u_reset
                truth.corrective_resets.append((d, wear, wear * share))
                wear *= share
                truth.breakdown_days.append(d)
                breakdown_minutes = int((min(down_until, window_end) - t_break) / minute)
                if down_until < window_end:
                    emit(down_until, RUNNING, util)

            run_span = available - breakdown_minutes
            wear_share = stop_per_wear * sens * wear / arch.eta
            rate = arch.stop_rate + wear_share + (NUISANCE_STOP_RATE if nuisance else 0.0)
            for _ in range(_poisson(stop_rng, rate * run_span / SHIFT_MINUTES)):
                ts = running_from + int(stop_rng.random() * available) * minute
                length = int(_clamp(round(math.exp(math.log(STOP_MEDIAN_MIN) + STOP_SIGMA * stop_rng.gauss(0.0, 1.0))),
                                    STOP_MIN, STOP_MAX))
                vocabulary = WEAR_STOP_REASONS if stop_rng.random() * rate < wear_share else OTHER_STOP_REASONS
                downtime.append((ts, vocabulary[stop_rng.randrange(len(vocabulary))], format_duration(length)))
                stop_minutes += length

        if shifts > 0:
            planned = SHIFT_MINUTES * shifts
            lost = (planned if running_from is None else int((running_from - period_start) / minute)
                    + breakdown_minutes + stop_minutes)
            runtime = int(_clamp(planned - lost, 0, planned))
            perf = _clamp((arch.perf_base + perf_offset) * (1.0 - perf_per_wear * sens * wear / arch.eta)
                          * (1.0 + 0.02 * perf_noise), 0.3, 1.0)
            total = int(runtime * 60 / arch.ideal_cycle_s * perf)
            reject_rate = _clamp(arch.reject_base * reject_factor + reject_per_wear2 * sens * (wear / arch.eta) ** 2
                                 + drift, 0.0, 0.5)
            spread = math.sqrt(total * reject_rate * (1.0 - reject_rate))
            rejected = int(_clamp(round(total * reject_rate + spread * reject_noise), 0, total))
            if not any(a <= d < b for a, b in gaps):
                production.append((window_end, planned, runtime, arch.ideal_cycle_s, total, total - rejected, rejected))
            if has_inspections and d % 7 == INSPECTION_WEEKDAY:
                p_fail = min(0.95, reject_rate * INSPECTION_STRICTNESS)
                failed = sum(1 for _ in range(INSPECTION_SAMPLE) if qual_rng.random() < p_fail)
                inspections.append((day_start(d) + timedelta(hours=INSPECTION_HOUR), INSPECTION_SAMPLE, failed))
            # Accrues over the running span BEFORE logged stops: stops are a few
            # percent of run time, and keeping wear independent of the stoppage
            # stream is what lets weak_symptoms share main's failures exactly.
            wear += (arch.wear_rate * wear_mult * load * max(1.0 + 0.3 * wear_noise, 0.0)
                     * run_span / 60.0 / REFERENCE_RUN_HOURS)

        if shifts > 0 and status == RUNNING and not (shifts == 3 and next_shifts > 0):
            emit(window_end, IDLE, 0)
        if status == BREAKDOWN and down_until < period_end:
            emit(down_until, IDLE, 0)
        truth.wear.append(wear)

    if pending_pm is not None:
        maintenance.append((day_start(pending_pm).date(), None, False, True))
        truth.pending_pm_days.append(pending_pm)

    if variant == "logging_gaps":
        drop = make_rng(seed, "logging_gaps", str(mid))
        downtime.sort(key=lambda r: r[0])
        downtime = [row for row in downtime if drop.random() >= LOGGING_GAP_DROP_P]
    downtime.sort(key=lambda r: r[0])
    inspections.sort(key=lambda r: r[0])
    maintenance.sort(key=lambda r: r[0])

    history = MachineHistory(mid, f"{arch.prefix}-{mid:03d}", events=events, downtime=downtime,
                             production=production, inspections=inspections, maintenance=maintenance)
    return history, truth


def generate_fleet(n_machines, days, seed, variant="main", *, machine_ids=None) -> Fleet:
    """``n_machines`` synthetic machines (ids 1..n) over ``days`` days. See the module docstring."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an int")
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")
    if isinstance(n_machines, bool) or not isinstance(n_machines, int) or n_machines < 1:
        raise ValueError("n_machines must be a positive int")
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive int")
    ids = list(range(1, n_machines + 1)) if machine_ids is None else sorted(set(machine_ids))
    if any(isinstance(i, bool) or not isinstance(i, int) or not 1 <= i <= n_machines for i in ids):
        raise ValueError(f"machine_ids must lie in 1..{n_machines}")
    plant_cache = {}
    histories, truth = [], {}
    for mid in ids:
        history, machine_truth = _simulate(mid, days, seed, variant, plant_cache)
        histories.append(history)
        truth[mid] = machine_truth
    return Fleet(histories, truth, n_machines, days, seed, variant)
