"""Two factories press ACCEPT at the same instant. Exactly one may win.

WHY THIS CANNOT BE A UNIT TEST
------------------------------
SQLite serialises writers, so a race there proves nothing — the second
transaction simply waits and then sees the first one's result. The bug this
guards against only appears when two transactions genuinely run at once, which
needs a real PostgreSQL and real threads.

THE INVARIANT MUST BE THE DATABASE'S, NOT PYTHON'S
--------------------------------------------------
The tempting implementation is read-modify-write:

    claim = db.query(...).first()
    if claim.status == "Pending":       # both transactions see Pending
        claim.status = "Claimed"        # both write it
        inst.factory_tenant_code = ...  # both assign the machine

Both administrators get a 200. The machine ends up at whichever transaction
committed last, and the other factory has a confirmation, a notification, and a
row on their Connected Equipment screen for a machine they do not have.

`oem_claims.accept` instead issues two conditional UPDATEs whose ROW COUNT is the
decision, so the database arbitrates:

    UPDATE machine_claims        SET status='Claimed' WHERE id=? AND status='Pending'
    UPDATE machine_installations SET factory_tenant_code=? WHERE id=? AND factory_tenant_code IS NULL

Run: python backend/verify_pg_claim.py [port]        (PostgreSQL only)
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB = "amp_scratch_claim"
ROUNDS = 25
FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "5432"
    import pg_scratch
    version = pg_scratch.ensure(port, DB)
    url = pg_scratch.scratch_url(port, DB)
    os.environ["DATABASE_URL"] = url

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models
    import oem_claims
    import tenancy
    from database import Base

    engine = create_engine(url, pool_size=12, max_overflow=12)
    Session = sessionmaker(bind=engine)
    print(version.split(",")[0])
    print(f"{ROUNDS} races, two factories each\n")

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    tenancy.install_scoping()

    db = Session()
    tok = tenancy.set_current_tenant(None)
    db.add(models.OemOrganization(oem_code="OEM_RACE", name="Race"))
    model = models.MachineModel(oem_code="OEM_RACE", family="Compressor",
                                model_code="X", name="X")
    db.add(model)
    db.commit()
    model_id = model.id
    tenancy.reset_current_tenant(tok)
    db.close()

    print("1. TWO FACTORIES, ONE CODE, AT THE SAME INSTANT")
    print("-" * 72)
    winners_seen, both_won, none_won = [], 0, 0

    for round_no in range(ROUNDS):
        setup = Session()
        tok = tenancy.set_current_tenant(None)
        inst = models.MachineInstallation(
            oem_code="OEM_RACE", serial_number=f"SN-RACE-{round_no:03d}",
            model_id=model_id, factory_tenant_code=None, site="",
            status="Manufactured")
        setup.add(inst)
        setup.commit()
        claim, code = oem_claims.create(setup, "OEM_RACE", inst, "race-admin")
        setup.commit()
        claim_id, inst_id = claim.id, inst.id
        tenancy.reset_current_tenant(tok)
        setup.close()

        results = {}
        barrier = threading.Barrier(2)

        def contend(tenant):
            # Each thread gets its OWN session and connection; anything shared
            # would serialise the very thing under test.
            s = Session()
            t = tenancy.set_current_tenant(None)
            try:
                c = s.query(models.MachineClaim).filter(
                    models.MachineClaim.id == claim_id).first()
                i = s.query(models.MachineInstallation).filter(
                    models.MachineInstallation.id == inst_id).first()
                barrier.wait(timeout=10)          # start together
                won = oem_claims.accept(s, c, i, tenant, f"{tenant}-admin")
                if won:
                    s.commit()
                else:
                    s.rollback()
                results[tenant] = won
            except Exception as e:
                s.rollback()
                results[tenant] = f"error: {e}"
            finally:
                tenancy.reset_current_tenant(t)
                s.close()

        threads = [threading.Thread(target=contend, args=(t,))
                   for t in ("FACTORY_A", "FACTORY_B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        won = [t for t, r in results.items() if r is True]
        if len(won) == 2:
            both_won += 1
        elif not won:
            none_won += 1
        else:
            winners_seen.append(won[0])

        # THE DATABASE IS THE ARBITER: whatever the threads believe, the row can
        # name only one factory.
        verify = Session()
        tv = tenancy.set_current_tenant(None)
        row = verify.query(models.MachineInstallation).filter(
            models.MachineInstallation.id == inst_id).first()
        cl = verify.query(models.MachineClaim).filter(
            models.MachineClaim.id == claim_id).first()
        actual_tenant, actual_status = row.factory_tenant_code, cl.status
        tenancy.reset_current_tenant(tv)
        verify.close()

        if won and actual_tenant != won[0]:
            FAILURES.append(
                f"round {round_no}: winner said {won[0]} but the row says {actual_tenant}")
        if actual_status != "Claimed":
            FAILURES.append(f"round {round_no}: claim status is {actual_status}")

    check(f"never did BOTH factories win ({ROUNDS} races)", both_won == 0,
          f"{both_won} rounds had two winners")
    check("never did NEITHER win", none_won == 0, f"{none_won} rounds had no winner")
    check("exactly one winner in every race", len(winners_seen) == ROUNDS,
          f"{len(winners_seen)} of {ROUNDS}")
    # Not an assertion about fairness — only that the outcome is not hard-wired
    # to whichever thread happens to start first.
    print(f"       winners: FACTORY_A {winners_seen.count('FACTORY_A')}, "
          f"FACTORY_B {winners_seen.count('FACTORY_B')}")

    print()
    print("2. THE LOSER'S TRANSACTION LEFT NOTHING BEHIND")
    print("-" * 72)
    db = Session()
    tok = tenancy.set_current_tenant(None)
    assigned = db.query(models.MachineInstallation).filter(
        models.MachineInstallation.factory_tenant_code.isnot(None)).count()
    claimed = db.query(models.MachineClaim).filter(
        models.MachineClaim.status == "Claimed").count()
    multi = db.execute(__import__("sqlalchemy").text(
        "SELECT count(*) FROM machine_claims WHERE status <> 'Claimed'")).scalar()
    tenancy.reset_current_tenant(tok)
    db.close()
    check("every raced machine has exactly one factory", assigned == ROUNDS,
          f"{assigned} of {ROUNDS}")
    check("every claim was spent exactly once", claimed == ROUNDS,
          f"{claimed} of {ROUNDS}")
    check("no claim was left half-used", multi == 0, str(multi))

    print()
    print("3. THE UNIQUE INDEX ON token_hash IS ENFORCED BY POSTGRESQL")
    print("-" * 72)
    from sqlalchemy import text
    from datetime import datetime, timedelta
    with engine.begin() as conn:
        inst_id = conn.execute(text(
            "INSERT INTO machine_installations "
            "(oem_code, serial_number, model_id, site, status) "
            f"VALUES ('OEM_RACE','SN-DUP',{model_id},'','Manufactured') "
            "RETURNING id")).scalar()
    try:
        with engine.begin() as conn:
            for _ in range(2):
                conn.execute(text(
                    "INSERT INTO machine_claims "
                    "(oem_code, installation_id, token_hash, code_hint, status, expires_at) "
                    "VALUES ('OEM_RACE', :i, 'the-same-hash', 'ABCD', 'Pending', :e)"),
                    {"i": inst_id, "e": datetime.utcnow() + timedelta(days=1)})
        check("two claims cannot share a token hash", False, "the duplicate was accepted")
    except Exception as e:
        check("two claims cannot share a token hash — the DATABASE refuses",
              "unique" in str(e).lower(), str(e)[:120])

    engine.dispose()
    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print("   *", f)
        return 1
    print("EXACTLY ONE FACTORY WINS, AND POSTGRESQL IS WHAT DECIDES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
