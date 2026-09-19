"""python -m copilot_eval — the Copilot scorecard (ADR-0022, ADR-0023).

    python -m copilot_eval
        AMP's own engine and every scripted model behaviour. No network.

    python -m copilot_eval --provider local [--record FILE]
        A REAL model, through its provider as configured by the environment
        (AMP_LLM_BASE_URL / AMP_LLM_MODEL for the self-hosted one), scored against
        AMP's own engine on the same data, with the adoption gate's verdict.
        --record writes the record a reviewer commits to ai/adopted_models.json.

Runs on a private in-memory database, so it touches no real data; the only data
a real model sees is the three synthetic factories.
"""
import argparse
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import tenancy  # noqa: E402
from database import Base  # noqa: E402
from copilot_eval import adoption, fakes, fixtures, harness  # noqa: E402


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    fixtures.seed(Session)
    return Session


def show(report):
    print(report.summary())
    for f in report.failures()[:10]:
        print("   ", f)
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="copilot_eval")
    ap.add_argument("--provider", help="score a real model through this provider (local, anthropic, gemini)")
    ap.add_argument("--record", help="write the adoption record for --provider to this file")
    args = ap.parse_args(argv)
    Session = session()
    baseline = harness.run(Session)
    show(baseline)
    if not args.provider:
        for mode in fakes.MODES:
            show(harness.run(Session, llm=fakes.ScriptedLLM(mode)))
        return 0

    import ai_copilot
    from ai.llm import ProviderLLM
    provider = next((p for p in ai_copilot.PROVIDERS if p.name == args.provider), None)
    if provider is None or not provider.is_configured():
        print(f"provider {args.provider!r} is not configured; nothing was measured")
        return 2
    report = harness.run(Session, llm=ProviderLLM(provider), label=f"{provider.name} · {provider.model()}")
    show(report)
    rec = adoption.record(provider.name, provider.model(), report, baseline)
    print("ADOPTION GATE:", "PASSED" if rec["passed"] else "NOT PASSED")
    for r in rec["reasons"]:
        print("  -", r)
    if args.record:
        with open(args.record, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        print(f"record written to {args.record}")
    return 0 if rec["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
