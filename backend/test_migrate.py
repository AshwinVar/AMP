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
import re
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


class _TempDbWithPercent:
    """A throwaway SQLite database whose URL contains a percent sign.

    Percent is not exotic in a real connection string: it is what URL-encoding
    produces. A Postgres password containing '@' arrives as %40, '/' as %2F,
    ':' as %3A. Railway, RDS and Supabase all generate passwords from a
    character set that includes them, so a large fraction of real deployments
    hand AMP a DATABASE_URL with a % in it.
    """

    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        # %40 is what a literal '@' in a password looks like once encoded.
        self.path = os.path.join(self.dir, "amp%40audit.db")
        return f"sqlite:///{self.path.replace(os.sep, '/')}"

    def __exit__(self, *exc):
        for p in (self.path,):
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass


def test_a_url_containing_a_percent_sign_still_migrates():
    """A DATABASE_URL with a % in it must migrate, not crash the deploy.

    THE BUG THIS PINS. alembic/env.py handed the URL to
    `config.set_main_option("sqlalchemy.url", ...)`. Alembic's Config is backed
    by configparser, which treats % as interpolation syntax, so ANY url-encoded
    character raised

        ValueError: invalid interpolation syntax ... at position N

    and the release command died.

    WHY IT WAS WORSE THAN A CRASH. migrate.run() creates the schema BEFORE it
    stamps. So the failure landed between the two: 48 tables created,
    alembic_version absent. Every redeploy then hit the identical crash, and the
    database could never be stamped without hand intervention — while the app
    itself still booted fine, because main.py builds its own schema with
    create_all. A customer would see a working product with dead migrations.

    Production was unaffected only because Railway's current password happens to
    contain no encodable character. That is luck, not design, and it is exactly
    the kind of luck that runs out on the first customer you onboard.
    """
    with _TempDbWithPercent() as url:
        assert "%" in url, url
        rc, out = _run(
            "import migrate; from database import engine;"
            "migrate.run(verbose=False);"
            "print('STAMPED', migrate._is_stamped(engine));"
            "print('AT_HEAD', migrate.current_revision(engine)==migrate.head_revision())", url)
        assert rc == 0, out
        assert "STAMPED True" in out, out
        assert "AT_HEAD True" in out, out
        print("PASS a DATABASE_URL containing % migrates and stamps")


def test_a_percent_url_survives_a_second_run():
    """CONTROL for the half-migrated state.

    The original failure left a schema with no stamp, so the interesting
    question is not only "does the first run work" but "is the database in a
    state the next deploy can proceed from". Running twice proves the stamp
    took and the second pass is the intended no-op rather than a second crash.
    """
    with _TempDbWithPercent() as url:
        rc, out = _run(
            "import migrate; from database import engine;"
            "migrate.run(verbose=False); migrate.run(verbose=False);"
            "print('STILL_HEAD', migrate.current_revision(engine)==migrate.head_revision())", url)
        assert rc == 0, out
        assert "STILL_HEAD True" in out, out
        print("PASS a second run against a %-url database is a clean no-op")


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


def test_every_revision_id_fits_the_version_column():
    """alembic_version.version_num is VARCHAR(32). A longer revision id lets a
    migration apply every one of its ALTERs and THEN fail on the stamp.

    That is the worst possible shape of failure and it is not hypothetical:
    measured against PostgreSQL 18, revision "0003_tenant_scoped_document_
    numbers" (35 chars) ran its DDL and died on

        UPDATE alembic_version SET version_num='0003_tenant_scoped_document_numbers'
        psycopg2.errors.StringDataRightTruncation: value too long for type
        character varying(32)

    Alembic wraps a migration in a transaction so THAT case rolled back, but the
    deploy still fails on every subsequent attempt and the release command stays
    broken — the same dead-migrations outcome as #499, reached by a different
    route. SQLite never shows it: it ignores VARCHAR lengths entirely, so the
    whole suite passes locally while production cannot deploy.

    32 is Alembic's own default and is not configurable per-revision, so the
    only fix is short ids. Asserted here rather than left to review because the
    id is chosen once, in a file nobody opens again.
    """
    versions = os.path.join(HERE, "alembic", "versions")
    ids = []
    for name in sorted(os.listdir(versions)):
        if not name.endswith(".py"):
            continue
        src = io.open(os.path.join(versions, name), encoding="utf-8").read()
        m = re.search(r'^revision = "([^"]+)"', src, re.M)
        assert m, f"{name} declares no revision id"
        ids.append((name, m.group(1)))

    too_long = [(n, r, len(r)) for n, r, in
                [(n, r) for n, r in ids] if len(r) > 32]
    assert not too_long, (
        "revision id longer than alembic_version.version_num VARCHAR(32): "
        + ", ".join(f"{n}: {r!r} is {ln} chars" for n, r, ln in too_long))

    # CONTROL: the check is reading real ids, not an empty list. An empty
    # versions directory would satisfy the assertion above.
    assert len(ids) >= 2, f"expected several revisions, found {ids}"
    print(f"PASS all {len(ids)} revision ids fit VARCHAR(32) "
          f"(longest: {max(len(r) for _, r in ids)})")


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
    test_a_url_containing_a_percent_sign_still_migrates()
    test_a_percent_url_survives_a_second_run()
    test_an_existing_schema_is_stamped_not_rebuilt()
    test_running_twice_is_a_no_op()
    test_every_revision_id_fits_the_version_column()
    test_the_baseline_is_an_empty_anchor()
    test_downgrading_past_the_baseline_refuses()
    test_status_is_read_only()
    test_env_refuses_without_a_database_url()
    test_boot_still_works_untouched()
    print("ALL MIGRATION FRAMEWORK TESTS PASSED")
