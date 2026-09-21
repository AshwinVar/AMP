"""Mutation harness for the external-model consent gate (ADR-0037).

Each mutation is a small, plausible edit that quietly sends a company's data to
a hosted model without that company's consent, or misreports whether it would:
the gate removed, asked of the wrong provider, the wrong capability, a preview
treated as the company's own user, a refusal that still builds the model, a
decision remembered across requests, a route that bypasses the one chokepoint,
a status that reports consent without reading it, a page that hides the
decision, a writer or a gate that refuses the capability as unknown, and the
consent wording left blank. For each one the harness applies the edit, runs the
suites that are supposed to notice, and restores the file byte for byte. A
mutation that leaves every suite green SURVIVED: that guard is untested.

RUN IT ALONE. Like every mutate_* harness it EDITS THE WORKING TREE and restores
it afterwards; anything reading those files meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_external_model_consent.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import io
import os
import subprocess
import sys

SUITES = ["test_external_model_consent.py", "test_ai_copilot_fallback.py", "test_amp_ai_integration_consent.py"]
SUITE_TIMEOUT = 600   # seconds per suite: a mutation that hangs a suite is a survivor, not a wait

COP = "ai_copilot.py"
GATE = "amp_ai/consent.py"
CONTRACTS = "amp_ai/core/contracts.py"

# (label, file, old, new)
MUTATIONS = [
    # --- the chokepoint ---------------------------------------------------------
    ("copilot: a hosted provider is used without the company's consent", COP,
     "    if provider.external:\n        allowed, why = external_model_allowed(db, current_user, provider)\n"
     "        if not allowed:\n            return None, why\n    else:",
     "    if False:\n        allowed, why = external_model_allowed(db, current_user, provider)\n"
     "        if not allowed:\n            return None, why\n    else:"),
    ("copilot: consent is asked of the self-hosted model and not the hosted one", COP,
     "    if provider.external:\n        allowed, why = external_model_allowed(db, current_user, provider)",
     "    if not provider.external:\n        allowed, why = external_model_allowed(db, current_user, provider)"),
    ("copilot: a refusal still builds the model (the data leaves, the note stays)", COP,
     "        if not allowed:\n            return None, why\n    else:",
     "        if not allowed:\n            pass\n    else:"),
    ("copilot: the LEARNING consent is taken as consent to send data outside AMP", COP,
     "    decision = consent.DbConsentGate().check(db, tenant, CAPABILITY_EXTERNAL_MODEL, scope=provider.name)",
     '    decision = consent.DbConsentGate().check(db, tenant, "telemetry_baseline")'),
    # --- ADR-0038: a consent names the provider it was given for ---------------------
    ("copilot: the gate is asked without the provider about to be used", COP,
     "    decision = consent.DbConsentGate().check(db, tenant, CAPABILITY_EXTERNAL_MODEL, scope=provider.name)",
     "    decision = consent.DbConsentGate().check(db, tenant, CAPABILITY_EXTERNAL_MODEL)"),
    ("copilot: the gate is asked for a fixed provider, whatever is configured", COP,
     "    decision = consent.DbConsentGate().check(db, tenant, CAPABILITY_EXTERNAL_MODEL, scope=provider.name)",
     '    decision = consent.DbConsentGate().check(db, tenant, CAPABILITY_EXTERNAL_MODEL, scope="anthropic")'),
    ("gate: a consent given for one provider is honoured for another", GATE,
     "            if row.scope != wanted:",
     "            if False:"),
    ("write: the grant does not record what it was for", GATE,
     "            row.scope = scope if scoped else None",
     "            row.scope = None"),
    ("write: a scoped grant with no provider configured is stored (advance consent)", GATE,
     "    if granted and scoped and not scope:\n        raise ValueError(",
     "    if False:\n        raise ValueError("),
    ("page: the entry does not say whether the grant is active for what is configured", GATE,
     '            "active": granted and (not scoped or (now_for is not None and r.scope == now_for)),',
     '            "active": granted,'),
    ("contracts: the external-model consent names no provider", CONTRACTS,
     "SCOPED_CAPABILITIES = (CAPABILITY_EXTERNAL_MODEL,)",
     "SCOPED_CAPABILITIES = ()"),
    ("copilot: a founder preview uses the company's consent as its own", COP,
     "    if tenancy.is_preview(current_user):\n        return False,",
     "    if False:\n        return False,"),
    ("copilot: the decision is read once and remembered across requests", COP,
     "    decision = consent.DbConsentGate().check(db, tenant, CAPABILITY_EXTERNAL_MODEL, scope=provider.name)\n"
     "    if not decision.granted:",
     '    memo = external_model_allowed.__dict__.setdefault("memo", {})\n'
     "    decision = memo.get(tenant)\n"
     "    if decision is None:\n"
     "        decision = memo[tenant] = consent.DbConsentGate().check(db, tenant, CAPABILITY_EXTERNAL_MODEL, scope=provider.name)\n"
     "    if not decision.granted:"),
    ("copilot: the refusal loses its reason (answered from rules, silently)", COP,
     "        return False, (\"Answered from live factory data by AMP's own engine; the hosted AI model was not \"\n"
     "                       f\"asked: {decision.reason}\")",
     "        return False, None"),
    # --- the report's rules fallback names the wrong company -------------------------
    ("report: the rules fallback builds the report for the token's tenant, not the request's", COP,
     "    tenant = tenancy.request_tenant(current_user)\n    from ai import orchestrator",
     '    tenant = current_user.get("tenant", "DEFAULT")\n    from ai import orchestrator'),
    # --- the routes bypass the chokepoint -----------------------------------------
    ("copilot: /ai/ask words the answer with the provider regardless", COP,
     "    llm, not_used = _copilot_llm(db, current_user)\n    # ADR-0035: the caller's own prior turns",
     "    from ai.llm import ProviderLLM\n"
     "    llm, not_used = ProviderLLM(_resolve_provider(), ask=_ask_llm, on_error=_record_llm_error), None\n"
     "    # ADR-0035: the caller's own prior turns"),
    ("copilot: /ai/report words the report with the provider regardless", COP,
     "    llm, not_used = _copilot_llm(db, current_user)\n    try:",
     "    from ai.llm import ProviderLLM\n"
     "    llm, not_used = ProviderLLM(_resolve_provider(), ask=_ask_llm, on_error=_record_llm_error), None\n"
     "    try:"),
    # --- the status misreports ----------------------------------------------------
    ("status: the company is reported as consented without reading its decision", COP,
     "        allowed, why = external_model_allowed(db, current_user, provider)\n    finally:",
     "        allowed, why = True, None\n    finally:"),
    ("status: a self-hosted model is reported as a hosted one needing consent", COP,
     "    if provider is None or not provider.is_configured() or not provider.external:\n"
     '        return {"provider": None, "consent": None, "reason": None}',
     "    if provider is None or not provider.is_configured():\n"
     '        return {"provider": None, "consent": None, "reason": None}'),
    # --- the consent page and its gate ---------------------------------------------
    ("gate: the consent page lists only the learning capabilities", GATE,
     "    for capability in CONSENT_CAPABILITIES:",
     "    for capability in CONSENT_CAPABILITIES[:1]:"),
    ("gate: the external-model consent cannot be written (validated against learning only)", GATE,
     "    if capability not in CONSENT_CAPABILITIES:\n        raise ValueError(f\"unknown consent capability {capability!r}\")",
     "    if capability not in CONSENT_CAPABILITIES[:1]:\n        raise ValueError(f\"unknown consent capability {capability!r}\")"),
    ("gate: a granted external-model consent is refused as unknown at check time", GATE,
     "        if capability not in CONSENT_CAPABILITIES:\n            return _refused(named,",
     "        if capability not in CONSENT_CAPABILITIES[:1]:\n            return _refused(named,"),
    ("gate: what the hosted model receives is left unsaid", GATE,
     "        \"reads\": (\"When one of this company's own users asks the Copilot a question or opens the AI report, \"",
     "        \"reads\": (\"\" if True else \"When one of this company's own users asks the Copilot a question or opens the AI report, \""),
    ("contracts: the external-model capability is not a consent capability at all", CONTRACTS,
     "CONSENT_CAPABILITIES = LEARNING_CAPABILITIES + (CAPABILITY_EXTERNAL_MODEL,)",
     "CONSENT_CAPABILITIES = LEARNING_CAPABILITIES"),
]


def run_suites():
    failed = []
    here = os.path.dirname(os.path.abspath(__file__))
    for suite in SUITES:
        try:
            proc = subprocess.run([sys.executable, suite], capture_output=True, text=True, errors="replace",
                                  cwd=here, timeout=SUITE_TIMEOUT)
            if proc.returncode != 0:
                failed.append(suite)
        except subprocess.TimeoutExpired:
            failed.append(suite + " (timeout)")
    return failed


# A mutation here is one a DIFFERENT guard already covers. Each needs a reason
# that survives reading. (The writer's own refusal of a scoped grant with no
# provider is shadowed over HTTP by the route's 400, and is caught by the
# direct-call check in test_external_model_consent §10 -- so it is not listed.)
EXPECTED_SURVIVORS = {}


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
    print(f"{'mutation':<78} {'verdict':<10} caught by")
    print("-" * 120)

    survived = []
    for label, path, old, new in MUTATIONS:
        source = originals[path]
        if source.count(old) != 1:
            print(f"{label:<78} {'SKIP':<10} pattern hits {source.count(old)}x in {path}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source.replace(old, new, 1))
        try:
            failing = run_suites()
        finally:
            io.open(os.path.join(here, path), "w", encoding="utf-8", newline="\n").write(source)
        if failing:
            verdict, note = "caught", ", ".join(s.replace("test_", "").replace(".py", "")[:28] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:70] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<78} {verdict:<10} {note}", flush=True)
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
