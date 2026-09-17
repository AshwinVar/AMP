"""The AMP-native copilot: an intent model that may only PROPOSE a pillar name (ADR-0020).

WHAT IS PINNED
--------------
  1. NOT A PROVIDER  PROVIDERS stays ("anthropic", "gemini"); amp-native sits beside
                     it. With no LLM key, _provider() is None and _ai_enabled() is
                     False exactly as before.
  2. SWITCHED ON ONLY WHEN EARNED  NATIVE.is_configured() needs the artifact to
                     verify against the PINNED hash, adoption.adopted to be true,
                     and AMP_NATIVE_COPILOT not "off". The committed model was NOT
                     adopted, so today the engine is "rules" and every /copilot/ask
                     response is byte-for-byte what it was before this change.
  3. THE CONTRACT OF A PROPOSAL  answer(..., proposer=) runs the machine-name and
                     find checks first (the machine lookup runs ONCE), then asks the
                     proposer. Only a RouteDecision naming an allowlisted pillar is
                     used; None, __none__, a low-confidence decision, a non-pillar
                     name, a non-RouteDecision or a proposer that raises all fall
                     back to the keyword router. The tenant never comes from it.
  4. NO NETWORK      with every socket and urlopen stubbed to fail loudly, the
                     native path still answers, and nothing tried to connect.
  5. VIEW FOR FREE   with an LLM configured, an adopted native model may choose the
                     drill-in view, at zero extra queries.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_integration_copilot.py
"""
import http.client
import os
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import ai_copilot
import models
import read_model_routes
import tenancy
from ai import assistant
from amp_ai.copilot_intent import classifier
from amp_ai.core.contracts import RouteDecision
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
T = "NATIVE_COPILOT"
USER = {"tenant": T, "role": "Admin", "sub": "nc-admin"}
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


ENV_KEYS = ("AI_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AMP_NATIVE_COPILOT")


def clean_env():
    for k in ENV_KEYS:
        os.environ.pop(k, None)


def seed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    now = datetime.utcnow()
    db.add(models.TenantConfig(tenant_code=T))
    for i in range(3):
        m = models.Machine(tenant_code=T, name=f"CNC-{i:02d}", site="P1",
                           status="Breakdown" if i == 0 else "Running", utilization=60, downtime="5 min")
        db.add(m)
        db.flush()
        db.add(models.DowntimeLog(tenant_code=T, machine_id=m.id, reason="Feeder jam", duration="12 min",
                                  created_at=now - timedelta(days=1)))
        db.add(models.ProductionRecord(tenant_code=T, machine_id=m.id, planned_minutes=480, runtime_minutes=400,
                                       ideal_cycle_time_seconds=30, total_count=600, good_count=570,
                                       rejected_count=30, created_at=now - timedelta(days=1)))
    db.add(models.Machine(tenant_code="OTHER", name="SECRET-PRESS", site="P1", status="Breakdown",
                          utilization=0, downtime="0 min"))
    db.commit()
    tenancy.reset_current_tenant(tok)
    db.close()
    return engine, Session


class FakeClassifier:
    """Stands in for a loaded, ADOPTED classifier with a fixed proposal."""

    def __init__(self, route="downtime", confidence=0.95, adopted=True):
        self._route, self._confidence, self.adopted = route, confidence, adopted
        self.model_version = "copilot_intent@test"
        self.calls = []

    def route(self, question):
        self.calls.append(question)
        return RouteDecision(self._route, self._confidence, [], self.model_version)


class Patched:
    """Point NATIVE at a given classifier loader for the duration of a block."""

    def __init__(self, loader):
        self.loader = loader

    def __enter__(self):
        self.original = ai_copilot.NATIVE._load
        ai_copilot.NATIVE._load = self.loader
        return self

    def __exit__(self, *exc):
        ai_copilot.NATIVE._load = self.original


def adopted_real_classifier():
    """The REAL committed weights, verified against the pin, with only the adoption flag set for the test."""
    real = classifier.load()
    artifact = dict(real.artifact)
    artifact["adoption"] = {"adopted": True, "reasons": []}
    return classifier.IntentClassifier(artifact)


def count_statements(engine):
    counter = {"n": 0}

    def before(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", before)
    return counter, lambda: event.remove(engine, "before_cursor_execute", before)


def ask(Session, question, **kw):
    db = Session()
    tok = tenancy.set_current_tenant(T)
    try:
        return assistant.answer(db, T, question, **kw)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


# ----------------------------------------------------------------------------
def section_not_a_provider():
    print("=" * 74)
    print("1. AMP-NATIVE IS NOT AN LLM PROVIDER, AND IS OFF UNLESS ADOPTED")
    print("=" * 74)
    clean_env()
    check("PROVIDERS is still exactly anthropic, gemini", [p.name for p in ai_copilot.PROVIDERS]
          == ["anthropic", "gemini"], str([p.name for p in ai_copilot.PROVIDERS]))
    check("with no keys: no provider, AI disabled", ai_copilot._provider() is None
          and ai_copilot._ai_enabled() is False)
    check("NATIVE is an AIProvider named amp-native", isinstance(ai_copilot.NATIVE, ai_copilot.AIProvider)
          and ai_copilot.NATIVE.name == "amp-native")
    raised = None
    try:
        ai_copilot.NATIVE.ask("system", "user")
    except NotImplementedError as e:
        raised = e
    check("NATIVE.ask() refuses: it proposes routes, it does not write text", raised is not None)

    real = classifier.load()
    check("CONTROL: the committed artifact loads against the pinned hash", real.adopted is False,
          str(real.adopted))
    check("the committed model is NOT adopted -> NATIVE.is_configured() is False",
          ai_copilot.NATIVE.is_configured() is False)
    check("...so the answer engine is 'rules'", ai_copilot._answer_engine() == "rules", ai_copilot._answer_engine())
    check("...and there is no native proposer", ai_copilot.native_proposer() is None)

    with Patched(lambda: FakeClassifier(adopted=True)):
        check("an adopted model with no LLM key -> engine 'amp-native'",
              ai_copilot.NATIVE.is_configured() and ai_copilot._answer_engine() == "amp-native",
              ai_copilot._answer_engine())
        check("...and a proposer is offered", callable(ai_copilot.native_proposer()))
        os.environ["AMP_NATIVE_COPILOT"] = "off"
        check("AMP_NATIVE_COPILOT=off switches it off", ai_copilot.NATIVE.is_configured() is False
              and ai_copilot._answer_engine() == "rules" and ai_copilot.native_proposer() is None)
        os.environ["AMP_NATIVE_COPILOT"] = " OFF "
        check("...case- and space-insensitively", ai_copilot.NATIVE.is_configured() is False)
        os.environ.pop("AMP_NATIVE_COPILOT")
        os.environ["ANTHROPIC_API_KEY"] = "k"
        check("an LLM key wins: engine 'llm'", ai_copilot._answer_engine() == "llm")
        check("...and /copilot/ask gets no native proposer while an LLM is configured",
              ai_copilot.native_proposer() is None)
        os.environ.pop("ANTHROPIC_API_KEY")
        os.environ["AI_PROVIDER"] = "gemini"
        check("an AI_PROVIDER naming a provider with no key is not an LLM -> amp-native",
              ai_copilot._answer_engine() == "amp-native", ai_copilot._answer_engine())
        clean_env()
    with Patched(lambda: FakeClassifier(adopted=False)):
        check("a verified but NOT adopted model stays off", ai_copilot.NATIVE.is_configured() is False)

    def raising():
        from amp_ai.core.artifact import ArtifactIntegrityError
        raise ArtifactIntegrityError("contents do not match the embedded sha256 (edited or corrupted)")
    with Patched(raising):
        check("an artifact that fails verification -> off, not an exception",
              ai_copilot.NATIVE.is_configured() is False and ai_copilot._answer_engine() == "rules")
        st = ai_copilot.NATIVE.status()
        check("...and status() reports it unavailable with the reason",
              st.get("available") is False and "sha256" in str(st.get("reason")), str(st))

    status = ai_copilot.ai_status(current_user={"tenant": "ACME", "role": "Admin", "sub": "x"})
    check("/ai/status keeps enabled/provider/model", status.get("enabled") is False
          and status.get("provider") is None and status.get("model") is None, str(status))
    check("...and adds engine + native {available, adopted, version}", status.get("engine") == "rules"
          and status.get("native", {}).get("available") is True and status.get("native", {}).get("adopted") is False
          and status.get("native", {}).get("version") == "copilot_intent@v1", str(status))


def section_proposer_contract(engine, Session):
    print()
    print("=" * 74)
    print("2. WHAT A PROPOSAL CAN AND CANNOT DO")
    print("=" * 74)
    q = "how is quality looking?"
    base = ask(Session, q)
    check("CONTROL: the keyword router says quality", base["matched"] == "quality", base["matched"])
    check("CONTROL: without a proposer the response has no route_source", "route_source" not in base, str(base))

    fake = FakeClassifier(route="downtime", confidence=0.91)
    out = ask(Session, q, proposer=fake.route)
    check("an allowlisted proposal is honoured", out["matched"] == "downtime", out["matched"])
    check("...labelled route_source=model with its confidence and model version",
          out.get("route_source") == "model" and out.get("confidence") == 0.91
          and out.get("model_version") == "copilot_intent@test", str(out))

    def declined(route, confidence=0.9):
        return lambda question: RouteDecision(route, confidence, [], "copilot_intent@test")

    cases = [
        ("route None (no opinion / __none__ / below tau)", declined(None, 0.2)),
        ("a real function that is not a pillar", declined("find")),
        ("a private name", declined("_machines")),
        ("a name that does not exist", declined("drop_all_tables")),
        ("a dict instead of a RouteDecision", lambda question: {"route": "downtime", "confidence": 1.0}),
        ("a bare string instead of a RouteDecision", lambda question: "downtime"),
        ("None instead of a RouteDecision", lambda question: None),
    ]

    def boom(question):
        raise RuntimeError("model exploded")
    cases.append(("a proposer that raises", boom))
    for label, proposer in cases:
        try:
            out = ask(Session, q, proposer=proposer)
            check(f"{label} -> keyword route, labelled keywords",
                  out["matched"] == "quality" and out.get("route_source") == "keywords"
                  and out["answer"] == base["answer"], f"{out.get('matched')} {out.get('route_source')}")
        except Exception as exc:  # noqa: BLE001
            check(f"{label} -> keyword route, labelled keywords", False, f"raised {exc!r}")

    spy = FakeClassifier(route="downtime")
    out = ask(Session, "what is the status of CNC-01?", proposer=spy.route)
    check("a question naming a machine still answers about that machine",
          out["matched"] == "machine_detail", out["matched"])
    check("...and the proposer was never asked", spy.calls == [], str(spy.calls))
    # No machine name in it: a question naming a machine is answered about that
    # machine before the find prefix is even looked at (that is today's order).
    out = ask(Session, "find feeder jam", proposer=spy.route)
    check("a find question still runs the search, proposer never asked",
          out["matched"] == "find" and spy.calls == [], f"{out['matched']} {spy.calls}")
    out = ask(Session, "is SECRET-PRESS down?", proposer=FakeClassifier(route="machines").route)
    check("the tenant comes from AMP: another tenant's machine name is not found and not shown",
          out["matched"] == "machines" and "SECRET-PRESS" not in out["answer"], str(out)[:200])

    # The machine lookup runs ONCE: the proposer path costs exactly the chosen_route
    # path plus the single machine-name query.
    calls = {"n": 0}
    original = assistant._machine_named

    def counting(db, question):
        calls["n"] += 1
        return original(db, question)

    assistant._machine_named = counting
    counter, stop = count_statements(engine)
    try:
        ask(Session, q, proposer=FakeClassifier(route="downtime").route)
        with_proposer = counter["n"]
        machine_lookups = calls["n"]
        counter["n"] = 0
        ask(Session, q, chosen_route="downtime")
        chosen = counter["n"]
    finally:
        stop()
        assistant._machine_named = original
    check("_machine_named ran exactly once on the proposer path", machine_lookups == 1, str(machine_lookups))
    check("the proposer path costs the chosen-route path plus that one query",
          with_proposer == chosen + 1, f"proposer={with_proposer} chosen={chosen}")


def section_endpoints(engine, Session):
    print()
    print("=" * 74)
    print("3. /copilot/ask AND /ai/ask")
    print("=" * 74)
    clean_env()
    questions = ["Why is my OEE low?", "Which machines are in breakdown?", "What should I reorder first?",
                 "Are any orders late?", "How much are losses costing us?", "How are we doing vs last week?",
                 "Summarise today's production.", "what's the downtime situation", "zzz qqq"]

    def endpoint(question):
        # Bind the tenant the way TenantScopeMiddleware does for a real request.
        db = Session()
        tok = tenancy.set_current_tenant(T)
        try:
            return read_model_routes.copilot_ask({"question": question}, db=db, current_user=USER)
        finally:
            tenancy.reset_current_tenant(tok)
            db.close()

    same = all(endpoint(qq) == ask(Session, qq) for qq in questions)
    check("committed (not adopted) model: /copilot/ask == the keyword answer, for every question", same)
    os.environ["AMP_NATIVE_COPILOT"] = "off"
    with Patched(lambda: FakeClassifier(route="downtime", adopted=True)):
        check("adopted but AMP_NATIVE_COPILOT=off: identical to the keyword answer",
              all(endpoint(qq) == ask(Session, qq) for qq in questions))
    clean_env()

    # --- no network, real weights ------------------------------------------------
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise AssertionError("network access attempted")

    saved = (urllib.request.urlopen, socket.socket.connect, socket.create_connection,
             http.client.HTTPConnection.connect)
    urllib.request.urlopen = refuse
    socket.socket.connect = refuse
    socket.create_connection = refuse
    http.client.HTTPConnection.connect = refuse
    real = adopted_real_classifier()
    sources = []
    try:
        with Patched(lambda: real):
            check("the real weights, marked adopted, switch the engine to amp-native",
                  ai_copilot._answer_engine() == "amp-native")
            for qq in questions:
                out = endpoint(qq)
                sources.append(out.get("route_source"))
                if not (isinstance(out.get("answer"), str) and out["answer"]):
                    check(f"'{qq}' answered", False, str(out))
    finally:
        (urllib.request.urlopen, socket.socket.connect, socket.create_connection,
         http.client.HTTPConnection.connect) = saved
    check("every question was answered with the network unplugged", len(sources) == len(questions))
    check("nothing tried to open a connection", attempts == [], str(attempts))
    check("the model actually routed some of them (the native path ran)", "model" in sources, str(sources))
    check("...and the rest fell back to keywords, labelled so",
          set(sources) <= {"model", "keywords"}, str(sources))

    # --- /ai/ask with an LLM configured ---------------------------------------------
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    original = ai_copilot._ask_claude
    ai_copilot._ask_claude = lambda system, user: "Some prose."
    q = "zzz qqq"
    try:
        counter, stop = count_statements(engine)
        db = Session()
        try:
            plain = ai_copilot.ai_ask({"question": q}, db=db, current_user=USER)
            plain_n = counter["n"]
            counter["n"] = 0
            with Patched(lambda: FakeClassifier(route="maintenance", adopted=True)):
                native = ai_copilot.ai_ask({"question": q}, db=db, current_user=USER)
            native_n = counter["n"]
            counter["n"] = 0
            with Patched(lambda: FakeClassifier(route="maintenance", adopted=False)):
                unadopted = ai_copilot.ai_ask({"question": q}, db=db, current_user=USER)
        finally:
            stop()
            db.close()
        check("CONTROL: without the model the view is route_view's", plain.get("view")
              == assistant.route_view(q) == "overview", str(plain.get("view")))
        check("an adopted model's proposal chooses the drill-in view", native.get("view") == "cmms",
              str(native.get("view")))
        check("...at zero extra queries", native_n == plain_n, f"{native_n} vs {plain_n}")
        check("...the prose is still the LLM's and labelled llm", native.get("answer") == "Some prose."
              and native.get("source") == "llm")
        check("a NOT adopted model changes nothing", unadopted.get("view") == plain.get("view"))

        def boom(system, user):
            raise RuntimeError("Anthropic API 529: overloaded")
        ai_copilot._ask_claude = boom
        db = Session()
        try:
            with Patched(lambda: FakeClassifier(route="downtime", adopted=True)):
                fb = ai_copilot.ai_ask({"question": "how is quality looking?"}, db=db, current_user=USER)
        finally:
            db.close()
        check("LLM failure with an adopted model: the rules fallback takes the proposal",
              fb.get("source") == "rules" and fb.get("view") == "downtime", str(fb)[:200])
    finally:
        ai_copilot._ask_claude = original
        clean_env()


def section_existing_suites():
    print()
    print("=" * 74)
    print("4. THE SUITES THAT PIN TODAY'S COPILOT STILL PASS UNCHANGED")
    print("=" * 74)
    env = dict(os.environ, DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite:///./ci.db"),
               PYTHONIOENCODING="utf-8")
    for suite in ("test_ai_provider_registry.py", "test_ai_copilot_fallback.py", "test_copilot_drill_in.py",
                  "test_ai_route_allowlist.py"):
        p = subprocess.run([sys.executable, suite], cwd=HERE, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        check(f"{suite} exits 0", p.returncode == 0, (p.stdout + p.stderr)[-400:])


def main_():
    clean_env()
    engine, Session = seed()
    section_not_a_provider()
    section_proposer_contract(engine, Session)
    section_endpoints(engine, Session)
    section_existing_suites()
    clean_env()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_amp_ai_integration_copilot():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
