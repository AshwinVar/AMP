"""Migration framework adoption (migrate.py + alembic/).

WHAT THIS PINS. AMP built its schema with create_all plus ~30 idempotent boot
DDL helpers for its whole life. Introducing Alembic on a database that already
holds customer data is the risky part, not writing migrations afterwards — get
the adoption wrong and the first deploy either tries to recreate 48 existing
tables or silently believes it has applied changes it has not.

The dangerous case is the LIVE one: an existing schema with no alembic_version
table. It must be STAMPED (recorded as already at baseline), never MIGRATED.

Each scenario runs in its OWN SUBPROCESS with its own DATABASE_URL. That is
not ceremony: database.py reads DATABASE_URL at import and builds the engine
and declarative Base once, so swapping databases inside a single process means
reloading modules and re-registering every table on a live MetaData — fragile,
and it would test the reload dance rather than the migration. A subprocess also
exercises migrate.py the way a deploy actually invokes it.

Run:  python backend/test_migrate.py     (exit 0 = pass)
"""
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _run(script, url):
    """Execute `script` in a subprocess pointed at `url`. Returns (rc, output)."""
    env = dict(os.environ, DATABASE_URL=url, PYTHONPATH=HERE)
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       env=env, cwd=HERE, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class _TempDb:
    """A throwaway file-backed SQLite database.

    File-backed, not in-memory: migrate.run() opens its own connections, and an
    in-memory database would be a different empty database on every connect.
    """

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        return f"sqlite:///{self.path.replace(os.sep, '/')}"

    def __exit__(self, *exc):
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass


def test_a_fresh_database_is_built_and_stamped():
    with _TempDb() as url:
        rc, out = _run(
            "import migrate; from database import engine;"
            "migrate.run(verbose=False);"
            "from sqlalchemy import inspect;"
            "t=set(inspect(engine).get_table_names());"
            "print('STAMPED', migrate._is_stamped(engine));"
            "print('AT_HEAD', migrate.current_revision(engine)==migrate.head_revision());"
            "print('SCHEMA', 'machines' in t and 'users' in t)", url)
        assert rc == 0, out
        assert "STAMPED True" in out, out
        assert "AT_HEAD True" in out, out
        assert "SCHEMA True" in out, out
        print("PASS a fresh database is created and stamped at head")


def test_an_existing_schema_is_stamped_not_rebuilt():
    """THE LIVE CASE — the one that would break production.

    Production has 48 tables and no alembic_version. Adoption must record the
    baseline WITHOUT running DDL. The CONTROL is the surviving row: a migration
    that dropped and recreated the table would satisfy a naive "tables still
    exist" assertion while destroying every customer's data.
    """
    with _TempDb() as url:
        rc, out = _run(
            "from database import Base, engine, SessionLocal;"
            "import models, migrate;"
            "Base.metadata.create_all(bind=engine);"
            "db=SessionLocal();"
            "db.add(models.Machine(id=1,name='PRESS-01',status='Running',utilization=80));"
            "db.commit(); db.close();"
            "print('PRE_STAMPED', migrate._is_stamped(engine));"
            "migrate.run(verbose=False);"
            "db=SessionLocal();"
            "m=db.query(models.Machine).filter(models.Machine.id==1).first();"
            "db.close();"
            "print('POST_STAMPED', migrate._is_stamped(engine));"
            "print('ROW_SURVIVED', m is not None and m.name=='PRESS-01')", url)
        assert rc == 0, out
        assert "PRE_STAMPED False" in out, out
        assert "POST_STAMPED True" in out, out
        assert "ROW_SURVIVED True" in out, "adoption rebuilt the table and lost data:\n" + out
        print("PASS an existing schema is stamped, its data untouched")


def test_running_twice_is_a_no_op():
    """Deploys re-run migrations. The second run must change nothing and must
    not fail — the same idempotency the boot DDL helpers already guarantee."""
    with _TempDb() as url:
        rc, out = _run(
            "import migrate;"
            "a=migrate.run(verbose=False);"
            "b=migrate.run(verbose=False);"
            "print('STABLE', a==b==migrate.head_revision())", url)
        assert rc == 0, out
        assert "STABLE True" in out, out
        print("PASS re-running migrations is idempotent")


def test_the_baseline_is_an_empty_anchor():
    """0001_baseline must contain no DDL. If it ever grows a create_table,
    adopting Alembic on production would try to build tables that already exist
    and the deploy would fail. This is the guard on that invariant."""
    src = io.open(os.path.join(HERE, "alembic", "versions", "0001_baseline.py"),
                  encoding="utf-8").read()
    body = src.split("def upgrade")[1].split("def downgrade")[0]
    for forbidden in ("op.create_table", "op.add_column", "op.drop_", "op.alter_", "op.execute"):
        assert forbidden not in body, f"baseline must stay empty, found {forbidden}"
    print("PASS the baseline revision contains no DDL")


def test_downgrading_past_the_baseline_refuses():
    """A silent no-op downgrade would let someone believe they had rolled back
    to a state that never existed."""
    rc, out = _run(
        "import importlib.util, os;"
        "s=importlib.util.spec_from_file_location('b', os.path.join('alembic','versions','0001_baseline.py'));"
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m);"
        "\ntry:\n m.downgrade()\n print('NO_RAISE')\nexcept RuntimeError as e:\n print('RAISED', 'nothing to downgrade' in str(e).lower())",
        "sqlite:///./ci.db")
    assert "RAISED True" in out, out
    print("PASS downgrading below the baseline refuses loudly")


def test_status_is_read_only():
    with _TempDb() as url:
        rc, out = _run(
            "import migrate; from database import engine;"
            "migrate.status();"
            "print('STILL_UNSTAMPED', not migrate._is_stamped(engine))", url)
        assert "STILL_UNSTAMPED True" in out, "--status must not stamp:\n" + out
        print("PASS --status is read-only")


def test_env_refuses_without_a_database_url():
    """alembic/env.py must never invent a connection string. A migration
    pointed at the wrong database is worse than one that refuses to run."""
    src = io.open(os.path.join(HERE, "alembic", "env.py"), encoding="utf-8").read()
    assert 'os.environ.get("DATABASE_URL")' in src
    assert "raise RuntimeError" in src, "must refuse rather than default"
    ini = io.open(os.path.join(HERE, "alembic.ini"), encoding="utf-8").read()
    assert "\nsqlalchemy.url =\n" in ini or "sqlalchemy.url = \n" in ini, \
        "alembic.ini must not carry a hardcoded URL"
    print("PASS env.py refuses to run without DATABASE_URL, and the ini holds no URL")


def test_boot_still_works_untouched():
    """Adopting Alembic must not change how the app boots today. create_all and
    the boot DDL helpers stay until a later change retires them deliberately —
    removing both mechanisms in one deploy would leave no fallback."""
    rc, out = _run("import main; print('ROUTES', len(main.app.routes))", "sqlite:///./ci.db")
    assert rc == 0, out
    assert "ROUTES" in out, out
    print("PASS the application still boots unchanged")


if __name__ == "__main__":
    test_a_fresh_database_is_built_and_stamped()
    test_an_existing_schema_is_stamped_not_rebuilt()
    test_running_twice_is_a_no_op()
    test_the_baseline_is_an_empty_anchor()
    test_downgrading_past_the_baseline_refuses()
    test_status_is_read_only()
    test_env_refuses_without_a_database_url()
    test_boot_still_works_untouched()
    print("ALL MIGRATION FRAMEWORK TESTS PASSED")
