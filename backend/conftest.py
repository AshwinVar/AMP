"""Makes the standalone backend suites collectable by pytest, without changing one of them.

WHY THIS FILE EXISTS
--------------------
Every backend suite is a self-contained script: module-level ``test_*`` functions
plus an ``if __name__ == "__main__":`` block that calls each one and prints a
verdict. CI runs them that way (``python test_X.py``) and will keep doing so —
that mode is the contract, and nothing here changes it.

What that mode cannot do is measure coverage across 160-odd separate processes
without extra plumbing, and it gives no way to run "every test touching X".
pytest can do both, and it already recognises module-level ``test_*`` functions,
so collection is almost free. Two things stand between "almost" and "works":

1. ``database.py`` does ``os.environ["DATABASE_URL"]`` at import time — a hard
   KeyError with no fallback, deliberately, so a missing URL cannot be silently
   papered over in production. Under the per-file runner each suite inherits the
   shell's environment (or ``backend/.env`` via ``load_dotenv``); under pytest
   the very first collected module blows up before a single test runs.

2. Under the per-file runner each suite gets a fresh interpreter, so global state
   dies with the process. pytest runs all ~1000 tests in ONE process, which turns
   two harmless module-level globals into cross-suite contamination. The tenant
   ContextVar is the dangerous one — see the fixture below.

Both shims are additive: they only affect the pytest path. Running
``python test_tenancy.py`` never imports this file.
"""
import os
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent


# SHIM 1 — satisfy database.py's import-time DATABASE_URL.
#
# Falsy, not merely unset: an exported-but-empty DATABASE_URL gets the fallback
# too. ``os.environ.setdefault`` alone would leave "" in place, and "" satisfies
# database.py's ``os.environ[...]`` lookup only to die three lines later inside
# create_engine with "Could not parse SQLAlchemy URL from string ''" — repeated
# once per test module, which is 150+ identical collection errors hiding the one
# sentence that matters. An empty value is not a considered choice; it is a shell
# variable that expanded to nothing (an unset CI secret, a stray `export`), so
# treat it as absent.
#
# Otherwise an explicitly set DATABASE_URL still wins, which is how CI supplies
# sqlite:///./ci.db and how a developer can point the suite at a local Postgres
# for parity checks.
#
# Note what this DOES shadow: ``backend/.env``. database.py calls load_dotenv()
# at import time, and load_dotenv does not override variables that are already
# set — so by getting in first we take precedence over the developer's .env.
# That is deliberate rather than incidental. main.py runs
# ``Base.metadata.create_all(bind=engine)`` and two ``tenancy.ensure_tenant_columns``
# ALTER passes at IMPORT time, not on startup, and a dozen suites import main. One
# process importing main once against a developer's real .env database is the
# status quo under the per-file runner; a pytest session doing it while every
# other module shares that connection is a much easier way to scribble on a
# database somebody cared about. A throwaway file keeps ``pytest`` inert by
# default, and the escape hatch is one environment variable.
#
# Absolute path on purpose: ``pytest backend/`` invoked from the repo root has
# its CWD at the root, and a relative sqlite URL would drop the file there.
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = f"sqlite:///{_BACKEND / '.pytest.db'}"


# SHIM 2 — stop a leaked tenant scoping every subsequent test in the session.
@pytest.fixture(autouse=True)
def _clear_tenant_context():
    """Reset tenancy's current-tenant ContextVar around every test.

    ``tenancy.install_scoping()`` registers process-wide SQLAlchemy listeners
    (do_orm_execute / before_flush) that filter reads and stamp writes by the
    ContextVar. Roughly a dozen suites call it, and once installed it stays
    installed for the whole pytest session — including for the great majority of
    suites that know nothing about tenancy.

    That is survivable only because both listeners (_apply_tenant_filter and
    _stamp_tenant_on_new) return early when ``current_tenant()`` is None, which
    the seeding and simulation paths already rely on. So the invariant that keeps the
    single-process run honest is "no test starts with a tenant bound". A suite
    that sets a tenant and then fails an assertion before its
    ``reset_current_tenant`` leaves it bound, and every later test in the process
    silently reads through a tenant filter it never asked for — which surfaces as
    a cascade of empty result sets in unrelated suites, minutes of confusion away
    from the test that actually broke.

    Imported inside the fixture, not at module scope: importing tenancy pulls in
    models -> database, and that must not happen until SHIM 1 above has run.
    """
    import tenancy

    tenancy.set_current_tenant(None)
    yield
    tenancy.set_current_tenant(None)
