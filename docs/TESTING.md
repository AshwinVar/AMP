# AMP — Testing and coverage

The backend has more than 160 test files and over a thousand test functions. Until
now none of it was measured: there was no way to answer "which branches has nobody
ever exercised?", so the honest answer to "is this module tested?" was always
"there is a file with its name on it".

This document covers the three ways to run the backend suite, what the coverage
report means, and what number we hold the line at.

---

## The one rule

**The suites are standalone scripts and must stay that way.** Every
`backend/test_*.py` defines module-level `test_*` functions and ends with:

```python
if __name__ == "__main__":
    test_one()
    test_two()
    print("SOMETHING OK: ...")
```

CI runs each file with `python test_X.py` and treats exit 0 as a pass. That is
the contract. pytest was added **on top** of it, not in place of it: no existing
test file was modified, none of them import pytest, and none of them will break
if pytest is uninstalled. `conftest.py`, `pytest.ini` and `.coveragerc` are never
read by `python test_X.py` — verified on 2026-08-04 by looping the whole set after
they landed (170 suites run, none failed). If you add a suite, follow the same
shape; a new file that only works under pytest breaks CI.

This document covers the unit suites and their coverage. Browser end-to-end tests,
where they exist, are a separate concern and are not measured here.

---

## Running the suite

### 1. Per file — what CI actually does

```bash
cd backend
DATABASE_URL="sqlite:///./ci.db" python test_tenancy.py
```

One process per suite, no plugins, no configuration. This is the mode to use when
a suite fails in CI and you want to reproduce it exactly, and the mode to use when
you suspect the failure is caused by another suite (see *Cross-suite leakage*
below) — a fresh interpreter per file rules that out by construction.

The whole set, the way the CI job loops over it:

```bash
cd backend
for f in test_*.py; do python "$f" >/dev/null 2>&1 || echo "FAIL $f"; done
```

`DATABASE_URL` must be set. `backend/database.py` reads `os.environ["DATABASE_URL"]`
with no fallback on purpose — a missing database URL has to fail loudly rather
than quietly connect to something local. Locally, `backend/.env` supplies it;
in CI the workflow sets `sqlite:///./ci.db`. The suites build their own in-memory
engines, so this value only satisfies import-time wiring.

### 2. Under pytest — one process, filtering, coverage

```bash
cd backend
pip install pytest
pytest                      # everything
pytest test_tenancy.py      # one suite
pytest -k "tenant"          # every test whose name mentions tenants, across all files
pytest -x --tb=short        # stop at the first failure
```

`backend/conftest.py` supplies the two shims this needs (a default `DATABASE_URL`
and a tenant-context reset between tests); `backend/pytest.ini` holds the
collection rules. Both are documented inline — read them before changing them.

What you get over mode 1: `-k` filtering across the whole suite, a single 90-second
run instead of 169 interpreter starts, and coverage.

What you lose: process isolation. See *Cross-suite leakage*.

### 3. With coverage

```bash
cd backend
pip install pytest pytest-cov
pytest --cov --cov-report=term-missing:skip-covered
pytest --cov --cov-report=html && start htmlcov/index.html   # line-by-line browser view
```

Neither `pytest` nor `pytest-cov` is in `requirements.txt`, and that is deliberate:
they are measurement tools, not things the application needs to run. For the same
reason `--cov` is **not** in `pytest.ini`'s `addopts` — if it were, a bare `pytest`
on a machine without the plugin would die with "unrecognized arguments".

Coverage settings (branch mode, exclusions) live in `backend/.coveragerc`, which
both `pytest --cov` and a plain `coverage run` pick up automatically.

If you want the number for the code paths CI actually executes — one process per
suite, exactly as in mode 1 — measure it that way instead:

```bash
cd backend
export DATABASE_URL="sqlite:///./ci.db"
for f in test_*.py; do coverage run -p "$f" >/dev/null 2>&1; done
coverage combine && coverage report
```

`-p` gives each run its own data file; `combine` unions them. Slower (each suite
pays interpreter startup plus coverage tracing), but it is the ground truth for
"what does the CI job cover".

---

## Reading the report

```
Name                  Stmts   Miss Branch BrPart  Cover   Missing
------------------------------------------------------------------
core_routes.py          149     48     38      6  66.8%   40-44, 52, 109->118, 218-241
```

| Column | Meaning |
|---|---|
| `Stmts` | executable statements in the file |
| `Miss` | statements never executed by any test |
| `Branch` | branch destinations (an `if` contributes two) |
| `BrPart` | branches where only one direction was ever taken |
| `Cover` | (statements + branches taken) / (statements + branches) |
| `Missing` | line numbers never run, and arcs never taken |

`218-241` means those lines never ran. `109->118` is different and more
interesting: line 109 ran, but control **never jumped from 109 to 118** — the
`if` on line 109 was only ever entered one way. That is the null-guard nobody
tested, and it is the single most useful thing in the report.

Branch coverage is on for exactly that reason. This codebase is mostly read-models
that branch on NULL, empty and zero-denominator cases — the paths that turn into a
500 in front of a customer. Line coverage marks `if total == 0:` as covered the
moment the happy path walks past it. Branch coverage does not.

### What is excluded, and why

`.coveragerc` omits `venv/`, the test files themselves, and code no unit test can
legitimately drive: `e2e_sim.py` and `phase30_plc_simulator.py` (need a live API or
a broker), `mqtt_machine_publisher.py`, and the `phase11_*.py` scaffolding, which
is copy-paste notes-to-self that is not even importable — `phase11_routes_to_merge.py`
decorates with a bare `@app`, `phase11_model_to_add.py` subclasses an undefined
`Base`. Alembic version scripts are omitted too: they are exercised by running a
migration, not by unit tests.

Every `if __name__ == "__main__":` block is excluded from the report. Under pytest
those blocks never run by design, and counting them as missed would penalise the
exact convention CI depends on.

Nothing else is hidden. Operator scripts that *could* be tested but aren't —
`backfill_enterprise_tenants.py`, `migrate.py`, `reset_machines.py`,
`live_simulator.py` — stay in the report at 0%. Making an untested module invisible
is the failure mode this whole exercise exists to fix.

---

## The threshold

**`--cov-fail-under=78`.**

That number is derived from a measurement, not chosen as an aspiration. Measured
on 2026-08-04 against the tree this gate ships with:

```
TOTAL                            11025   1779   2756    315  81.2%
1081 passed, 3 warnings in 92.53s (0:01:32)
```

81.2% branch coverage — 1,779 statements and 315 half-taken branches unexercised.
78 sits three points under that, and the gap is sized rather than rounded:

- **It has to ratchet, not block.** A threshold at or above the current number
  turns the very next merge red for a reason unrelated to the change in it. The
  floor's job is to stop coverage sliding backwards, and it can only do that if it
  starts below where we already are.
- **Three points is roughly one run of ordinary churn.** Four measurements taken
  across an afternoon of parallel work read 79.2%, 80.7%, 81.0% and 81.2%. Some
  of that spread is failing tests in the first run — a test that raises stops
  executing lines — but most of it is simply modules landing and their tests
  landing with them: `monitoring.py` (199 statements) and `migrate.py` (56)
  appeared between the runs. A threshold within one point of current would go red
  on that, and a check that fails on noise gets deleted.
- **Three points still catches the thing worth catching.** A new module of ~200
  statements landing with no tests costs about 1.8 points on an 11,000-statement
  base — `monitoring.py` is exactly that size. Two of those, or one large one, trips
  the floor. That is the regression the threshold is for.

Raise it as the number climbs. The intended ritual is: when the total has sat
comfortably above the floor for a while, move the floor up. Never move it down to
make a build pass.

**Read the headline sceptically.** `schemas.py` (772 statements) and `models.py`
(575) report 100% because they are class declarations that execute on import; that
is a seventh of the codebase counted as covered for having been imported. The total
is a *trend line and a ratchet*, not a quality claim. The per-module column and the
`109->118` arcs are where the real information is.

### Worst-covered modules (2026-08-04)

| Module | Cover | Why it matters |
|---|---|---|
| `backfill_enterprise_tenants.py` | 0.0% | Assigns tenant ownership to legacy audit and inventory rows. Untested code that decides who owns which records. |
| `live_simulator.py` | 0.0% | Never imported by any suite. |
| `migrate.py` | 0.0% | New migration runner. |
| `reset_machines.py` | 0.0% | Operator script. |
| `factory_simulator.py` | 24.7% | 484 statements, 346 unexercised. Generates every demo tenant's data — a pitch runs on this. |
| `industrial_iot_routes.py` | 39.0% | |
| `monitoring.py` | 47.7% | Newly landed. |
| `factory_ops_routes.py` | 49.2% | 138 statements unexercised in live shop-floor endpoints. |
| `main.py` | 49.6% | Largely the startup migrations and the WebSocket loop — genuinely awkward to unit-test, and worth a look regardless. |
| `users_routes.py` | 54.5% | User creation and role assignment. |

---

## Cross-suite leakage

Under mode 1 every suite gets a fresh interpreter, so global state dies with the
process. Under pytest all ~1,000 tests share one interpreter, and module-level
globals persist across suites. Two consequences worth knowing:

1. **Tenancy.** `tenancy.install_scoping()` registers process-wide SQLAlchemy
   listeners that filter reads by a `ContextVar`. A dozen suites call it, and once
   installed it stays installed. Both listeners are no-ops when the ContextVar is
   `None`, so `conftest.py` resets it around every test. Without that, one suite
   failing mid-scope would silently filter every later suite's queries.

2. **Logging.** Importing `main` calls `logging_config.configure_logging()`, which
   installs a root handler at INFO. Under mode 1 a suite that never imports `main`
   never has logging configured, so `log.info(...)` records are dropped before
   anything formats them. Under pytest, one suite importing `main` configures
   logging for the whole session — and pytest's log-capture handler re-raises
   formatting errors instead of swallowing them.

**Known consequence of (2), open at time of writing.** A `pytest` run over the
whole backend currently reports ~21 failures in `test_mqtt_service.py`,
`test_mqtt_resilience.py` and `test_mqtt_listener.py`, all
`TypeError: not all arguments converted during string formatting`. They are not
flaky tests and not caused by the pytest setup — they are a real defect that
per-file runs cannot see. Several `print(x, y)` calls were converted to
`log.info(x, y)`, but logging treats the second argument as a `%`-format
parameter, so the record blows up when something finally formats it:

```
backend/mqtt_service.py:113   log.info("Topic:", msg.topic)
```

Sites: `mqtt_service.py` lines 23, 93, 95, 113, 116, 256, 272 and `main.py` line
535. The fix is to interpolate (`log.info("Topic: %s", msg.topic)` or an f-string).
In production this does not crash the process — logging catches it and writes
`--- Logging error ---` to stderr — but the log line is lost, which for the MQTT
ingest path means the shop-floor telemetry log is silently empty.

Reproduce in two files:

```bash
cd backend
pytest test_agent_routes.py test_mqtt_service.py    # 8 failed, 3 passed
pytest test_mqtt_service.py                         # 9 passed
python test_mqtt_service.py                         # exit 0
```

Until those call sites are fixed, `pytest -p no:logging` runs green (it disables
the log-capture plugin, so the formatting error is swallowed as it is in
production). Use it to get a coverage number; do **not** put it in `addopts` —
suppressing this permanently would hide the next broken log call too.

---

## Warnings

`pytest.ini` silences exactly two known deprecation streams — Pydantic v2's
class-based `Config` and FastAPI's `on_event` — because between them they emit
around 60 lines of summary on every run, enough to bury a warning that matters.
There is deliberately no blanket `ignore::DeprecationWarning`.

One warning is left visible on purpose: `PytestReturnNotNoneWarning` from
`test_event_bus.py::test_production_completed_moves_bom_and_logs_event`, which
returns a tuple instead of asserting. pytest has announced that will become an
error; the warning is the ratchet that stops it being forgotten.

---

## Frontend

`frontend/` runs Vitest (`npm test` → `vitest run`) over the test files in
`lib/`, plus the handful in `components/` and `app/` where the behaviour worth
pinning genuinely lives in a component or a page — a component whose bug is
"renders nothing", or a page-level routing decision such as which portal a
manufacturer lands on after signing in (ADR-0017). `coverage.include` stays at
`lib/`; see below for why widening it would make the number worse, not better.

```bash
cd frontend
npm run test:coverage
```

### The number, and what it is not

**92.6% branch coverage of `lib/`** across 223 tests. The floor is **89** — the
same ~3 points of slack, for the same reasons, as the backend's 78.

The first measurement was 74.9%, and the floor was going to be 72. Then the
modules that were actually uncovered got tests, so the floor was raised to match
rather than left where the ratchet started. That is the intended ritual.

Read the next paragraph before quoting that figure anywhere.

**It measures `lib/`, not "the frontend".** Point the same run at `components/`
and `app/` and it reads **4.5%** branch coverage, because 95 components and a
2,900-line dashboard have no unit tests at all. They are exercised by the
Playwright suite in `e2e/`, which drives a real browser and produces no Vitest
coverage data — so their real coverage is neither 4.5% nor 74.9%; it is
*unmeasured by this tool*.

Widening `include` to make the number look comprehensive would make it dishonest
instead: it would still be measuring only what Vitest ran. A coverage figure is
only meaningful next to a statement of what it covered.

| Scope | Branch coverage | Gated |
| --- | --- | --- |
| `lib/` — shared hooks and logic | 92.6% | **yes, at 89** |
| `components/`, `app/` | 4.5% by Vitest | no — see `e2e/` |

### Why branch and not statement

Same argument as the backend. Statement coverage marks `if (!res.ok)` covered the
moment the happy path walks past it; branch coverage does not. The error paths
are the entire reason these modules have tests — `#383` (empty state on error),
`#385` (double submit), `#393` (session expiry on writes) were all failures in a
branch the happy path never takes.

### Where the gaps were, and where they went

The first measurement named them precisely, which is the point of `all: true` —
a module nobody tests shows up as 0% instead of vanishing from the report:

| Module | Branch before | After | Why it mattered |
| --- | --- | --- | --- |
| `lib/utils.ts` | **0%** | 100% | never imported by a test at all |
| `lib/useLoadError.tsx` | **0%** | 100% | every conditional in the `#383` error hook untested |
| `lib/api.ts` | 56.4% | 92.3% | the layer every HTTP request goes through |
| `lib/modules.ts` | 72.7% | 100% | pack gating; `#413` was a fallback-path bug |
| `lib/live.ts` | 75.0% | 87.5% | the live socket (`#400` reconnect) |

Raise the threshold as the number climbs. Never lower it to make a build pass.

### The gate is a gate

Checked rather than assumed, the same way the backend floor was: set
`branches` to 99 and `npm run test:coverage` exits non-zero; at 89 it exits 0.
A threshold that can never fail is decoration.

Every test added in that pass was also mutation-checked — the source was broken
deliberately and the test had to notice. One survived, and the finding is
recorded in `lib/api.test.ts` rather than papered over: "navigate once on a
401 storm" is enforced by *two* independent mechanisms, so deleting either alone
cannot fail any assertion. The comment there states exactly which mutations the
test does and does not catch.
