"""The self-hosted model path, end to end over real HTTP, with no model (ADR-0023).

A stub OpenAI-compatible server (the protocol Ollama, the llama.cpp server and vLLM
all speak) plays the model. Everything on AMP's side is the real code: the
provider's HTTP call, the adapter, the orchestrator, the grounding gate, /ai/ask
and the adoption record. What is pinned:

  1. CONFIGURATION. Both AMP_LLM_BASE_URL and AMP_LLM_MODEL are needed; only
     http(s) is accepted (urllib would also open file://); the self-hosted
     provider is registered LAST, so configuring it never takes over from a
     hosted provider already in use.
  2. THE WIRE. The request is the OpenAI chat-completions shape, temperature 0,
     not streamed, tools offered with tool_choice auto when planning; a bearer
     token only when one is configured; a timeout, an HTTP error, a non-JSON
     body or a missing message each raise an error that does NOT carry the
     response body (it can echo the prompt).
  3. THE ADAPTER. tool_calls with JSON-string arguments are read; a runtime that
     writes the call into the text instead is read too; a <think> block is
     stripped; a hosted provider without tool calling words but does not plan.
  4. ADOPTION. A configured model that has NOT passed the evaluation is never
     called: /ai/ask answers from AMP's own engine and says why. Adopted, it
     plans and words, and its wording is shown only past the grounding gate.
  5. WHAT THE MODEL SEES. No request body ever carries the tenant code, the
     user, or another factory's data.
  6. /ai/ask TAKES THE TENANT FROM THE REQUEST. A founder previewing FACTORY_B
     gets FACTORY_B's figures (it used to be the token's own tenant).
  7. THE EVALUATION RUNS THROUGH THIS PATH: the full harness over the stub
     model through real HTTP has zero disclosures and zero ungrounded answers
     shown, and `python -m copilot_eval --provider local --record` writes a
     record whose verdict matches the gate.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_copilot_local_provider.py
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import ai_copilot
import tenancy
from ai import llm_adoption, orchestrator
from ai.llm import ProviderLLM, _json_calls
from ai.tools import Principal
from copilot_eval import fixtures as F
from copilot_eval import harness
from database import Base

failures = []
ENV_KEYS = ("AI_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AMP_LLM_BASE_URL", "AMP_LLM_MODEL",
            "AMP_LLM_API_KEY", "AMP_LLM_TIMEOUT", "AMP_NATIVE_COPILOT")
MODEL = "stub-qwen:test"


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


# ── The stub model ──────────────────────────────────────────────────

KEYWORDS = [("downtime", "get_downtime"), ("oee", "get_oee"), ("machine", "get_machine_status"),
            ("stock", "get_inventory_status"), ("reorder", "get_inventory_status"),
            ("cost", "get_financial_losses"), ("plan", "get_production_vs_target"),
            ("target", "get_production_vs_target"), ("quality", "get_quality_summary")]


def keyword_model(body):
    """A crude stand-in model: plans by keyword, words by quoting facts."""
    msgs = body.get("messages") or []
    user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
    if body.get("tools"):
        name = next((tool for key, tool in KEYWORDS if key in user.lower()), "get_factory_summary")
        m = re.search(r"\b([A-Z]{2,}-\d{2})\b", user)
        args = {"machine": m.group(1)} if m else {}
        if m:
            name = "get_machine_history"
        return 200, {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}, 0
    facts = json.loads(user.split("FACTS (data): ", 1)[1]) if "FACTS (data): " in user else []
    shown = [f for f in facts if f.get("value") is not None][:3]
    text = "<think>internal reasoning</think>" + " ".join(
        f"{f['label']}: {f['value']}{'%' if f.get('unit') == '%' else ''}." for f in shown)
    return 200, {"choices": [{"message": {"role": "assistant", "content": text or "Nothing to report."}}]}, 0


class Stub(BaseHTTPRequestHandler):
    responder = staticmethod(keyword_model)
    seen = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        Stub.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                          "body": body, "raw": raw.decode("utf-8", "replace")})
        status, payload, delay = Stub.responder(body)
        if delay:
            time.sleep(delay)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args):
        pass


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    F.seed(Session)
    return Session


def clean_env():
    for k in ENV_KEYS:
        os.environ.pop(k, None)


def adopted_record(path, passed=True):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"models": [{"provider": "local", "model": MODEL, "evaluated_at": "2026-09-19T00:00:00Z",
                               "passed": passed, "reasons": [] if passed else ["test"]}]}, f)


def ask_endpoint(Session, user, question, bound):
    db = Session()
    tok = tenancy.set_current_tenant(bound)
    try:
        return ai_copilot.ai_ask({"question": question}, db=db, current_user=user)
    finally:
        tenancy.reset_current_tenant(tok)
        db.close()


def main():
    Session = session()
    server, base = start_stub()
    record_file = os.path.join(tempfile.mkdtemp(), "adopted.json")
    original_record = llm_adoption.RECORD_PATH
    local = ai_copilot.LOCAL
    try:
        print("=" * 74)
        print("1. CONFIGURATION")
        print("=" * 74)
        clean_env()
        check("nothing set: not configured", not local.is_configured())
        os.environ["AMP_LLM_BASE_URL"] = base
        check("a URL without a model: not configured", not local.is_configured())
        os.environ["AMP_LLM_MODEL"] = MODEL
        check("URL and model: configured", local.is_configured())
        for bad in ("file:///etc/passwd", "ftp://127.0.0.1/v1", "127.0.0.1:11434/v1"):
            os.environ["AMP_LLM_BASE_URL"] = bad
            check(f"{bad!r} is refused (http and https only)", not local.is_configured())
        os.environ["AMP_LLM_BASE_URL"] = base
        check("the self-hosted provider is registered last",
              ai_copilot.PROVIDERS[-1] is local and local.external is False)
        os.environ["GEMINI_API_KEY"] = "g"
        check("with a hosted key also set, auto-detect keeps the hosted provider",
              ai_copilot._provider() == "gemini")
        os.environ["AI_PROVIDER"] = "local"
        check("AI_PROVIDER=local selects it explicitly", ai_copilot._provider() == "local")
        del os.environ["GEMINI_API_KEY"]
        os.environ.pop("AI_PROVIDER")
        check("alone, auto-detect finds it", ai_copilot._provider() == "local" and ai_copilot._ai_enabled())

        print()
        print("=" * 74)
        print("2. THE WIRE")
        print("=" * 74)
        Stub.seen.clear()
        Stub.responder = staticmethod(keyword_model)
        out = local.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "downtime?"}],
                         tools=[{"type": "function", "function": {"name": "get_downtime", "description": "d",
                                                                  "parameters": {"type": "object", "properties": {}}}}])
        req = Stub.seen[-1]
        check("POST to {base}/chat/completions", req["path"] == "/v1/chat/completions", req["path"])
        body = req["body"]
        check("the model, temperature 0, not streamed, bounded tokens",
              body.get("model") == MODEL and body.get("temperature") == 0 and body.get("stream") is False
              and isinstance(body.get("max_tokens"), int), str({k: body.get(k) for k in ("model", "temperature", "stream")}))
        check("tools offered with tool_choice auto", body.get("tools") and body.get("tool_choice") == "auto")
        check("no bearer token unless one is configured", "authorization" not in req["headers"])
        check("the tool call comes back", out["tool_calls"] and out["tool_calls"][0]["function"]["name"] == "get_downtime")
        os.environ["AMP_LLM_API_KEY"] = "sekret-token"
        local.chat([{"role": "user", "content": "x"}])
        check("a configured key goes as a bearer token", Stub.seen[-1]["headers"].get("authorization")
              == "Bearer sekret-token")
        os.environ.pop("AMP_LLM_API_KEY")
        for label, responder, want in [
                ("an HTTP 500", lambda b: (500, b'{"error": "ECHO: the whole prompt"}', 0), "HTTP 500"),
                ("a body that is not JSON", lambda b: (200, b"<html>ECHO the prompt</html>", 0), "not JSON"),
                ("a body with no message", lambda b: (200, {"choices": []}, 0), "no message")]:
            Stub.responder = staticmethod(responder)
            try:
                local.chat([{"role": "user", "content": "x"}])
                check(f"{label} raises", False)
            except RuntimeError as e:
                check(f"{label} raises, naming the failure and not the body", want in str(e) and "ECHO" not in str(e),
                      str(e))
        Stub.responder = staticmethod(lambda b: (200, {"choices": [{"message": {"content": "late"}}]}, 3))
        os.environ["AMP_LLM_TIMEOUT"] = "1"
        started = time.perf_counter()
        try:
            local.chat([{"role": "user", "content": "x"}])
            check("a slow model times out", False)
        except RuntimeError as e:
            check("a slow model times out at AMP_LLM_TIMEOUT", time.perf_counter() - started < 2.8, str(e))
        os.environ.pop("AMP_LLM_TIMEOUT")
        Stub.responder = staticmethod(keyword_model)

        # WHY THE MODEL STOPPED is carried out of chat() (ADR-0034). A reasoning
        # model that thinks past the budget returns no call and no content; only
        # finish_reason says it was cut off rather than silent, and the first
        # real model evaluation blamed the model for three hours without it.
        # Pinned through the REAL provider over HTTP, not a stand-in: a mutation
        # that made chat() drop the field survived every suite that used one.
        for label, finish, want in [("a string finish reason is carried out", "length", "length"),
                                    ("a finish reason that is not a string becomes None", 123, None),
                                    ("a missing finish reason is None, not an error", None, None)]:
            choice = {"message": {"role": "assistant", "content": ""}}
            if finish is not None:
                choice["finish_reason"] = finish
            Stub.responder = staticmethod(lambda b, c=choice: (200, {"choices": [c]}, 0))
            out = local.chat([{"role": "user", "content": "x"}])
            check(label, out.get("finish_reason") == want, repr(out.get("finish_reason")))
        # TOKEN COUNTS ride along the same way (ADR-0034 §11): the orchestrator
        # logs them per request, and only the counts -- the prompt never leaves
        # the request. Anything that is not an integer count is dropped rather
        # than logged, because a runtime could put anything in that object.
        for label, usage, want in [
                ("integer token counts are kept", {"prompt_tokens": 2010, "completion_tokens": 416, "total_tokens": 2426},
                 {"prompt_tokens": 2010, "completion_tokens": 416, "total_tokens": 2426}),
                ("a count that is not an integer is dropped, not logged",
                 {"prompt_tokens": "2010", "completion_tokens": 416, "extra": "x"}, {"completion_tokens": 416}),
                ("no usage object means no counts", None, {})]:
            payload = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            if usage is not None:
                payload["usage"] = usage
            Stub.responder = staticmethod(lambda b, p=payload: (200, p, 0))
            out = local.chat([{"role": "user", "content": "x"}])
            check(label, out.get("usage") == want and local.last_usage == want,
                  f"out={out.get('usage')!r} last={local.last_usage!r}")
        Stub.responder = staticmethod(keyword_model)

        print()
        print("=" * 74)
        print("3. THE ADAPTER")
        print("=" * 74)
        llm = ProviderLLM(local)
        steps = llm.plan("How is CNC-01 doing?", [{"name": "get_machine_history", "description": "d",
                                                   "parameters": {"type": "object", "properties": {}}}])
        check("tool_calls are read, JSON-string arguments and all",
              steps == [{"name": "get_machine_history", "arguments": '{"machine": "CNC-01"}'}], str(steps))
        check("a call written into the text is read too",
              _json_calls('Sure: [{"name": "get_oee", "arguments": {}}]') == [{"name": "get_oee", "arguments": {}}])
        check("text with no call reads as no call", _json_calls("I would look at OEE.") == [])
        text = llm.phrase("q", [{"id": "F1", "label": "Plant OEE", "value": 71, "unit": "%"}], "draft")
        check("a <think> block is stripped from the wording", "think" not in text and "Plant OEE: 71%" in text, text)
        hosted = ProviderLLM(ai_copilot.PROVIDERS[0])
        check("a provider without tool calling words but does not plan", hosted.can_plan is False
              and hosted.plan("q", []) is None)

        print()
        print("=" * 74)
        print("4. ADOPTION: A MODEL THAT HAS NOT PASSED IS NEVER CALLED")
        print("=" * 74)
        llm_adoption.RECORD_PATH = os.path.join(tempfile.mkdtemp(), "none.json")
        Stub.seen.clear()
        admin_a = {"tenant": F.A, "role": "Admin", "sub": "a-admin"}
        r = ask_endpoint(Session, admin_a, "How much downtime did we have?", F.A)
        check("not adopted: answered from AMP's own engine", r["source"] == "rules" and r["engine"] == "rules")
        check("...saying why", "has not passed" in (r.get("note") or ""), r.get("note"))
        check("...and the model was not called at all", Stub.seen == [], f"{len(Stub.seen)} calls")
        check("...with the evidence still there", r["evidence"] and r["tools"][0]["tool"] == "get_downtime")
        adopted_record(record_file, passed=False)
        llm_adoption.RECORD_PATH = record_file
        r = ask_endpoint(Session, admin_a, "How much downtime did we have?", F.A)
        check("a record that did NOT pass adopts nothing", r["source"] == "rules" and Stub.seen == [])
        adopted_record(record_file, passed=True)
        r = ask_endpoint(Session, admin_a, "How much downtime did we have?", F.A)
        check("adopted: the model plans and words, labelled llm", r["source"] == "llm" and r["model"] == MODEL
              and r["plan"]["planner"] == "llm", f"{r['source']} {r['plan']}")
        check("...its wording passed the gate", r["grounding"]["passed"] is True)
        check("...the answer has no <think> block", "think" not in r["answer"], r["answer"])
        check("...and the drill-in view is AMP's", r["view"] == "downtime", r["view"])
        Stub.responder = staticmethod(lambda b: keyword_model(b) if b.get("tools") else (200, {"choices": [
            {"message": {"content": "OEE is 99% and WELD-07 is fine."}}]}, 0))
        r = ask_endpoint(Session, admin_a, "How much downtime did we have?", F.A)
        check("a hallucinating adopted model is not shown", r["source"] == "rules"
              and "99" not in r["answer"] and "WELD-07" not in json.dumps(r), r["answer"])
        Stub.responder = staticmethod(keyword_model)

        print()
        print("=" * 74)
        print("4b. NO HOSTED KEY ANYWHERE: AMP RUNS ON THE SELF-HOSTED MODEL ALONE (ADR-0034 §14)")
        print("=" * 74)
        # The founder's brief: "AMP must be capable of running without a Gemini
        # API key. Test that explicitly." So: no ANTHROPIC_API_KEY, no
        # GEMINI_API_KEY, no AI_PROVIDER -- only the self-hosted model, adopted.
        # Auto-detection must reach it (it is last in PROVIDERS on purpose), and
        # every Copilot surface must answer, including the daily report, which
        # until ADR-0034 was the one path that handed the model a raw factory
        # snapshot and returned its prose unchecked.
        for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AI_PROVIDER"):
            os.environ.pop(k, None)
        os.environ["AMP_LLM_BASE_URL"], os.environ["AMP_LLM_MODEL"] = base, MODEL
        adopted_record(record_file, passed=True)
        llm_adoption.RECORD_PATH = record_file
        Stub.responder = staticmethod(keyword_model)
        check("with no hosted key, the resolved provider is the self-hosted one",
              ai_copilot._resolve_provider() is ai_copilot.LOCAL, str(ai_copilot._resolve_provider()))
        check("...and the copilot counts as enabled", ai_copilot._ai_enabled() is True)
        r = ask_endpoint(Session, admin_a, "How much downtime did we have?", F.A)
        check("/ai/ask answers, worded by the self-hosted model", r["source"] == "llm" and r["model"] == MODEL,
              f"{r.get('source')} {r.get('model')}")
        db = Session()
        tok = tenancy.set_current_tenant(F.A)
        try:
            rep = ai_copilot.ai_report(db=db, current_user=admin_a)
        finally:
            tenancy.reset_current_tenant(tok)
            db.close()
        check("/ai/report answers too, through the orchestrator", isinstance(rep.get("report"), str) and rep["report"],
              str(rep)[:200])
        check("...and it is labelled by its engine, not left as raw model prose",
              rep.get("source") in ("llm", "rules") and "ANTHROPIC" not in json.dumps(rep), str(rep)[:200])
        # The gate stands on this path now. A model that invents a figure in the
        # briefing is refused and the report says it was composed from data.
        Stub.responder = staticmethod(lambda b: keyword_model(b) if b.get("tools") else (200, {"choices": [
            {"message": {"content": "Summary: OEE is 99% and WELD-07 stopped 40 times."}}]}, 0))
        db = Session()
        tok = tenancy.set_current_tenant(F.A)
        try:
            rep2 = ai_copilot.ai_report(db=db, current_user=admin_a)
        finally:
            tenancy.reset_current_tenant(tok)
            db.close()
        check("a report wording the gate refuses is replaced, and says so",
              rep2.get("source") == "rules" and "99" not in rep2["report"] and "WELD-07" not in rep2["report"]
              and "not used" in (rep2.get("note") or ""), str(rep2)[:240])
        Stub.responder = staticmethod(keyword_model)
        status = ai_copilot.ai_status(current_user={"tenant": "DEFAULT", "role": "Admin", "sub": "founder"})
        check("/ai/status names the self-hosted provider as the active one",
              status.get("provider") == "local" and status.get("enabled") is True, str(status)[:200])

        print()
        print("=" * 74)
        print("5. WHAT THE MODEL SEES")
        print("=" * 74)
        Stub.seen.clear()
        for q in ("How much downtime did we have?", "What is our OEE?", "How is CNC-01 doing?"):
            ask_endpoint(Session, admin_a, q, F.A)
        sent = " ".join(s["raw"] for s in Stub.seen)
        check("the model was asked (so the check below is not vacuous)", len(Stub.seen) >= 6, str(len(Stub.seen)))
        for token in (F.A, "a-admin", "Admin"):
            check(f"no request body carries {token!r}", token not in sent)
        others = [m for owner, ms in F.MARKERS.items() if owner != F.A for m in ms if m in sent]
        check("no request body carries another factory's data", not others, str(others))

        print()
        print("=" * 74)
        print("6. /ai/ask TAKES THE TENANT FROM THE REQUEST (the founder's preview)")
        print("=" * 74)
        founder = {"tenant": "DEFAULT", "role": "Admin", "sub": "founder"}
        r = ask_endpoint(Session, founder, "What are the top causes of downtime?", F.B)
        text = json.dumps({k: v for k, v in r.items() if k != "question"})
        check("previewing FACTORY_B answers with FACTORY_B's data", "Hydraulic seal leak" in text, r["answer"])
        check("...and none of FACTORY_A's", "Alpha fixture swap" not in text)

        print()
        print("=" * 74)
        print("7. THE EVALUATION RUNS THROUGH THIS PATH")
        print("=" * 74)
        report = harness.run(Session, llm=ProviderLLM(local), label="stub over HTTP")
        m = report.metrics()
        print(report.summary())
        check("through real HTTP: zero unauthorized disclosures", m["unauthorized_disclosures"] == 0,
              "; ".join(x for x in report.failures() if x.startswith("DISCLOSURE"))[:300])
        check("...zero ungrounded answers shown", m["ungrounded_shown"] == 0)
        check("...zero money fabrications", m["money_fabrications"] == 0)
        check("...and the model actually worded answers", m["llm_worded"] > 0, str(m["llm_worded"]))
        from copilot_eval.__main__ import main as eval_main
        out_file = os.path.join(tempfile.mkdtemp(), "record.json")
        code = eval_main(["--provider", "local", "--record", out_file])
        rec = json.load(open(out_file, encoding="utf-8"))
        from copilot_eval.adoption import gate
        check("the CLI writes a record whose verdict IS the gate's", rec["passed"] == gate(rec["metrics"], rec["baseline"])[0]
              and code == (0 if rec["passed"] else 1), f"passed={rec['passed']} code={code} {rec['reasons']}")
        check("...naming the provider and the exact model", rec["provider"] == "local" and rec["model"] == MODEL)
        os.environ["AMP_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
        started = time.perf_counter()
        r = ask_endpoint(Session, admin_a, "What is our OEE?", F.A)
        check("the model's server is down: AMP answers anyway, from its own engine",
              r["source"] == "rules" and r["answer"] and r["evidence"], r.get("note"))
        status = ai_copilot.ai_status(current_user=founder)
        check("...and the founder's /ai/status shows the failure", status.get("last_error") is not None)
        check("/ai/status reports the self-hosted model and its adoption",
              status["local"]["configured"] is True and status["local"]["model"] == MODEL
              and status["local"]["adopted"] is True, str(status.get("local")))
    finally:
        server.shutdown()
        llm_adoption.RECORD_PATH = original_record
        clean_env()

    if failures:
        print(f"\n{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
