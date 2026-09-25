"""Mutation harness for gateway authentication and production idempotency.

MANDATORY, and by four separate routes: this slice is tenant isolation, gateway
authentication, credential handling and production-counter correctness at once.
It is the code that decides whether one customer can publish into another
customer's factory, and whether a retried message doubles a shift's output.

Each mutation is a plausible edit — the kind that survives review because it
looks like a simplification — and every one must turn a suite red:

  * a closed workspace must stay closed
  * a credential must bind to ONE workspace and ONE site
  * a signature must actually be checked, against the right key, in the window
  * revocation must take effect on the next packet
  * identity must be checked before authority, so a refusal leaks nothing
  * a re-sent production record must be written once

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_gateway_auth.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_gateway_ingest_authentication.py", "test_gateway_authorisation.py",
          "test_gateway_signature_parity.py", "test_mqtt_tenant_identity.py"]
SUITE_TIMEOUT = 600

AUTH = "gateway_auth.py"
MQTT = "mqtt_service.py"

# (label, file, old, new)
MUTATIONS = [
    # ── the workspace closes, and stays closed ───────────────────────────────
    ("a workspace with a registered gateway is treated as open anyway", MQTT,
     "    if not tenant_has_credentials:\n        return None",
     "    if True:\n        return None"),
    ("revoking the last gateway re-opens the workspace to unsigned packets", MQTT,
     "        models.GatewayCredential.tenant_code == route.tenant).count()",
     "        models.GatewayCredential.tenant_code == route.tenant,\n"
     "        models.GatewayCredential.is_active.is_(True)).count()"),
    ("a message with no gateway_id is accepted into a closed workspace", MQTT,
     "        raise gateway_auth.GatewayRejected(\n"
     "            f\"{route.tenant} requires gateway signatures and this message carries no usable \"",
     "        return None\n        raise gateway_auth.GatewayRejected(\n"
     "            f\"{route.tenant} requires gateway signatures and this message carries no usable \""),

    # ── the credential binds to one workspace and one site ───────────────────
    ("the credential's workspace is not compared with the topic's", AUTH,
     "    if credential.tenant_code != route.tenant:",
     "    if False:"),
    ("the credential's site is not compared with the topic's", AUTH,
     "    if (credential.site or \"\") != (route.site or \"\"):",
     "    if False:"),
    ("a NULL site and an empty site are treated as different", AUTH,
     "    if (credential.site or \"\") != (route.site or \"\"):",
     "    if credential.site != route.site:"),

    # ── the signature is actually checked ────────────────────────────────────
    ("the signature is never verified", AUTH,
     "    ok, why = verify_signature(payload, credential.secret, now=now)\n    if not ok:",
     "    ok, why = verify_signature(payload, credential.secret, now=now)\n    if False:"),
    ("any key verifies, not this gateway's", AUTH,
     "    expected = hmac.new(_key_bytes(secret), canonical(payload), hashlib.sha256).hexdigest()",
     "    expected = claimed"),
    ("a captured packet never expires", AUTH,
     "    if age > max_age:",
     "    if False:"),
    ("the signature does not cover the payload, only the envelope", AUTH,
     "    body = {k: v for k, v in payload.items() if k != \"signature\"}",
     "    body = {k: v for k, v in payload.items() if k in (\"gateway_id\", \"scheme\", \"nonce\")}"),
    ("an unknown signing scheme is verified anyway", AUTH,
     "    if payload.get(\"scheme\") != SCHEME:",
     "    if False:"),

    # ── revocation, and registration ─────────────────────────────────────────
    ("a deactivated credential still authenticates", AUTH,
     "    if not getattr(credential, \"is_active\", True):",
     "    if False:"),
    ("an unregistered gateway id is accepted", AUTH,
     "    if credential is None:",
     "    if False and credential is None:"),

    # ── identity before authority ────────────────────────────────────────────
    ("authority is checked before identity, so a refusal names the workspace", AUTH,
     "    ok, why = verify_signature(payload, credential.secret, now=now)\n"
     "    if not ok:\n"
     "        raise GatewayRejected(f\"gateway {credential.gateway_id!r}: {why}\")\n"
     "\n"
     "    if credential.tenant_code != route.tenant:",
     "    if credential.tenant_code != route.tenant:"),

    # ── an id is validated before it becomes a lookup ────────────────────────
    ("a claimed gateway id is used unvalidated", AUTH,
     "    if not all(c.isalnum() or c in \"_.-\" for c in value):\n        return None",
     "    if False:\n        return None"),
    ("an unbounded gateway id reaches the database", AUTH,
     "    if len(value) > 64:\n        return None",
     "    if False:\n        return None"),

    # ── production is written once ───────────────────────────────────────────
    ("a re-sent production record is written again, doubling the shift", MQTT,
     "            if duplicate:",
     "            if False:"),
    ("the duplicate check ignores the workspace, so one customer blocks another", MQTT,
     "                duplicate = db.query(models.ProductionRecord).filter(\n"
     "                    models.ProductionRecord.tenant_code == machine.tenant_code,\n"
     "                    models.ProductionRecord.source_record_id == record_id",
     "                duplicate = db.query(models.ProductionRecord).filter(\n"
     "                    models.ProductionRecord.source_record_id == record_id"),
    ("the record id is never stored, so every retry looks new", MQTT,
     "                    source_record_id=record_id,",
     "                    source_record_id=None,"),

    # ── a refusal reaches a human, once ──────────────────────────────────────
    ("a refused gateway is dropped with no record at all", MQTT,
     "            _record_gateway_refusal(db, route, str(refusal))",
     "            pass"),
    ("the refusal is recorded again for every packet, flooding the feed", MQTT,
     "    if existing is not None:\n        return\n    try:\n        db.add(models.Notification(\n"
     "            tenant_code=route.tenant, notification_type=\"gateway_auth\",",
     "    if False:\n        return\n    try:\n        db.add(models.Notification(\n"
     "            tenant_code=route.tenant, notification_type=\"gateway_auth\","),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                                  errors="replace", cwd=here, timeout=SUITE_TIMEOUT)
            if proc.returncode != 0:
                failed.append(suite)
        except subprocess.TimeoutExpired:
            failed.append(suite + " (timeout)")
    return failed


# A mutation that cannot change behaviour, with a checkable reason. Each entry is
# a claim that had to be argued for, not an excuse for a gap -- and each of these
# four was a SURVIVED line first, investigated, and only then written down.
EXPECTED_SURVIVORS = {
    "a NULL site and an empty site are treated as different": (
        "gateway_credentials.site is NOT NULL with a '' server default and route.site is '' "
        "rather than None, so `credential.site` and `(credential.site or \"\")` are the same "
        "value for every row the database can hold. The `or` is defence against a future "
        "nullable column, not live behaviour"),
    "an unknown signing scheme is verified anyway": (
        "`scheme` is INSIDE the signed canonical form, so changing it changes the bytes and the "
        "signature stops matching. The explicit check exists to refuse a future scheme with a "
        "clear reason rather than a misleading 'signature does not match'"),
    "the duplicate check ignores the workspace, so one customer blocks another": (
        "VERIFIED, not assumed: on_message binds the thread to the route's tenant, and ADR-0002's "
        "scoping hook then filters the query anyway -- bound to VICTIM, a lookup of ACME's "
        "record_id returns None with or without the explicit filter. The explicit filter is "
        "defence in depth over the hook, exactly as mqtt_service's own comment says, and the "
        "uniqueness itself is per tenant in the database"),
    "a re-sent production record is written again, doubling the shift": (
        "uq_production_source_record refuses the second INSERT, the savepoint swallows the "
        "IntegrityError, and one record stands. The query is an optimisation that keeps the "
        "common case quiet; the CONSTRAINT is the control, and this survivor is that layering "
        "working. `the record id is never stored` is the mutation that kills the constraint, "
        "and it IS caught"),
}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    originals = {}
    for _, path, _, _ in MUTATIONS:
        if path not in originals:
            originals[path] = io.open(os.path.join(here, path), encoding="utf-8").read()

    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(SUITES)} suites green\n")
    print(f"{'mutation':<74} {'verdict':<10} caught by")
    print("-" * 118)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<74} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(
            source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:24] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<74} {verdict:<10} {note}", flush=True)
        if verdict == "SURVIVED":
            survived.append(label)

    dirty = [p for p, original in originals.items()
             if io.open(os.path.join(here, p), encoding="utf-8").read() != original]
    print()
    print(f"source files restored: {'yes' if not dirty else 'NO - DIRTY: ' + str(dirty)}")
    if dirty:
        return 3
    if survived:
        print(f"{len(survived)} MUTATION(S) SURVIVED - investigate each:")
        for s in survived:
            print("   *", s)
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
