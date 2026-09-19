"""python -m copilot_eval — print the Copilot scorecard (ADR-0022).

Runs on a private in-memory database, so it touches no real data. Scores AMP's
own engine and every scripted model behaviour. A real model is scored the same
way once its provider adapter exists; until then this prints no claim about one.
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import tenancy  # noqa: E402
from database import Base  # noqa: E402
from copilot_eval import fakes, fixtures, harness  # noqa: E402


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    Session = sessionmaker(bind=engine)
    fixtures.seed(Session)
    return Session


def main():
    Session = session()
    reports = [harness.run(Session)]
    reports += [harness.run(Session, llm=fakes.ScriptedLLM(m)) for m in fakes.MODES]
    for r in reports:
        print(r.summary())
        for f in r.failures()[:10]:
            print("   ", f)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
