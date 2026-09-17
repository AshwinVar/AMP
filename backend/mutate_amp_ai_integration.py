"""Mutation harness for the AMP-native AI INTEGRATION guards (ADR-0020, capability E).

Each mutation is a small, plausible edit that quietly breaks one guard the
integration relies on: the consent gate's explicit tenant filter and its refusal,
the audit row committed in the same transaction, founder preview refused, the
Admin-only consent write, the database consent gate at the anomaly call site, a
baseline never reused across tenants, the pinned-hash card, the rule still shown
when the model is unavailable, the prefix-keyed throttle, the copilot's
adoption and kill-switch checks, the proposer's allowlist and failure handling,
no network on the native path, and the migration's unique constraint. For each
one the harness applies the edit, runs the suite that is supposed to notice, and
restores the file byte for byte. A mutation that leaves its suite green
SURVIVED: that guard is untested.

RUN IT ALONE, ideally on a copy of backend/. Like every mutate_* harness it EDITS
THE WORKING TREE and restores it afterwards; anything reading those files
meanwhile sees broken code.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_amp_ai_integration.py
Exit 0 only if every mutation applied exactly once and was caught.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CONSENT = "test_amp_ai_integration_consent.py"
ROUTES = "test_amp_ai_integration_routes.py"
COPILOT = "test_amp_ai_integration_copilot.py"
ISOLATION = "test_amp_ai_integration_isolation.py"
STRUCTURAL = "test_amp_ai_integration_structural.py"
MIGRATION = "test_amp_ai_integration_migration.py"

GATE = "amp_ai/consent.py"
ROUTE = "native_ai_routes.py"
REG = "amp_ai/registry.py"
HTTP = "http_security.py"
COP = "ai_copilot.py"
ASSIST = "ai/assistant.py"
RMR = "read_model_routes.py"
SVC = "amp_ai/telemetry_anomaly/service.py"
MIG = "alembic/versions/0009_native_ai_consent.py"

# (label, file, old, new, suite that must go red)
MUTATIONS = [
    # --- consent: the gate ---------------------------------------------------------------
    ("gate: the consent read loses its explicit tenant filter", GATE,
     "               .filter(models.AiLearningConsent.tenant_code == tenant,\n"
     "                       models.AiLearningConsent.capability == capability).first())\n"
     "        title = CAPABILITY_INFO",
     "               .filter(models.AiLearningConsent.capability == capability).first())\n"
     "        title = CAPABILITY_INFO", CONSENT),
    ("gate: a revoked or never-granted row counts as granted", GATE,
     "        if row.granted is not True:", "        if False:", CONSENT),
    ("gate: no row at all counts as granted", GATE,
     "        if row is None:\n            return _refused(capability,",
     "        if False:\n            return _refused(capability,", CONSENT),
    ("gate: an unknown capability is looked up instead of refused", GATE,
     "        if capability not in LEARNING_CAPABILITIES:\n            return _refused(named,",
     "        if False:\n            return _refused(named,", CONSENT),

    # --- consent: the write ----------------------------------------------------------------
    ("write: the consent change is committed without its audit row", GATE,
     "        db.add(platform_routes.build_audit_row(actor,",
     "        (platform_routes.build_audit_row(actor,", CONSENT),
    ("write: the consent row is committed BEFORE the audit row (two commits)", GATE,
     "        row.updated_at = now\n", "        row.updated_at = now\n        db.commit()\n", CONSENT),
    ("write: a failed write is not rolled back", GATE,
     "    except Exception:\n        db.rollback()\n        raise", "    except Exception:\n        raise", CONSENT),
    ("write: a non-boolean decision is stored", GATE,
     "    if type(granted) is not bool:", "    if False:", CONSENT),

    # --- consent: who may write ---------------------------------------------------------------
    ("route: a founder preview may consent on the customer's behalf", ROUTE,
     "    if tenant != _claim_tenant(current_user):", "    if False:", CONSENT),
    ("route: Supervisors and Operators may toggle consent", ROUTE,
     'CONSENT_EDITOR_ROLES = ["Admin"]', 'CONSENT_EDITOR_ROLES = ["Admin", "Supervisor", "Operator"]', CONSENT),
    ("route: the writer's refusal of an unknown capability becomes a 500", ROUTE,
     "    except ValueError as exc:\n        raise HTTPException(status_code=400,",
     "    except KeyError as exc:\n        raise HTTPException(status_code=400,", CONSENT),

    # --- the anomaly call site ------------------------------------------------------------------
    ("route: Operators may run the models", ROUTE,
     'ANALYST_ROLES = ["Admin", "Supervisor"]', 'ANALYST_ROLES = ["Admin", "Supervisor", "Operator"]', ROUTES),
    ("route: a consent refusal is answered 200", ROUTE,
     "        return JSONResponse(status_code=403, content={", "        return JSONResponse(status_code=200, content={",
     ROUTES),
    ("route: the gate reaches score_machine through a variable (not DbConsentGate(...))", ROUTE,
     "        return service.score_machine(db, tenant, machine_id, gate=consent.DbConsentGate())",
     "        gate = consent.DbConsentGate()\n        return service.score_machine(db, tenant, machine_id, gate=gate)",
     STRUCTURAL),
    ("service: the first baseline data loaded is reused for every later request", SVC,
     "    data = DT.load(db, tenant, machine_id, now=now)",
     '    data = globals().setdefault("_LEAK", {}).setdefault("data", DT.load(db, tenant, machine_id, now=now))',
     ISOLATION),

    # --- failure risk, cards, throttle ----------------------------------------------------------
    ("route: model unavailable shows no rule either", ROUTE,
     '"machines": _rule_only(histories, as_of),', '"machines": [],', ROUTES),
    ("registry: an unregistered name is looked up anyway", REG,
     "    if not isinstance(name, str) or name not in MODELS:", "    if not isinstance(name, str) or not name:",
     ROUTES),
    ("registry: the card carries the model's parameters", REG,
     '_CARD_KEYS = ("model_type",', '_CARD_KEYS = ("parameters", "model_type",', ROUTES),
    ("registry: the failure-risk card reads the file without the pinned hash", REG,
     "    artifact, _model = predict.load_model()",
     '    import json\n    artifact = json.load(open(predict.ARTIFACT_PATH, encoding="utf-8"))', ROUTES),
    ("registry: the consent read in consent_view loses its tenant filter", GATE,
     "    rows = {r.capability: r for r in db.query(models.AiLearningConsent)\n"
     "            .filter(models.AiLearningConsent.tenant_code == tenant).all()}",
     "    rows = {r.capability: r for r in db.query(models.AiLearningConsent).all()}", STRUCTURAL),
    ("throttle: a bucket per full path (a fresh budget per machine id)", HTTP,
     'f"{_client_key(scope)}:{prefix}"', "f\"{_client_key(scope)}:{scope['path']}\"", ROUTES),
    ("throttle: the anomaly endpoint is not rate limited", HTTP,
     '    "/ai/native/anomaly": (_env_int("RATE_LIMIT_AI", 20), 60),\n', "", STRUCTURAL),

    # --- the native copilot -----------------------------------------------------------------------
    ("copilot: a verified but NOT adopted model switches on", COP,
     '        return st["available"] is True and st["adopted"] is True', '        return st["available"] is True',
     COPILOT),
    ("copilot: AMP_NATIVE_COPILOT=off is ignored", COP,
     "        if self.switched_off():\n            return False\n", "", COPILOT),
    ("copilot: /copilot/ask gets a proposer while an LLM is configured", COP,
     '    return NATIVE.route if _answer_engine() == "amp-native" else None',
     "    return NATIVE.route if NATIVE.is_configured() else None", COPILOT),
    ("copilot: /copilot/ask always passes the native proposer", RMR,
     "proposer=ai_copilot.native_proposer())", "proposer=ai_copilot.NATIVE.route)", COPILOT),
    ("copilot: the native path phones home", COP,
     "        return self._load().route(question)",
     '        import urllib.request\n        urllib.request.urlopen("http://127.0.0.1:9/")\n'
     "        return self._load().route(question)", COPILOT),
    ("proposer: a name outside the allowlist is resolved", ASSIST,
     "    fn = _pillar(decision.route)", '    fn = globals().get("_" + decision.route)', COPILOT),
    ("proposer: a non-RouteDecision is trusted", ASSIST,
     "    if not isinstance(decision, RouteDecision) or decision.route is None:", "    if decision is None:", COPILOT),
    ("proposer: a proposer that raises breaks the answer", ASSIST,
     "    except Exception:   # noqa: BLE001 - a failing model must degrade to the keyword router",
     "    except ValueError:   # noqa: BLE001 - a failing model must degrade to the keyword router", COPILOT),
    ("proposer: consulted before the machine-name lookup", ASSIST,
     '    labelled = {} if proposer is None else {"route_source": "keywords"}\n',
     '    labelled = {} if proposer is None else {"route_source": "keywords"}\n'
     "    if proposer is not None:\n"
     "        _fn, _d = _proposed_pillar(proposer, question)\n"
     "        if _fn is not None:\n"
     "            _t, _v = _fn(db, tenant)\n"
     '            return {"question": question, "answer": _t, "view": _v, "matched": _fn.__name__.lstrip("_"),\n'
     '                    "route_source": "model", "confidence": _d.confidence, "model_version": _d.model_version}\n',
     COPILOT),

    # --- the migration -----------------------------------------------------------------------------
    ("migration: no UNIQUE (tenant_code, capability)", MIG,
     '        sa.UniqueConstraint("tenant_code", "capability", name="uq_ai_learning_consent"),\n', "", MIGRATION),
    ("migration: no tenant_code index", MIG,
     '    op.create_index("ix_ai_learning_consents_tenant_code", TABLE, ["tenant_code"])\n', "", MIGRATION),
]


def run(suite):
    # PYTHONDONTWRITEBYTECODE is load-bearing (see mutate_amp_ai_core.py): a same-length mutation restored
    # within the same second can leave a .pyc of the MUTATED code that Python then trusts.
    env = dict(os.environ, DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite:///./ci.db"),
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, suite], cwd=HERE, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=900)
    return proc.returncode


def drop_bytecode(path):
    cache = os.path.join(os.path.dirname(path), "__pycache__")
    stem = os.path.splitext(os.path.basename(path))[0] + "."
    if os.path.isdir(cache):
        for name in os.listdir(cache):
            if name.startswith(stem) and name.endswith(".pyc"):
                os.remove(os.path.join(cache, name))


def main():
    suites = sorted({m[4] for m in MUTATIONS})
    for rel in sorted({m[1] for m in MUTATIONS}):
        drop_bytecode(os.path.join(HERE, *rel.split("/")))
    for suite in suites:
        if run(suite) != 0:
            print(f"ABORT: {suite} fails before any mutation")
            return 2
    print(f"baseline: {len(suites)} suites green\n")
    print(f"{'mutation':<84} verdict")
    print("-" * 96)
    problems = []
    for label, rel, old, new, suite in MUTATIONS:
        path = os.path.join(HERE, *rel.split("/"))
        with open(path, "rb") as fh:
            original = fh.read()
        text = original.decode("utf-8")
        crlf = "\r\n" in text
        needle = old.replace("\n", "\r\n") if crlf else old
        replacement = new.replace("\n", "\r\n") if crlf else new
        hits = text.count(needle)
        if hits != 1:
            print(f"{label:<84} NOT APPLIED ({hits} matches in {rel})")
            problems.append(label)
            continue
        try:
            with open(path, "wb") as fh:
                fh.write(text.replace(needle, replacement, 1).encode("utf-8"))
            drop_bytecode(path)
            code = run(suite)
        finally:
            with open(path, "wb") as fh:
                fh.write(original)
            drop_bytecode(path)
        with open(path, "rb") as fh:
            if fh.read() != original:
                print(f"FATAL: {rel} was not restored byte for byte")
                return 3
        verdict = "caught" if code != 0 else "SURVIVED"
        print(f"{label:<84} {verdict} ({suite})", flush=True)
        if code == 0:
            problems.append(label)
    print()
    for suite in suites:
        if run(suite) != 0:
            print(f"FATAL: {suite} fails after restoring every file")
            return 3
    if problems:
        print(f"{len(problems)} of {len(MUTATIONS)} mutations not caught or not applied:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught; every file restored; suites green again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
