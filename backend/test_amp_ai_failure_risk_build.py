"""The failure-risk build: deterministic, leak-free, offline, and its verdict cannot be edited.

WHAT IS ASSERTED
----------------
  1  GATE        adoption_decision passes only when EVERY criterion holds, and
                 each criterion alone can fail it: paired PR-AUC lower bound
                 > 0, ROC-AUC and precision@k not below the rule, Brier below
                 the base rate p(1-p), ECE <= 0.05, and this is the FIRST
                 recorded run on this test set (a reused test set is flagged)
  2  SAMPLES     every machine-week's label window lies inside the generated
                 span; machines already in Breakdown at as_of are excluded
  3  SMALL BUILD ``--small`` with a fixed created_at runs in under 20 s, gives
                 the same payload hash twice (once in-process, once through
                 the CLI), opens NO database connection, and writes nothing
                 to the shipped artifacts directory. The 20 s is judged
                 wherever the wall clock measures the build (wallclock.py):
                 always standalone, not under the coverage job's tracer
  4  SPLIT       the small build's split is leak-free (entities disjoint,
                 label windows inside their period); the refit never trains
                 on a row after train_end; the test set is scored once and
                 recorded in the ledger
  5  COMMITTED   the committed artifact loads against the hash PINNED in
                 predict.py; its generator_sha256 is the committed generator;
                 the committed eval JSON agrees with the artifact and its
                 ``adopted`` equals the gate recomputed from its own metrics
                 and ledger
  6  SERVING     predict() gives contributions that add back up to the logit,
                 excludes a machine already down, fits nothing, leaves the
                 artifact and the histories untouched, and scores tenant B the
                 same whether or not tenant A was scored first
  7  TAMPER      an artifact whose adoption verdict was edited (embedded hash
                 recomputed), a changed coefficient digit, or a missing file
                 -> {"status": "model_unavailable"}, never a guess
  8  CALIBRATED  when class weights win, the served probability goes through
                 the Platt calibrator stored in the artifact

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_failure_risk_build.py
"""
import copy
import dataclasses
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import timedelta

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool

import wallclock
from amp_ai.core import artifact as A
from amp_ai.core import ledger as L
from amp_ai.core import split as SP
from amp_ai.core.logistic import BinaryLogisticRegression, PlattCalibrator, sigmoid
from amp_ai.core.standardize import Standardizer
from amp_ai.failure_risk import build as B
from amp_ai.failure_risk import features as F
from amp_ai.failure_risk import history as H
from amp_ai.failure_risk import predict as PR
from amp_ai.failure_risk import synthetic as S

BACKEND = os.path.dirname(os.path.abspath(__file__))
FIXED_CREATED_AT = "2026-09-17T00:00:00+00:00"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def ci(estimate, lo=None, hi=None):
    return {"estimate": estimate, "lo": lo, "hi": hi, "n_boot": 1000, "n_valid": 1000}


def passing_evaluation():
    return {
        "rows": 1000, "positives": 60, "prevalence": 0.06, "base_rate_brier": 0.06 * 0.94,
        "model": {"roc_auc": ci(0.80), "pr_auc": ci(0.30), "precision_at_k": ci(0.25),
                  "brier": ci(0.050), "ece": ci(0.02)},
        "rule": {"roc_auc": ci(0.60), "pr_auc": ci(0.10), "precision_at_k": ci(0.12)},
        "paired_model_minus_rule": {"pr_auc": {"mean": 0.2, "estimate": 0.2, "lo": 0.05, "hi": 0.3},
                                    "roc_auc": {"mean": 0.2, "estimate": 0.2, "lo": 0.1, "hi": 0.3},
                                    "precision_at_k": {"mean": 0.13, "estimate": 0.13, "lo": 0.02, "hi": 0.2}},
    }


DATA_HASH = "a" * 64
TEST_SET = "main:test"


def one_run_ledger():
    return L.record_test_evaluation({}, data_hash=DATA_HASH, test_set=TEST_SET)


# --------------------------------------------------------------------------- 1
def section_gate():
    print("\n1. The adoption gate")
    good = passing_evaluation()
    decision = B.adoption_decision(good, one_run_ledger(), DATA_HASH, TEST_SET)
    check("all criteria hold -> adopted", decision["adopted"] is True, str(decision.get("reasons")))
    check("adopted decision carries no failure reasons", decision["reasons"] == [], str(decision["reasons"]))
    check("the decision states its scope and the synthetic-only caveat",
          decision.get("caveat") == PR.CAVEAT and "existing screen" in decision.get("scope", ""))

    def failing(label, mutate, ledger=None, expect_reused=False):
        ev = copy.deepcopy(good)
        mutate(ev)
        d = B.adoption_decision(ev, one_run_ledger() if ledger is None else ledger, DATA_HASH, TEST_SET)
        check(f"not adopted when {label}", d["adopted"] is False and len(d["reasons"]) >= 1, str(d))
        if expect_reused:
            check("a reused test set is flagged test_set_reused", d["test_set_reused"] is True, str(d))

    def set_path(ev, path, value):
        node = ev
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    failing("the paired PR-AUC lower bound is exactly 0",
            lambda ev: set_path(ev, ["paired_model_minus_rule", "pr_auc", "lo"], 0.0))
    failing("the paired PR-AUC lower bound is withheld (None)",
            lambda ev: set_path(ev, ["paired_model_minus_rule", "pr_auc", "lo"], None))
    failing("model ROC-AUC is below the rule's",
            lambda ev: set_path(ev, ["model", "roc_auc", "estimate"], 0.59))
    failing("model precision@k is below the rule's",
            lambda ev: set_path(ev, ["model", "precision_at_k", "estimate"], 0.11))
    failing("Brier equals the base-rate Brier p(1-p)",
            lambda ev: set_path(ev, ["model", "brier", "estimate"], ev["base_rate_brier"]))
    failing("ECE is above 0.05", lambda ev: set_path(ev, ["model", "ece", "estimate"], 0.0500001))
    failing("ECE is undefined", lambda ev: set_path(ev, ["model", "ece", "estimate"], None))
    twice = L.record_test_evaluation(one_run_ledger(), data_hash=DATA_HASH, test_set=TEST_SET)
    failing("the test set was already evaluated once", lambda ev: None, ledger=twice, expect_reused=True)
    failing("nothing was recorded for this test set", lambda ev: None, ledger={})

    edge = copy.deepcopy(good)
    edge["model"]["roc_auc"]["estimate"] = edge["rule"]["roc_auc"]["estimate"]
    edge["model"]["precision_at_k"]["estimate"] = edge["rule"]["precision_at_k"]["estimate"]
    edge["model"]["ece"]["estimate"] = B.ECE_MAX
    d = B.adoption_decision(edge, one_run_ledger(), DATA_HASH, TEST_SET)
    check("ties with the rule on ROC-AUC / precision@k and ECE exactly 0.05 still pass (>=, <=)",
          d["adopted"] is True, str(d["reasons"]))
    check("every criterion is reported with its value and verdict",
          len(d["criteria"]) == 6 and all({"name", "passed", "value"} <= set(c) for c in d["criteria"]),
          str(d["criteria"]))


# --------------------------------------------------------------------------- 2
def section_samples():
    print("\n2. Machine-weeks: label windows inside the data, broken machines excluded")
    cfg = B.SMALL
    days = B.as_of_days(cfg)
    last_label_end = S.as_of_for_day(days[-1]) + timedelta(days=cfg.horizon)
    check("the last as-of date's label window ends inside the generated span",
          last_label_end <= S.day_start(cfg.days), f"last day {days[-1]}, label end {last_label_end}")
    check("and no later weekly date would still fit",
          S.as_of_for_day(days[-1] + cfg.step_days) + timedelta(days=cfg.horizon) > S.day_start(cfg.days))
    check("as-of dates start at the lookback and step weekly",
          days[0] == H.LOOKBACK_DAYS and all(b - a == cfg.step_days for a, b in zip(days, days[1:])))
    check("the configuration keeps the label horizon of history.py", cfg.horizon == H.HORIZON_DAYS)
    fleet = S.generate_fleet(12, cfg.days, cfg.seed)
    samples, excluded = B.make_samples(fleet.histories, cfg)
    check("no sample is a machine already in Breakdown at its as-of",
          not any(H.in_breakdown_at(s["history"], s["as_of"]) for s in samples))
    expected_excluded = sum(1 for h in fleet.histories for d in days if H.in_breakdown_at(h, S.as_of_for_day(d)))
    check("the excluded count is every broken machine-week and nothing else",
          excluded == expected_excluded and len(samples) + excluded == len(fleet.histories) * len(days),
          f"{excluded} vs {expected_excluded}")
    check("the fixture actually contains a machine-week that had to be excluded", expected_excluded > 0)
    ok = all(s["label"] == H.breakdown_in_horizon(s["history"], s["as_of"])
             and s["features"] == F.extract(s["history"], s["as_of"]) for s in samples[:200])
    check("labels and features come from history.breakdown_in_horizon / features.extract", ok)


# --------------------------------------------------------------------------- 3 / 4
class ConnectionSpy:
    def __init__(self):
        self.events = []
        self._original_connect = None

    def _on_pool_connect(self, dbapi_connection, record):
        self.events.append("pool-connect")

    def __enter__(self):
        event.listen(Pool, "connect", self._on_pool_connect)
        self._original_connect = Engine.connect
        spy = self

        def connect(engine, *args, **kwargs):
            spy.events.append("engine-connect")
            return spy._original_connect(engine, *args, **kwargs)

        Engine.connect = connect
        return self

    def __exit__(self, *exc):
        Engine.connect = self._original_connect
        event.remove(Pool, "connect", self._on_pool_connect)
        return False


def section_small_build(judge_time=True):
    print("\n3/4. Small build: deterministic, offline, leak-free")
    with ConnectionSpy() as spy:
        started = time.perf_counter()
        first = B.build(B.SMALL, created_at=FIXED_CREATED_AT, prior_ledger={})
        elapsed = time.perf_counter() - started
    if judge_time:
        check("the small build finishes in under 20 s", elapsed < 20.0, f"{elapsed:.1f} s")
    else:
        print("     20 s budget NOT judged: a line tracer is running (the backend job enforces it)")
    print(f"     (small build: {elapsed:.1f} s)")
    check("the build opened no database connection", spy.events == [], str(spy.events))
    with ConnectionSpy() as probe:
        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        engine.dispose()
    check("the connection spy does see a real connection (the guard can fire)", len(probe.events) >= 2,
          str(probe.events))

    art = first.artifact
    check("the artifact passes structural validation", A.validate_artifact(art) is None)
    check("the returned sha256 is the payload hash", first.sha256 == A.payload_hash(art) == art["sha256"])
    check("artifact features are the extraction's feature names", art["features"] == list(F.FEATURE_NAMES))
    check("provenance: synthetic source, no consent needed, generator hash recorded",
          art["training_data_source"] == f"synthetic:{S.GENERATOR_ID} seed={B.SMALL.seed}"
          and art["consent_basis"] == B.CONSENT_BASIS and art["generator"] == S.GENERATOR_ID
          and art["generator_sha256"] == B.generator_sha256() and art["seed"] == B.SMALL.seed)

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite:///./ci.db"),
                   PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-m", "amp_ai.failure_risk.build", "--small", "--out", tmp,
                               "--created-at", FIXED_CREATED_AT], cwd=BACKEND, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=300)
        check("the CLI --small build exits 0", proc.returncode == 0, proc.stderr[-2000:])
        written = os.path.join(tmp, B.ARTIFACT_FILE)
        cli_hash = None
        if os.path.exists(written):
            cli_hash = A.load_artifact(written, expected_model_type=PR.MODEL_TYPE,
                                       expected_sha256=first.sha256)["sha256"]
        check("the CLI build gives the same payload hash as the in-process build", cli_hash == first.sha256,
              f"{cli_hash} vs {first.sha256}")
        check("the CLI wrote the eval JSON next to the artifact", os.path.exists(os.path.join(tmp, B.EVAL_FILE)))
        if os.path.exists(os.path.join(tmp, B.EVAL_FILE)):
            again = subprocess.run([sys.executable, "-m", "amp_ai.failure_risk.build", "--small", "--out", tmp,
                                    "--created-at", FIXED_CREATED_AT], cwd=BACKEND, env=env,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            with open(os.path.join(tmp, B.EVAL_FILE), encoding="utf-8") as fh:
                rerun = json.load(fh)
            check("a second build into the same directory counts a second test run (ledger carried over)",
                  again.returncode == 0 and rerun["adoption"]["test_set_reused"] is True
                  and rerun["adoption"]["adopted"] is False,
                  f"exit {again.returncode}; {rerun['adoption']}")
    shipped_dir = os.path.dirname(PR.ARTIFACT_PATH)
    shipped = {}
    for name in (B.ARTIFACT_FILE, B.EVAL_FILE):
        p = os.path.join(shipped_dir, name)
        shipped[p] = open(p, "rb").read() if os.path.exists(p) else None
    refused = subprocess.run([sys.executable, "-m", "amp_ai.failure_risk.build", "--small",
                              "--created-at", FIXED_CREATED_AT], cwd=BACKEND,
                             env=dict(os.environ, DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite:///./ci.db"),
                                      PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"),
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    clobbered = []
    for p, content in shipped.items():
        now = open(p, "rb").read() if os.path.exists(p) else None
        if now != content:
            clobbered.append(os.path.basename(p))
            if content is None:
                os.remove(p)
            else:
                with open(p, "wb") as fh:
                    fh.write(content)
    check("--small refuses to write into the shipped artifacts directory",
          refused.returncode == 2 and "refus" in (refused.stderr + refused.stdout).lower() and not clobbered,
          f"exit {refused.returncode}; clobbered (restored) {clobbered}; {refused.stderr[-300:]}")

    second = B.build(B.SMALL, created_at=FIXED_CREATED_AT, prior_ledger={})
    check("two in-process small builds give the same payload hash", second.sha256 == first.sha256,
          f"{first.sha256} vs {second.sha256}")

    cfg = B.SMALL
    train, val, test = first.split
    try:
        SP.verify_entity_time_split(train, val, test, "machine_id", "day", train_end=cfg.train_end,
                                    val_start=cfg.val_start, test_start=cfg.test_start, horizon=cfg.horizon,
                                    gap=cfg.gap)
        leak = None
    except SP.SplitError as exc:
        leak = str(exc)
    check("the build's split verifies as leak-free", leak is None, leak or "")
    check("the configuration leaves horizon + gap between train_end and test_start",
          cfg.test_start >= cfg.train_end + cfg.horizon + cfg.gap)
    ev = first.eval_doc
    check("the refit never trains on a row after train_end",
          ev["training"]["fitted_rows_max_day"] <= cfg.train_end, str(ev["training"]))
    check("the refit row count is train + validation rows up to train_end (when unweighted)",
          ev["training"]["class_weight"] is not None
          or ev["training"]["fitted_rows"] == len(SP.refit_rows(train, val, "day", train_end=cfg.train_end)),
          str(ev["training"]))
    check("the test rows are the test split", ev["metrics"]["test"]["rows"] == len(test))
    check("one ledger entry per evaluated test set (main + every variant), each counted once",
          all(L.run_count(ev["eval_ledger"], art["generator_sha256"], B.test_set_name(cfg, v)) == 1
              for v in ("main",) + cfg.variants))
    check("the eval JSON's adoption is the artifact's", ev["adoption"] == art["adoption"])
    check("the gate recomputed from the small eval JSON equals its verdict",
          B.adoption_decision(ev["metrics"]["test"], ev["eval_ledger"], ev["generator_sha256"],
                              B.test_set_name(cfg, "main")) == ev["adoption"])
    check("every misspecification variant is reported",
          set(ev["metrics"]["misspecification"]) == set(cfg.variants) and set(cfg.variants) ==
          set(S.VARIANTS) - {"main"})


# --------------------------------------------------------------------------- 5
def strict_json(path):
    def refuse(name):
        raise ValueError(f"non-finite {name}")
    with open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"), parse_constant=refuse)


def section_committed():
    print("\n5. The committed artifact and eval JSON")
    check("predict.py pins a 64-hex artifact hash",
          isinstance(PR.ARTIFACT_SHA256, str) and len(PR.ARTIFACT_SHA256) == 64)
    try:
        art = A.load_artifact(PR.ARTIFACT_PATH, expected_model_type=PR.MODEL_TYPE, expected_sha256=PR.ARTIFACT_SHA256)
    except (A.ArtifactIntegrityError, ValueError) as exc:
        check("the committed artifact loads against the pinned hash", False, str(exc))
        return
    check("the committed artifact loads against the pinned hash", art["sha256"] == PR.ARTIFACT_SHA256)
    check("it was built by the FULL configuration and seed",
          art["seed"] == B.FULL.seed == B.SEED and
          art["training_data_source"] == f"synthetic:{S.GENERATOR_ID} seed={B.SEED}")
    check("its generator_sha256 is the committed synthetic.py (rebuild after any generator change)",
          art["generator_sha256"] == B.generator_sha256(), f"{art['generator_sha256']} vs {B.generator_sha256()}")
    # LINE ENDINGS, ON ANY PLATFORM. The check above can only see a hash that
    # depends on line endings when the checkout HAS CRLF: on Windows it failed
    # when the normalisation was removed, and on Linux, where CI runs, it passed.
    # The weekly mutation fleet's first run showed that mutation surviving there
    # (#642). So feed the generator both ways explicitly and require one hash.
    source = open(S.__file__, "rb").read().replace(b"\r\n", b"\n")
    hashes, original = {}, S.__file__
    try:
        for name, data in (("LF", source), ("CRLF", source.replace(b"\n", b"\r\n"))):
            with tempfile.NamedTemporaryFile("wb", suffix=".py", delete=False) as fh:
                fh.write(data)
            S.__file__ = fh.name
            try:
                hashes[name] = B.generator_sha256()
            finally:
                os.remove(fh.name)
    finally:
        S.__file__ = original
    check("the generator hash is the same for an LF and a CRLF checkout",
          hashes["LF"] == hashes["CRLF"] == art["generator_sha256"], str(hashes))
    check("consent basis: no customer data", art["consent_basis"] == B.CONSENT_BASIS)
    ev = strict_json(os.path.join(os.path.dirname(PR.ARTIFACT_PATH), B.EVAL_FILE))
    check("the eval JSON names the pinned artifact", ev["artifact_sha256"] == PR.ARTIFACT_SHA256)
    for key in ("metrics", "baseline_metrics", "adoption", "eval_ledger", "created_at", "generator_sha256", "seed"):
        check(f"eval JSON {key} equals the artifact's", ev[key] == art[key])
    main_set = B.test_set_name(B.FULL, "main")
    recomputed = B.adoption_decision(ev["metrics"]["test"], ev["eval_ledger"], ev["generator_sha256"], main_set)
    check("adopted equals the gate recomputed from the eval JSON's own metrics and ledger",
          recomputed == ev["adoption"], f"{recomputed} vs {ev['adoption']}")
    runs = L.run_count(ev["eval_ledger"], ev["generator_sha256"], main_set)
    check("the ledger records at least one run on the shipped test set", runs >= 1, str(runs))
    before = ev["ledger_before"]
    replay = before
    for v in ("main",) + B.FULL.variants:
        replay = L.record_test_evaluation(replay, data_hash=ev["generator_sha256"], test_set=B.test_set_name(B.FULL, v))
    check("ledger_before plus this build's recorded runs is the ledger", replay == ev["eval_ledger"])
    check("the caveat is on the artifact", art["adoption"]["caveat"] == PR.CAVEAT)
    print(f"     committed: adopted={art['adoption']['adopted']}; reasons={art['adoption']['reasons']}")


# --------------------------------------------------------------------------- 6
def serving_histories():
    fleet = S.generate_fleet(40, 200, seed=424242)
    return fleet, S.as_of_for_day(190)


def section_serving():
    print("\n6. predict(): explained, pure, isolated")
    fleet, as_of = serving_histories()
    histories = fleet.histories
    art_before = open(PR.ARTIFACT_PATH, "rb").read()
    artifact, model = PR.load_model()
    snapshot = A.canonical_json(artifact)

    calls = []
    originals = (Standardizer.__dict__["fit"], BinaryLogisticRegression.__dict__["fit"],
                 PlattCalibrator.__dict__["fit"])

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("predict must not fit anything")

    Standardizer.fit = classmethod(lambda cls, *a, **k: refuse(*a, **k))
    BinaryLogisticRegression.fit = refuse
    PlattCalibrator.fit = refuse
    try:
        before = copy.deepcopy([(h.events, h.downtime, h.production, h.inspections, h.maintenance) for h in histories])
        result = PR.predict(histories, as_of)
    finally:
        Standardizer.fit, BinaryLogisticRegression.fit, PlattCalibrator.fit = originals
    check("predict returns status ok", result.get("status") == "ok", str(result)[:300])
    check("predict fitted nothing", calls == [])
    check("the histories are unchanged after scoring",
          before == [(h.events, h.downtime, h.production, h.inspections, h.maintenance) for h in histories])
    check("the artifact file is unchanged after scoring", open(PR.ARTIFACT_PATH, "rb").read() == art_before)
    check("the loaded artifact dict is unchanged after scoring", A.canonical_json(artifact) == snapshot)
    machines = result.get("machines", [])
    check("one entry per history, in order", [m["machine_id"] for m in machines] == [h.machine_id for h in histories])
    check("the result carries the caveat, version and adoption flag",
          result.get("caveat") == PR.CAVEAT and result.get("adopted") == artifact["adoption"]["adopted"]
          and result.get("model_version") == f"{PR.MODEL_NAME}@{artifact['version']}")

    scored = [m for m in machines if m["probability"] is not None]
    check("the fixture has scored machines", len(scored) >= 30, str(len(scored)))
    worst = 0.0
    for h, m in zip(histories, machines):
        if m["probability"] is None:
            continue
        row = F.extract(h, as_of)
        s = model.score(row)
        everything = model.contributions(s["z"], top=None)
        total = math.fsum(c["contribution"] for c in everything) + model.intercept
        worst = max(worst, abs(total - s["logit"]))
        if m["top_contributions"] != [dict(c, raw_value=row[c["name"]], imputed=row[c["name"]] is None)
                                      for c in everything[:3]]:
            worst = float("inf")
        if not math.isclose(m["probability"], s["probability"], rel_tol=0, abs_tol=1e-15):
            worst = float("inf")
    check("contributions + intercept == logit for every machine, top 3 served", worst <= 1e-9, str(worst))
    check("probabilities lie in (0, 1) and bands follow the artifact thresholds",
          all(0.0 < m["probability"] < 1.0 and m["band"] == model.band(m["probability"]) for m in scored))
    check("every machine also carries the rule score beside the model",
          all(isinstance(m["rule_score"], float) for m in machines))
    check("per-machine data basis says the tenant's records were read, not learned from",
          all(m["data_basis"]["learned_from"] is False and m["data_basis"]["lookback_days"] == H.LOOKBACK_DAYS
              for m in machines))

    # a machine that is down at as_of
    down = next((h for h in fleet.histories for d in range(150, 199)
                 if H.in_breakdown_at(h, S.as_of_for_day(d))), None)
    if down is None:
        check("fixture: a machine down at some as-of exists", False)
    else:
        day = next(d for d in range(150, 199) if H.in_breakdown_at(down, S.as_of_for_day(d)))
        r = PR.predict([down], S.as_of_for_day(day))["machines"][0]
        check("a machine already in Breakdown is not scored (probability None, reason given)",
              r["probability"] is None and r["band"] is None and r["excluded_reason"] == "already_in_breakdown"
              and r["top_contributions"] == [])

    tenant_a, tenant_b = histories[:20], histories[20:]
    b_alone = PR.predict(tenant_b, as_of)
    PR.predict(tenant_a, as_of)
    b_after = PR.predict(tenant_b, as_of)
    check("tenant B's scores do not depend on whether tenant A was scored first", b_alone == b_after)

    trunc = [H.truncate(h, as_of) for h in tenant_b]
    check("a database-shaped (already truncated) history scores exactly like the full one",
          PR.predict(trunc, as_of)["machines"] == b_alone["machines"])


# --------------------------------------------------------------------------- 7
def section_tamper():
    print("\n7. A tampered or missing artifact makes the model unavailable")
    fleet, as_of = serving_histories()
    with open(PR.ARTIFACT_PATH, "rb") as fh:
        original = json.loads(fh.read().decode("utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        edited = copy.deepcopy(original)
        edited["adoption"]["adopted"] = not edited["adoption"]["adopted"]
        edited["sha256"] = A.payload_hash(edited)
        path = os.path.join(tmp, "adoption.json")
        with open(path, "wb") as fh:
            fh.write(A.canonical_json(edited))
        r = PR.predict(fleet.histories[:3], as_of, artifact_path=path)
        check("edited verdict with a recomputed embedded hash -> model_unavailable",
              r.get("status") == "model_unavailable" and "machines" not in r and r.get("reason"), str(r)[:200])

        coef = copy.deepcopy(original)
        coef["parameters"]["logistic"]["coef"][0] += 1e-6
        path = os.path.join(tmp, "coef.json")
        with open(path, "wb") as fh:
            fh.write(A.canonical_json(coef))
        r = PR.predict(fleet.histories[:3], as_of, artifact_path=path)
        check("a changed coefficient -> model_unavailable", r.get("status") == "model_unavailable", str(r)[:200])

        r = PR.predict(fleet.histories[:3], as_of, artifact_path=os.path.join(tmp, "missing.json"))
        check("a missing artifact -> model_unavailable", r.get("status") == "model_unavailable", str(r)[:200])

        same = os.path.join(tmp, "copy.json")
        shutil.copyfile(PR.ARTIFACT_PATH, same)
        r = PR.predict(fleet.histories[:3], as_of, artifact_path=same)
        check("an untouched copy still scores (the refusals above are about the edits)", r.get("status") == "ok")

    art = copy.deepcopy(original)
    reordered = list(reversed(art["features"]))
    try:
        PR.LoadedModel.from_parameters(art["parameters"], reordered)
        refused = False
    except ValueError:
        refused = True
    check("an artifact whose feature list differs from features.FEATURE_NAMES is refused", refused)

    # Consistent with ITSELF but not with this code's extraction: a feature this code no longer produces.
    renamed = copy.deepcopy(original)
    names = list(renamed["features"])
    names[0] = "util_mean_7d"
    renamed["parameters"]["standardizer"]["names"] = names
    try:
        PR.LoadedModel.from_parameters(renamed["parameters"], names)
        refused = False
    except ValueError:
        refused = True
    check("an internally consistent artifact built on a feature this code does not extract is refused", refused)


# --------------------------------------------------------------------------- 8
def section_calibrated_path():
    print("\n8. The class-weighted path serves Platt-calibrated probabilities")
    # 240 machines of the small seed: the 120-machine validation split (8 positives) ranks worse than
    # chance and Platt fits a < 0 there, which is exactly the case the rejection below covers.
    cfg = dataclasses.replace(B.SMALL, n_machines=240)
    fleet = S.generate_fleet(cfg.n_machines, cfg.days, cfg.seed)
    samples, _ = B.make_samples(fleet.histories, cfg)
    train, val, _test = B.split_samples(samples, cfg)
    selection = {"l2": 1.0, "class_weight": "balanced"}
    fitted = B.fit_final(train, val, selection, cfg)
    check("a class-weighted fit stores a Platt calibrator", fitted["calibrator"] is not None,
          str(fitted.get("calibration_rejected")))
    if fitted["calibrator"] is None:
        return
    check("fixture: its slope is positive", fitted["calibrator"]["a"] > 0.0, str(fitted["calibrator"]))
    check("a class-weighted fit is not refitted on validation (validation calibrates it)",
          fitted["fitted_rows"] == len(train) and fitted["fitted_rows_max_day"] < cfg.val_start)
    params = {"standardizer": fitted["standardizer"], "logistic": fitted["logistic"],
              "calibrator": fitted["calibrator"], "bands": B.bands_for(fitted["base_rate"])}
    model = PR.LoadedModel.from_parameters(params, list(F.FEATURE_NAMES))
    row = val[0]["features"]
    s = model.score(row)
    cal = PlattCalibrator.from_dict(fitted["calibrator"])
    check("served probability = calibrator(logit), not sigmoid(logit)",
          math.isclose(s["probability"], cal.transform([s["logit"]])[0], rel_tol=0, abs_tol=1e-15)
          and not math.isclose(s["probability"], sigmoid(s["logit"]), rel_tol=0, abs_tol=1e-9),
          f"{s['probability']} vs sigmoid {sigmoid(s['logit'])}")
    unweighted = B.fit_final(train, val, {"l2": 1.0, "class_weight": None}, cfg)
    check("the unweighted fit has no calibrator and refits through refit_rows",
          unweighted["calibrator"] is None and unweighted["calibration_rejected"] is None
          and unweighted["fitted_rows"] == len(SP.refit_rows(train, val, "day", train_end=cfg.train_end)))

    # Validation on which the weighted model's ranking is REVERSED: Platt would fit a < 0.
    reversed_val = [dict(s, label=1 - s["label"]) for s in val]
    platt = PlattCalibrator().fit(
        BinaryLogisticRegression(l2=1.0, class_weight="balanced")
        .fit(Standardizer.from_dict(fitted["standardizer"]).transform([s["features"] for s in train]),
             [s["label"] for s in train])
        .decision_function(Standardizer.from_dict(fitted["standardizer"]).transform(
            [s["features"] for s in reversed_val])),
        [s["label"] for s in reversed_val])
    check("fixture: on reversed validation labels Platt's slope is negative", platt.a < 0.0, str(platt.a))
    rejected = B.fit_final(train, reversed_val, selection, cfg)
    check("a non-increasing calibrator rejects the class weights: unweighted refit shipped, reason recorded",
          rejected["class_weight"] is None and rejected["calibrator"] is None
          and "Platt slope" in (rejected["calibration_rejected"] or "")
          and rejected["fitted_rows"] == len(SP.refit_rows(train, reversed_val, "day", train_end=cfg.train_end)),
          str({k: rejected[k] for k in ("class_weight", "calibration_rejected", "fitted_rows")}))
    decreasing = dict(params, calibrator={"model": "platt_calibrator", "a": -0.25, "b": -2.0})
    try:
        PR.LoadedModel.from_parameters(decreasing, list(F.FEATURE_NAMES))
        refused = False
    except ValueError:
        refused = True
    check("predict refuses an artifact whose calibrator is decreasing", refused)


def main(judge_time=True):
    print("=" * 74)
    print("AMP-native AI failure risk: build, artifact and serving")
    print("=" * 74)
    section_gate()
    section_samples()
    section_small_build(judge_time)
    section_committed()
    section_serving()
    section_tamper()
    section_calibrated_path()
    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_amp_ai_failure_risk_build():
    """pytest entry point; CI runs this file as a standalone script. Only this
    entry asks wallclock.traced(); the standalone run always judges the budget."""
    assert main(judge_time=not wallclock.traced()) == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
