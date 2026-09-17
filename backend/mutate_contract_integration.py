"""Mutation harness for the shared pieces service contracts integrate with (ADR-0020).

The engine (mutate_contract_engine.py) and the routes (mutate_service_contracts.py)
have their own harnesses. This one covers what joins them to the rest of AMP:

    platform_routes.log_audit(tenant_code=, commit=)     test_audit_explicit_tenant
    oem_sharing.contract_statement_visible / bound_factory_read / widen_grants
                                                          test_oem_sharing_helpers
    telemetry_coverage and its writers                    test_telemetry_coverage
    contract_linkage and the release route                test_contract_linkage
    offboard_tenant._close_service_contracts              test_contract_offboarding
    retention's span policy and its evidence floor        test_retention
    oem_auth contract capabilities                        test_oem_authorization
    the contracts view in the core pack                   test_module_manifest

Each mutation is a plausible edit that would let a contract action lose its
audit row, show a manufacturer data the factory did not share, count missing
telemetry as a status, let a changed installation go on feeding a statement, or
destroy what two parties agreed. Every one must turn a suite red, or be listed
in EXPECTED_SURVIVORS with the reason another guard already covers it.

IT NEVER EDITS THE WORKING TREE: the backend is copied to a temporary directory,
each mutation applied and restored byte for byte there.

Run: python backend/mutate_contract_integration.py            (all mutations)
     python backend/mutate_contract_integration.py --check    (patterns only)
     python backend/mutate_contract_integration.py --only span
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = ["test_audit_explicit_tenant.py", "test_oem_authorization.py",
          "test_module_manifest.py", "test_retention.py", "test_oem_sharing_helpers.py",
          "test_telemetry_coverage.py", "test_contract_linkage.py",
          "test_contract_offboarding.py"]

AUDIT = "platform_routes.py"
SHARING = "oem_sharing.py"
COVERAGE = "telemetry_coverage.py"
MQTT = "mqtt_service.py"
SIM = "factory_simulator.py"
LINKAGE = "contract_linkage.py"
EQUIPMENT = "connected_equipment_routes.py"
OFFBOARD = "offboard_tenant.py"
RETENTION = "retention.py"
AUTH = "oem_auth.py"
MANIFEST = "modules.json"


def _one(path, old, new):
    return [(path, old, new)]


MUTATIONS = [
    # --- log_audit ------------------------------------------------------------------
    ("commit=False swallows a failure and rolls back", _one(AUDIT,
        "    if not commit:\n        db.add(_row())\n        return\n",
        "    if not commit:\n        try:\n            db.add(_row())\n        except Exception:\n"
        "            db.rollback()\n        return\n")),
    ("commit=False commits on its own", _one(AUDIT,
        "    if not commit:\n        db.add(_row())\n        return\n",
        "    if not commit:\n        db.add(_row())\n        db.commit()\n        return\n")),
    ("the explicit tenant is dropped", _one(AUDIT,
        "            tenant_code=tenant_code,\n        )",
        "        )")),

    # --- oem_sharing helpers -------------------------------------------------------
    ("any grant counts as consent to see statements", _one(SHARING,
        "    return SHARE_DOWNTIME in grants_for(db, contract.oem_code,\n"
        "                                        contract.factory_tenant_code)",
        "    return bool(grants_for(db, contract.oem_code, contract.factory_tenant_code))")),
    ("bound_factory_read accepts None or a sentinel", _one(SHARING,
        "            or oem_auth.is_sentinel(tenant_code)):\n"
        "        raise ValueError(\"bound_factory_read needs a factory tenant code\")",
        "            or oem_auth.is_sentinel(tenant_code)) and False:\n"
        "        raise ValueError(\"bound_factory_read needs a factory tenant code\")")),
    ("bound_factory_read does not restore the caller's binding", _one(SHARING,
        "    try:\n        yield\n    finally:\n        tenancy.reset_current_tenant(token)",
        "    yield")),
    ("widen_grants replaces the policy instead of widening it", _one(SHARING,
        "    policy.grants = \",\".join(sorted(existing | wanted))",
        "    policy.grants = \",\".join(sorted(wanted))")),
    ("widen_grants accepts an unknown grant", _one(SHARING,
        "    if unknown:\n        raise ValueError(f\"Unknown sharing grants: {', '.join(unknown)}\")",
        "    if False:\n        raise ValueError(f\"Unknown sharing grants: {', '.join(unknown)}\")")),
    ("widen_grants commits its own audit row", _one(SHARING,
        "        tenant_code=tenant_code, commit=False)\n    return policy",
        "        tenant_code=tenant_code, commit=True)\n    return policy")),
    ("widen_grants files its audit row under whatever tenant is bound", _one(SHARING,
        "        tenant_code=tenant_code, commit=False)\n    return policy",
        "        commit=False)\n    return policy")),
    ("visible_machine binds the tenant by hand again", _one(SHARING,
        "    with bound_factory_read(installation.factory_tenant_code):\n"
        "        machine = (db.query(models.Machine)",
        "    import tenancy\n    tenancy.set_current_tenant(installation.factory_tenant_code)\n"
        "    if True:\n        machine = (db.query(models.Machine)")),

    # --- telemetry_coverage -------------------------------------------------------
    ("a span is extended across any gap", _one(COVERAGE,
        "                        S.span_end >= at - timedelta(seconds=SPAN_GAP_SECONDS))",
        "                        S.span_end >= at - timedelta(days=36500))")),
    ("span_end is written on every message", _one(COVERAGE,
        "        if at - latest.span_end >= timedelta(seconds=SPAN_WRITE_RESOLUTION_SECONDS):",
        "        if True:")),
    ("message_count moves only when span_end is written", _one(COVERAGE,
        "        values = {\"message_count\": S.message_count + 1}",
        "        values = {}")),
    ("the latest span is looked up across sources", _one(COVERAGE,
        "                        S.source == source,\n", "")),
    ("the latest span is looked up across tenants", _one(COVERAGE,
        "                .filter(S.tenant_code == tenant_code,\n                        S.machine_id",
        "                .filter(S.machine_id")),
    ("a different status extends the span", _one(COVERAGE,
        "    if latest is not None and latest.status == text:",
        "    if latest is not None:")),
    ("the manual PATCH is a trusted span source", _one(COVERAGE,
        "SOURCES = (MQTT, IOT, INDUSTRIAL_GATEWAY, SIMULATOR)",
        "SOURCES = (MQTT, IOT, INDUSTRIAL_GATEWAY, SIMULATOR, \"manual\")")),
    ("a message with no status becomes Idle", _one(COVERAGE,
        "    if raw is None:\n        return None\n    text = str(raw).strip()\n    if not text:\n"
        "        return None",
        "    if raw is None:\n        return \"Idle\"\n    text = str(raw).strip()\n    if not text:\n"
        "        return \"Idle\"")),
    ("an unrecognised status is stored whole", _one(COVERAGE,
        "    return normalize_machine_status(text) or text[:STATUS_MAX_CHARS]",
        "    return normalize_machine_status(text) or text")),
    ("the message time is not truncated to UTC seconds", _one(COVERAGE,
        "    at = canonical.utc_seconds(at)\n    text = span_status(status)",
        "    text = span_status(status)")),
    ("an OEM sentinel may own a span", _one(COVERAGE,
        "    if (not isinstance(tenant_code, str) or not tenant_code.strip()\n"
        "            or oem_auth.is_sentinel(tenant_code)):",
        "    if (not isinstance(tenant_code, str) or not tenant_code.strip()):")),
    ("MQTT writes no span", _one(MQTT,
        "        telemetry_coverage.record_message(\n"
        "            db, route.tenant, machine.id, telemetry_coverage.MQTT,\n"
        "            payload.get(\"status\"), telemetry_coverage.received_at())\n", "")),
    ("MQTT records the defaulted status, not the raw one", _one(MQTT,
        "            payload.get(\"status\"), telemetry_coverage.received_at())",
        "            payload.get(\"status\", \"Idle\"), telemetry_coverage.received_at())")),
    ("the simulator heartbeat runs with no tenant bound", _one(SIM,
        "    if not tenant:\n        raise ValueError(\"tick_status_heartbeat needs a bound tenant\")",
        "    if False:\n        raise ValueError(\"tick_status_heartbeat needs a bound tenant\")")),

    # --- contract_linkage ---------------------------------------------------------
    ("the linkage listener is never registered", _one(LINKAGE,
        "    _installed[0] = True\n    event.listen(Session, \"before_flush\", _before_flush)",
        "    _installed[0] = True")),
    ("releasing equipment does not end coverage", _one(EQUIPMENT,
        "    contract_linkage.end_coverage(db, inst.id, contract_linkage.INSTALLATION_RELEASED)\n",
        "")),
    ("a later change moves the first stamp", _one(LINKAGE,
        "                          SCM.coverage_ended_at.is_(None),\n", "")),
    ("a same-value write ends coverage", _one(LINKAGE,
        "    if old_machine != new_machine:\n"
        "        return MACHINE_UNLINKED if new_machine is None else MACHINE_RELINKED\n"
        "    return None",
        "    return MACHINE_UNLINKED")),
    ("coverage of an ended contract is stamped", _one(LINKAGE,
        "            if now >= contract_periods.contract_effective_end(contract):\n"
        "                continue\n", "")),
    ("the coverage end is audited for the factory only", _one(LINKAGE,
        "            for tenant in (contract.factory_tenant_code,\n"
        "                           oem_auth.sentinel_tenant(contract.oem_code)):\n"
        "                platform_routes.log_audit(session, \"system\"",
        "            for tenant in (contract.factory_tenant_code,):\n"
        "                platform_routes.log_audit(session, \"system\"")),
    ("no CoverageEnded event is published", _one(LINKAGE,
        "            oem_events.publish(event_bus, session, oem_events.CoverageEnded(",
        "            (lambda *a: None)(event_bus, session, oem_events.CoverageEnded(")),

    # --- offboarding ---------------------------------------------------------------
    ("offboarding keeps the statement content", _one(OFFBOARD,
        "            if statement.canonical_json is not None:\n"
        "                statement.canonical_json = None\n",
        "            if statement.canonical_json is not None:\n"
        "                statement.canonical_json = statement.canonical_json\n")),
    ("offboarding keeps the disputes", _one(OFFBOARD,
        "        for dispute in db.query(CD).filter(CD.contract_id == contract.id).all():\n"
        "            db.delete(dispute)\n",
        "        for dispute in db.query(CD).filter(CD.contract_id == contract.id).all():\n"
        "            pass\n")),
    ("offboarding keeps the attribution records", _one(OFFBOARD,
        "                db.delete(record)\n", "                pass\n")),
    ("offboarding terminates mid-period", _one(OFFBOARD,
        "    return min(boundary, contract.ends_at)", "    return min(now, contract.ends_at)")),
    ("offboarding leaves an accepted contract accepted", _one(OFFBOARD,
        "                contract.status = \"terminated\"\n", "")),
    ("offboarding closes contracts without an audit row", _one(OFFBOARD,
        "            platform_routes.log_audit(db, \"system:offboarding\", CLOSED_AT_OFFBOARDING,",
        "            (lambda *a, **k: None)(db, \"system:offboarding\", CLOSED_AT_OFFBOARDING,")),
    ("offboarding never closes contracts at all", _one(OFFBOARD,
        "        counts.update(_close_service_contracts(db, code))\n", "")),

    # --- retention, capabilities, manifest ------------------------------------------
    ("a --days override shortens the span evidence window", _one(RETENTION,
        "    if policy.evidence and days is not None:", "    if False:")),
    ("spans are kept 180 days", _one(RETENTION,
        "        models.MachineTelemetrySpan, \"span_end\", 400,",
        "        models.MachineTelemetrySpan, \"span_end\", 180,")),
    ("a viewer may manage contracts", _one(AUTH,
        "    OEM_VIEWER: {\"read_fleet\", \"read_contracts\"},",
        "    OEM_VIEWER: {\"read_fleet\", \"read_contracts\", \"manage_contracts\"},")),
    ("a service manager may sign contracts", _one(AUTH,
        "                          \"read_contracts\", \"manage_contracts\"},",
        "                          \"read_contracts\", \"manage_contracts\", \"sign_contracts\"},")),
    ("the contracts view leaves the core pack", _one(MANIFEST,
        "        {\"key\": \"connected\", \"label\": \"Connected Equipment\", \"icon\": \"◈\"},\n"
        "        {\"key\": \"contracts\", \"label\": \"Service Contracts\", \"icon\": \"§\"}\n",
        "        {\"key\": \"connected\", \"label\": \"Connected Equipment\", \"icon\": \"◈\"}\n")),
]

# A mutation another guard already covers. Each needs a reason that survives reading.
EXPECTED_SURVIVORS = {}


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
    root = tempfile.mkdtemp(prefix="amp_mutate_integration_")
    target = os.path.join(root, "backend")
    shutil.copytree(HERE, target, ignore=shutil.ignore_patterns(
        "__pycache__", "*.db", ".sweep", "node_modules", ".pytest_cache"))
    return root, target


def run_suites(root, stop_at_first=True):
    failed = []
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci.db", PYTHONIOENCODING="utf-8",
               CONTRACT_FAIL_FAST="1" if stop_at_first else "")
    for suite in SUITES:
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=root, env=env, timeout=900)
        if proc.returncode != 0:
            failed.append(suite)
            if stop_at_first:
                return failed
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
        print(f"baseline: all {len(SUITES)} suites green in a copy of backend/\n")
        print(f"{'mutation':<74} {'verdict':<9} caught by")
        print("-" * 118)
        survived, caught, shadowed = [], 0, 0
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
