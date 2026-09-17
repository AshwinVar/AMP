"""Mutation harness for the downtime attribution engine (ADR-0020).

Each mutation is a plausible edit to contract_money, contract_terms,
contract_periods, attribution_engine or contract_statements that would let a
statement count missing data as uptime, credit a period that met its SLA, read
another factory's evidence, let a stale compute overwrite an agreed statement,
or otherwise publish a number two companies would accept wrongly. Every one
must turn at least one suite red.

IT NEVER EDITS THE WORKING TREE. The backend is copied to a temporary directory
once; each mutation is applied there, the suites run there, and the file is
restored byte for byte (CRLF stays CRLF) before the next. Another builder can
keep working in the same worktree while this runs.

SUITES (fastest first; a mutation stops at the first suite that fails):
    test_contract_money, test_contract_terms, test_contract_periods,
    test_contract_statements, test_attribution_engine

POSTGRESQL. With PG_DATABASE_URL set, test_contract_statements also runs on a
PostgreSQL scratch database for every mutation the SQLite suites did not catch.

Run: python backend/mutate_contract_engine.py            (all mutations)
     python backend/mutate_contract_engine.py --check    (patterns only, no suites)
     python backend/mutate_contract_engine.py --only purged   (labels containing it)
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = ["test_contract_money.py", "test_contract_terms.py", "test_contract_periods.py",
          "test_contract_statements.py", "test_attribution_engine.py"]
PG_SUITE = "test_contract_statements.py"

MONEY = "contract_money.py"
TERMS = "contract_terms.py"
PERIODS = "contract_periods.py"
ENGINE = "attribution_engine.py"
STATEMENTS = "contract_statements.py"


def _one(path, old, new):
    return [(path, old, new)]


MUTATIONS = [
    # --- contract_money: exact decimals, the SLA state machine, the credit -------------
    ("D accepts non-ASCII digits (\\d)", _one(MONEY,
        'DECIMAL_TEXT = re.compile(r"\\A[0-9]{1,12}\\.[0-9]{2}\\Z")',
        'DECIMAL_TEXT = re.compile(r"\\A\\d{1,12}\\.\\d{2}\\Z")')),
    ("decimal_text rounds a third place instead of refusing it", _one(MONEY,
        "        if hundredths != hundredths.to_integral_value():\n"
        "            raise MoneyError(\n"
        '                f"decimal_text: {d!r} has more than two places; quantize it first")\n'
        "        n = int(hundredths)",
        "        n = int(hundredths.to_integral_value(rounding=ROUND_HALF_UP))")),
    ("money rounds half-even (banker's rounding)", _one(MONEY,
        "        return d.quantize(CENT, rounding=ROUND_HALF_UP)",
        "        return d.quantize(CENT)")),
    ("a percentage with no denominator displays as 0.00", _one(MONEY,
        "    if den == 0:\n        return None\n",
        '    if den == 0:\n        return "0.00"\n')),
    ("percentages are floored, not rounded half-up", _one(MONEY,
        "    return (2 * num * 10000 + den) // (2 * den)",
        "    return (num * 10000) // den")),
    ("< becomes <= in the tier comparison (the boundary credits)", _one(MONEY,
        "    return num * 10000 < _pct_hundredths(pct) * den",
        "    return num * 10000 <= _pct_hundredths(pct) * den")),
    ("tiers compare ROUNDED percentages (96.999 passes as 97.00)", _one(MONEY,
        "    return num * 10000 < _pct_hundredths(pct) * den",
        "    return _hundredths_half_up(num, den) < _pct_hundredths(pct)")),
    ("the first matching tier wins, not the largest credit", _one(MONEY,
        "            if best is None or tier.credit_pct > best.credit_pct:",
        "            if best is None:")),
    ("the credit is computed in float", _one(MONEY,
        "        return quantize_money((terms.period_fee * credit_pct).scaleb(-2))",
        "        return quantize_money(Decimal(float(terms.period_fee) * float(credit_pct))"
        ".scaleb(-2))")),
    ("insufficient measurement is still evaluated", _one(MONEY,
        "    if _below(covered - unmeasured, covered, terms.min_measured_pct):",
        "    if False:")),
    ("no attributable time is still evaluated", _one(MONEY,
        "    if base == 0:\n        return not_evaluable(",
        "    if False:\n        return not_evaluable(")),
    ("pending disputes still publish an amount", _one(MONEY,
        '        credit["range_min"] = decimal_text(_amount(terms, min_pct))\n',
        '        credit["range_min"] = decimal_text(_amount(terms, min_pct))\n'
        '        credit["amount"] = credit["range_max"]\n')),
    ("'disputes resolve FACTORY' keeps disputed time in the base", _one(MONEY,
        '        sla["availability_if_disputes_factory"] = pct_display(worst, base - disputed)',
        '        sla["availability_if_disputes_factory"] = pct_display(worst, base)')),
    ("UNMEASURED counts in the availability base (as uptime)", _one(MONEY,
        '    base = covered - unmeasured - t["factory_seconds"]',
        '    base = covered - t["factory_seconds"]')),
    ("totals that do not add up are accepted", _one(MONEY,
        '    if parts != values["covered_seconds"]:', "    if False:")),

    # --- contract_terms: fail closed, by name ---------------------------------------------
    ("status_defaults may omit a down status", _one(TERMS,
        "    if set(value) != set(down_status_keys()):",
        "    if not set(value) <= set(down_status_keys()):")),
    ("'manual' is accepted as a trusted source", _one(TERMS,
        'UNTRUSTABLE_SOURCES = ("manual",)', "UNTRUSTABLE_SOURCES = ()")),
    ("no trusted source at all is accepted", _one(TERMS,
        "    if not sources:\n        raise TermsError(field, \"at least one trusted",
        "    if False:\n        raise TermsError(field, \"at least one trusted")),
    ("the generic reasons AMP writes itself are not required", _one(TERMS,
        "    if missing:\n        raise TermsError(field,", "    if False:\n        raise TermsError(field,")),
    ("term_months need not be whole periods", _one(TERMS,
        "    if term_months % period_months:", "    if False:")),
    ("reason keys are not casefolded", _one(TERMS,
        '    return unicodedata.normalize("NFC", text.casefold())',
        '    return unicodedata.normalize("NFC", text)')),
    ("generic reasons keep document order (two hashes for one meaning)", _one(TERMS,
        "        raise TermsError(field, \"must include the reasons AMP writes itself: \"\n"
        "                                + \", \".join(missing))\n"
        "    return tuple(sorted(keys))",
        "        raise TermsError(field, \"must include the reasons AMP writes itself: \"\n"
        "                                + \", \".join(missing))\n"
        "    return tuple(keys)")),
    ("credit tiers need not descend", _one(TERMS,
        "        if tiers and below >= tiers[-1].below_pct:", "        if False:")),
    ("a percentage above 100.00 is accepted", _one(TERMS,
        "    if d > _HUNDRED:", "    if False:")),
    ("an unknown field is accepted", _one(TERMS,
        "        if type(key) is not str or (key not in required and key not in optional):",
        "        if type(key) is not str:")),
    ("a reason may map to DISPUTED", _one(TERMS,
        "        if party not in PARTIES:", "        if party not in BUCKETS:")),
    ("a tier above the SLA target is accepted", _one(TERMS,
        "        if below <= _ZERO or below > target:", "        if below <= _ZERO:")),

    # --- contract_periods: anchored, whole, local ------------------------------------------
    ("a spring-forward local time moves BACK, not forward", _one(PERIODS,
        "            lo = mid\n    return hi", "            lo = mid\n    return lo")),
    ("an ambiguous local time takes its second occurrence", _one(PERIODS,
        "    early = local.replace(tzinfo=zone, fold=0)",
        "    early = local.replace(tzinfo=zone, fold=1)")),
    ("excluded dates are covered anyway", _one(PERIODS,
        "        if day not in excluded:", "        if True:")),
    ("covered hours ignore the active range", _one(PERIODS,
        "    lo = max(period.start, active_start)\n    hi = min(period.end, active_end)",
        "    lo = period.start\n    hi = period.end")),
    ("an effective end off the grid makes a partial period", _one(PERIODS,
        "    if grid[-1] != effective_end:", "    if False:")),
    ("a contract may start mid-month", _one(PERIODS,
        "    if not is_month_start(terms, starts_at):", "    if False:")),
    ("touching covered windows do not merge", _one(PERIODS,
        "        if out and s <= out[-1][1]:", "        if out and s < out[-1][1]:")),
    ("the rolling OeeWindow is imported", _one(PERIODS,
        "from datetime import datetime, time, timedelta, timezone\n",
        "from datetime import datetime, time, timedelta, timezone\n"
        "from oee_contract import OeeWindow  # noqa\n")),

    # --- attribution_engine: every covered second, one bucket -------------------------------
    ("the span gap is ignored (a span holds only to its last message)", _one(ENGINE,
        "        end = s.end + gap\n", "        end = s.end\n")),
    ("a hold runs past the next span of its own source", _one(ENGINE,
        "        if i < len(same_source):\n            end = min(end, same_source[i])",
        "        if False:\n            end = min(end, same_source[i])")),
    ("a hold is cut by ANY source's next span", [
        (ENGINE, "        starts.setdefault(s.source, set()).add(s.start)",
         '        starts.setdefault("*", set()).add(s.start)'),
        (ENGINE, "        same_source = starts[s.source]", '        same_source = starts["*"]')]),
    ("an untrusted source's span is attributed instead of refused", _one(ENGINE,
        "        if s.source not in terms.trusted_sources:", "        if False:")),
    ("no telemetry counts as AVAILABLE", _one(ENGINE,
        '            return ct.UNMEASURED, "no_telemetry", evidence',
        '            return ct.AVAILABLE, "no_telemetry", evidence')),
    ("conflicting statuses take the first span's", _one(ENGINE,
        "        return _AVAILABLE, \"+\".join(sorted(statuses))\n    return _CONFLICT, None",
        "        return _AVAILABLE, \"+\".join(sorted(statuses))\n    return _state(holding[:1])")),
    ("Running from one source and Idle from another is DISPUTED", _one(ENGINE,
        "    if statuses <= set(ct.AVAILABLE_STATUSES):", "    if False:")),
    ("an unrecognised status counts as AVAILABLE", _one(ENGINE,
        "        return _UNRECOGNISED, status", "        return _AVAILABLE, status")),
    ("the reason lead-window lower bound is skipped", _one(ENGINE,
        "        logs = [r for r in reasons if ep.start - lead <= r.at < ep.end]",
        "        logs = [r for r in reasons if r.at < ep.end]")),
    ("reasons logged after the episode still count", _one(ENGINE,
        "        logs = [r for r in reasons if ep.start - lead <= r.at < ep.end]",
        "        logs = [r for r in reasons if ep.start - lead <= r.at]")),
    ("generic reasons count as explicit", _one(ENGINE,
        "    explicit = [k for k in (ct.reason_key(log.reason) for log in logs)\n"
        "                if k not in terms.generic_reasons]",
        "    explicit = [k for k in (ct.reason_key(log.reason) for log in logs)]")),
    ("an unmapped reason falls back to the status default", _one(ENGINE,
        '        return ct.DISPUTED, "unmapped_reason"',
        '        return terms.status_defaults[status], "unmapped_reason"')),
    ("reasons for both parties pick one", _one(ENGINE,
        "    if len(parties) > 1:\n        return ct.DISPUTED, \"conflicting_reasons\"",
        "    if False:\n        return ct.DISPUTED, \"conflicting_reasons\"")),
    ("unlinked time outranks a resolved dispute", _one(ENGINE,
        "        i = bisect.bisect_right(dispute_starts, a) - 1\n",
        "        if a >= until:\n"
        "            return ct.UNMEASURED, \"installation_unlinked\", {\"linkage\": {\n"
        "                \"coverage_ended_at\": canonical.ts(m.coverage_ended_at),\n"
        "                \"reason\": m.coverage_end_reason}}\n"
        "        i = bisect.bisect_right(dispute_starts, a) - 1\n")),
    ("an open dispute is ignored", _one(ENGINE,
        '            return ct.DISPUTED, f"open_dispute:#{d.id}", _dispute_evidence(d)',
        "            pass")),
    ("overlapping disputes are accepted", _one(ENGINE,
        "            if cur.start < prev.end:", "            if False:")),
    ("a resolved dispute needs no valid resolution bucket", _one(ENGINE,
        "        if d.status == DISPUTE_RESOLVED and d.resolution_bucket not in ct.RESOLUTION_BUCKETS:",
        "        if False:")),
    ("time after the unlink is attributed as before", _one(ENGINE,
        "        if a >= until:\n            return ct.UNMEASURED, \"installation_unlinked\"",
        "        if False:\n            return ct.UNMEASURED, \"installation_unlinked\"")),
    ("coverage_end ignores the unlink stamp", _one(ENGINE,
        "    return max(period.start, min(period.end, coverage_ended_at))",
        "    return period.end")),
    ("span evidence end is not clipped to the period", _one(ENGINE,
        '            "end": canonical.ts(min(s.end, period.end))}',
        '            "end": canonical.ts(s.end)}')),
    ("span evidence start is not clipped", _one(ENGINE,
        '            "start": canonical.ts(max(s.start, period.start - gap)),',
        '            "start": canonical.ts(s.start),')),
    ("neighbours merge without comparing evidence", _one(ENGINE,
        "                    (last.bucket, last.cause, last.evidence) == (bucket, cause, evidence):",
        "                    (last.bucket, last.cause) == (bucket, cause):")),
    ("the engine accepts spans outside the read window", _one(ENGINE,
        "        if s.end < low or s.start >= until:", "        if False:")),
    ("the engine accepts reasons outside the reason window", _one(ENGINE,
        "        if window is None or not window[0] <= r.at < window[1]:", "        if False:")),
    ("episodes are not clipped to the coverage end", _one(ENGINE,
        "    return [Episode(e.start, min(e.end, until), e.status) for e in episodes if e.start < until]",
        "    return episodes")),
    ("fractional seconds are accepted", _one(ENGINE,
        "    if type(value) is not datetime or value.tzinfo is not None or value.microsecond:",
        "    if type(value) is not datetime or value.tzinfo is not None:")),
    ("the covered-seconds invariant is not checked", _one(ENGINE,
        "    if sum(s.seconds for s in segments) != covered_seconds:", "    if False:")),

    # --- contract_statements: reads, persistence, acceptance --------------------------------
    ("the span read drops the tenant filter", _one(STATEMENTS,
        "              .filter(S.tenant_code == tenant_code,\n                      S.machine_id",
        "              .filter(S.machine_id")),
    ("the reason read drops the tenant filter", _one(STATEMENTS,
        "              .filter(L.tenant_code == tenant_code,\n                      L.machine_id",
        "              .filter(L.machine_id")),
    ("the span read drops its upper time bound", _one(STATEMENTS,
        "                      S.span_start < start_before,", "                      S.id == S.id,")),
    ("the span read drops its lower time bound", _one(STATEMENTS,
        "                      S.span_end >= end_at_least)", "                      S.id == S.id)")),
    ("the span read drops the trusted-source filter", _one(STATEMENTS,
        "                      S.source.in_(list(sources)),", "                      S.id == S.id,")),
    ("the reason read drops its lower time bound", _one(STATEMENTS,
        "                      L.created_at >= lo,", "                      L.id == L.id,")),
    ("the reason read drops its upper time bound", _one(STATEMENTS,
        "                      L.created_at < hi)", "                      L.id == L.id)")),
    ("the dispute read drops its upper window bound", _one(STATEMENTS,
        "                      CD.window_start < period.end,", "                      CD.id == CD.id,")),
    ("the dispute read drops its lower window bound", _one(STATEMENTS,
        "                      CD.window_end > period.start)", "                      CD.id == CD.id)")),
    ("the dispute read is not bound to the statement", _one(STATEMENTS,
        "                      CD.statement_id == statement.id,", "                      CD.id == CD.id,")),
    ("span bounds use the period end, not the unlink", _one(STATEMENTS,
        "        end_at_least, start_before = ae.span_read_window(period, stamp, gap)",
        "        end_at_least, start_before = ae.span_read_window(period, None, gap)")),
    ("spans are not truncated to whole seconds", _one(STATEMENTS,
        "                              start=canonical.utc_seconds(r.span_start),\n"
        "                              end=canonical.utc_seconds(r.span_end))",
        "                              start=r.span_start,\n"
        "                              end=r.span_end)")),
    ("every acceptance row counts, valid or not", _one(STATEMENTS,
        "                and canonical.acceptance_is_valid(row, statement):",
        "                and row.content_hash:")),
    ("one party's acceptance makes the statement agreed", _one(STATEMENTS,
        '    out["agreed"] = all(out[party] is not None for party in contract_terms.PARTIES)',
        '    out["agreed"] = any(out[party] is not None for party in contract_terms.PARTIES)')),
    ("SETTLE_SECONDS is removed", _one(STATEMENTS,
        "    if now < period.end + timedelta(seconds=settle_seconds()):\n        raise PeriodNotClosed(",
        "    if now < period.end:\n        raise PeriodNotClosed(")),
    ("expired evidence is recomputed anyway", _one(STATEMENTS,
        "    if evidence_expired(period, now):\n        raise EvidenceExpired(",
        "    if False:\n        raise EvidenceExpired(")),
    ("evidence expiry forgets the span gap", _one(STATEMENTS,
        "    return period.start - timedelta(seconds=span_gap_seconds()) < now - timedelta(days=days)",
        "    return period.start < now - timedelta(days=days)")),
    ("an agreed statement is not frozen", _one(STATEMENTS,
        '    if acceptance_state(db, statement)["agreed"]:', "    if False:")),
    ("the conditional update does not name the revision", _one(STATEMENTS,
        "        .where(CS.id == statement.id, CS.content_hash == old_hash,\n"
        "               CS.revision == old_revision)",
        "        .where(CS.id == statement.id, CS.content_hash == old_hash)")),
    ("the conditional update does not name the old hash", _one(STATEMENTS,
        "        .where(CS.id == statement.id, CS.content_hash == old_hash,\n"
        "               CS.revision == old_revision)",
        "        .where(CS.id == statement.id,\n               CS.revision == old_revision)")),
    ("a lost update is not detected", _one(STATEMENTS,
        "    if result.rowcount != 1:", "    if False:")),
    ("the revision does not increase", _one(STATEMENTS,
        "revision=CS.revision + 1,", "revision=CS.revision,")),
    ("records are not replaced when the content changes", _one(STATEMENTS,
        "    db.refresh(statement)\n    _replace_records(db, statement, segments)\n",
        "    db.refresh(statement)\n")),
    ("a no-op recompute is audited", _one(STATEMENTS,
        "    if statement.content_hash == new_hash:\n        return ComputeResult(statement, False, False)",
        "    if statement.content_hash == new_hash:\n"
        "        _audit(db, contract, actor, statement, None, None)\n"
        "        return ComputeResult(statement, False, False)")),
    ("only the factory's audit trail is written", _one(STATEMENTS,
        "    for tenant in (contract.factory_tenant_code, oem_auth.sentinel_tenant(contract.oem_code)):",
        "    for tenant in (contract.factory_tenant_code,):")),
    ("a statement exists before the factory accepts", _one(STATEMENTS,
        "    return (contract.status in ACCEPTED_CONTRACT_STATUSES\n"
        "            and contract.factory_accepted_at is not None)",
        "    return True")),
    ("proposed term versions govern periods", _one(STATEMENTS,
        "              .filter(TV.contract_id == contract.id, TV.status == TERMS_ACCEPTED)",
        "              .filter(TV.contract_id == contract.id)")),
    ("the lowest applicable version governs", _one(STATEMENTS,
        "    version = applicable[-1]", "    version = applicable[0]")),
    ("the parties' accepted hashes are not checked", _one(STATEMENTS,
        "    if not (digest == version.terms_hash == version.oem_accepted_hash\n"
        "            == version.factory_accepted_hash):",
        "    if not digest == version.terms_hash:")),
    ("a machine accepted for another factory is read", _one(STATEMENTS,
        "        if r.factory_tenant_at_acceptance != contract.factory_tenant_code \\\n"
        "                or type(r.machine_id_at_acceptance) is not int:",
        "        if type(r.machine_id_at_acceptance) is not int:")),
    ("a read bound to another tenant is allowed", _one(STATEMENTS,
        "    if bound is not None and bound != contract.factory_tenant_code:", "    if False:")),
    ("an unlink after the period changes that period's bytes", _one(STATEMENTS,
        '            "coverage_end": canonical.ts(stamp) if stamp is not None and stamp < period.end',
        '            "coverage_end": canonical.ts(stamp) if stamp is not None')),
    ("verify does not compare the bytes with the stored hash", _one(STATEMENTS,
        "        if blob_hash != statement.content_hash:", "        if False:")),
    ("verify does not compare the records with the bytes", _one(STATEMENTS,
        "            consistency = (CONSISTENT if records_hash is not None and records_hash == expected",
        "            consistency = (CONSISTENT if True")),
    ("an amendment may change the period grid", _one(STATEMENTS,
        "    if _grid(terms) != _grid(_parse_version(versions[0])):", "    if False:")),
    ("a statement purged at offboarding is recomputed over its kept hash", _one(STATEMENTS,
        "    if statement.canonical_json is None:\n        raise ContentPurged(",
        "    if False:\n        raise ContentPurged(")),
    ("verify recomputes a purged statement", _one(STATEMENTS,
        "        if statement.canonical_json is None:\n            live[\"reason\"] = CONTENT_PURGED",
        "        if False:\n            live[\"reason\"] = CONTENT_PURGED")),
    ("a lost insert race is not re-read", _one(STATEMENTS,
        "            statement = _statement_row(db, contract, period)\n            if statement is None:",
        "            statement = None\n            if statement is None:")),
]

# A mutation another guard already covers. Each needs a reason that survives reading.
EXPECTED_SURVIVORS = {
    "the covered-seconds invariant is not checked":
        "DEFENCE IN DEPTH THAT NO INPUT CAN TRIP. Segments are built by cutting each "
        "covered interval at sorted points and classifying every piece, so they tile "
        "the covered time by construction; the check exists to turn a future bug in "
        "that construction into an error. The randomized oracle test independently "
        "asserts the tiling and the seconds sum, so removing the in-code check loses "
        "no detection a test could observe.",
    "the conditional update does not name the old hash":
        "SHADOWED BY THE REVISION CONDITION. Every write that changes content_hash "
        "increments revision in the same UPDATE, so a row whose revision still equals "
        "the one read cannot have a different hash. The hash stays in the WHERE as "
        "the plan specifies; the revision half is mutation-tested on its own (C1 race).",
}


def _read(root, path):
    with open(os.path.join(root, path), "rb") as fh:
        return fh.read()


def _write(root, path, data):
    with open(os.path.join(root, path), "wb") as fh:
        fh.write(data)


def _apply(source_bytes, old, new):
    """Replace once, speaking the file's own newline convention."""
    text = source_bytes.decode("utf-8")
    if "\r\n" in text:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    count = text.count(old)
    if count != 1:
        return None, count
    return text.replace(old, new, 1).encode("utf-8"), 1


def _copy_backend():
    root = tempfile.mkdtemp(prefix="amp_mutate_engine_")
    target = os.path.join(root, "backend")
    shutil.copytree(HERE, target, ignore=shutil.ignore_patterns(
        "__pycache__", "*.db", ".sweep", "node_modules", ".pytest_cache"))
    return root, target


def run_suites(root, stop_at_first=True):
    failed = []
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci.db", PYTHONIOENCODING="utf-8")
    env.pop("PG_DATABASE_URL", None)
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=root, env=env, timeout=900)
        if proc.returncode != 0:
            failed.append(suite)
            if stop_at_first:
                return failed
    pg = os.environ.get("PG_DATABASE_URL")
    if pg and not failed:
        env = dict(os.environ, DATABASE_URL=pg, PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, PG_SUITE], capture_output=True, text=True,
                              errors="replace", cwd=root, env=env, timeout=900)
        if proc.returncode != 0:
            failed.append(PG_SUITE + " (PostgreSQL)")
    return failed


def main():
    global MUTATIONS
    check_only = "--check" in sys.argv
    if "--only" in sys.argv:
        needle = sys.argv[sys.argv.index("--only") + 1]
        MUTATIONS = [m for m in MUTATIONS if needle in m[0]]
        if not MUTATIONS:
            print(f"--only {needle!r} matches no mutation")
            return 2
    copy_root, root = _copy_backend()
    try:
        paths = sorted({p for _, edits in MUTATIONS for p, _, _ in edits})
        originals = {p: _read(root, p) for p in paths}
        bad = []
        for label, edits in MUTATIONS:
            text = dict(originals)
            for path, old, new in edits:
                result, count = _apply(text[path], old, new)
                if result is None:
                    bad.append(f"{label}: pattern hits {count} in {path}")
                    break
                text[path] = result
        unknown = set(EXPECTED_SURVIVORS) - {label for label, _ in MUTATIONS}
        if unknown and "--only" not in sys.argv:
            bad.extend(f"expected survivor names no mutation: {u}" for u in unknown)
        if bad:
            print("PATTERN PROBLEMS:")
            for b in bad:
                print("   *", b)
            return 2
        if check_only:
            print(f"all {len(MUTATIONS)} mutation patterns apply exactly once")
            return 0

        baseline = run_suites(root, stop_at_first=False)
        if baseline:
            print(f"ABORT: suites already failing before any mutation: {baseline}")
            return 2
        suites = SUITES + ([PG_SUITE + " (PostgreSQL)"] if os.environ.get("PG_DATABASE_URL")
                           else [])
        print(f"baseline: all {len(suites)} suites green in a copy of backend/ "
              f"({', '.join(suites)})\n")
        print(f"{'mutation':<74} {'verdict':<9} caught by")
        print("-" * 118)

        survived = []
        caught = shadowed = 0
        for label, edits in MUTATIONS:
            mutated = dict(originals)
            for path, old, new in edits:
                mutated[path], _ = _apply(mutated[path], old, new)
            touched = {p for p, _, _ in edits}
            try:
                for p in touched:
                    _write(root, p, mutated[p])
                failing = run_suites(root)
            finally:
                for p in touched:
                    _write(root, p, originals[p])
            if failing:
                verdict, note = "caught", ", ".join(
                    s.replace("test_", "").replace(".py", "") for s in failing)
                caught += 1
            elif label in EXPECTED_SURVIVORS:
                verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:40] + "..."
                shadowed += 1
            else:
                verdict, note = "SURVIVED", "-- nothing --"
                survived.append(label)
            print(f"{label[:74]:<74} {verdict:<9} {note}", flush=True)

        dirty = [p for p in paths if _read(root, p) != originals[p]]
        if dirty:
            print(f"\nERROR: files not restored byte for byte in the copy: {dirty}")
            return 3
        print()
        if survived:
            print(f"{len(survived)} mutation(s) not caught:")
            for s in survived:
                print("   *", s)
            return 1
        print(f"ALL {len(MUTATIONS)} MUTATIONS CAUGHT ({caught}) OR SHADOWED WITH A STATED "
              f"REASON ({shadowed}); the working tree was never edited")
        return 0
    finally:
        shutil.rmtree(copy_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
