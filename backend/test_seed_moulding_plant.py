"""The seeder builds a plant, refuses a bad part master, and never invents output.

WHAT IS PINNED
  1. It refuses to run without a password, and ships no default.
  2. Every way a part-master CSV can be wrong is refused by NAME, at the door,
     before a row is written — the same refusals the admin screen makes, because
     a CSV is the easiest way to enter fifty wrong numbers at once.
  3. It is idempotent: a second run corrects the plant rather than duplicating
     it, and a NEW effective date supersedes rather than overwrites.
  4. It writes no production records unless asked, and when asked it marks the
     machines (DEMO) so a seeded chart can never pass for a measured one.
  5. It stays inside its own tenant.

Run: DATABASE_URL="sqlite:///./ci_seed.db" python backend/test_seed_moulding_plant.py
"""
import io
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_seed.db")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import database  # noqa: E402
import models    # noqa: E402
import seed_moulding_plant as S  # noqa: E402

failures = []
T = "SEEDTEST"
OTHER = "SEEDTEST-OTHER"
TMP = os.path.join(HERE, "_seed_test_parts.csv")

GOOD = ("part_code,part_name,material,part_weight_g,cavities,active_cavities,"
        "ideal_cycle_time_s,price_per_piece\n"
        "ELE-CLIP,Ele clip,PP H 110,0.33,56,56,14,0.09\n"
        "CAP-A,Bottle cap,HDPE 5502,2.1,8,8,9.5,0.45\n")


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def write_csv(text):
    io.open(TMP, "w", encoding="utf-8", newline="").write(text)
    return TMP


def refuses(text, needle, label):
    try:
        S.read_parts(write_csv(text))
        check(label, False, "it was accepted")
    except S.Refused as e:
        check(label, needle.lower() in str(e).lower(), str(e))


def wipe(db):
    for M in (models.ProductionRecord, models.ToolAsset, models.PartSpec,
              models.Machine, models.User, models.TenantConfig, models.CompanyTenant):
        col = getattr(M, "tenant_code", None) or getattr(M, "company_code", None)
        db.query(M).filter(col.in_([T, OTHER])).delete(synchronize_session=False)
    db.commit()


def main():
    models.Base.metadata.create_all(bind=database.engine)
    db = database.SessionLocal()
    try:
        wipe(db)

        section("1. NO PASSWORD, NO SEED — AND NO DEFAULT ONE")
        os.environ.pop("SEED_PASSWORD", None)
        try:
            S.seed(db, tenant=T, name="X", machines=1, prefix="IMM",
                   parts=S.read_parts(write_csv(GOOD)), effective_from=date(2026, 1, 1))
            check("seeding without a password is refused", False, "it ran")
        except S.Refused as e:
            check("seeding without a password is refused",
                  "SEED_PASSWORD" in str(e), str(e))
        os.environ["SEED_PASSWORD"] = "short"
        try:
            S.seed(db, tenant=T, name="X", machines=1, prefix="IMM",
                   parts=S.read_parts(write_csv(GOOD)), effective_from=date(2026, 1, 1))
            check("a short password is refused", False, "it ran")
        except S.Refused:
            check("a short password is refused", True)
        os.environ["SEED_PASSWORD"] = "a-real-passphrase"

        section("2. THE PART MASTER IS VALIDATED BEFORE ANYTHING IS WRITTEN")
        head = GOOD.splitlines()[0] + "\n"
        refuses(head + "A,Part,PP,0,4,4,10,1", "part_weight_g",
                "a zero weight is refused and named")
        refuses(head + "A,Part,PP,1,4,4,0,1", "ideal_cycle_time_s",
                "a zero cycle time is refused and named")
        refuses(head + "A,Part,PP,heavy,4,4,10,1", "part_weight_g",
                "a non-numeric weight is refused")
        refuses(head + "A,Part,PP,1,16,18,10,1", "exceed",
                "more active cavities than the mould has is refused")
        refuses(head + ",Part,PP,1,4,4,10,1", "part_code", "a blank part code is refused")
        refuses(head + "A,Part,,1,4,4,10,1", "material", "a blank material is refused")
        refuses(head + "A,Part,PP,1,4,4,10,1\nA,Again,PP,1,4,4,10,2", "twice",
                "the same part code twice is refused")
        refuses(head, "no parts", "a header with no rows is refused")

        parts = S.read_parts(write_csv(GOOD))
        check("a zero price is ACCEPTED and means unpriced",
              S.read_parts(write_csv(head + "A,Part,PP,1,4,4,10,0"))[0]["price_per_piece"] == 0.0)
        check("blank active_cavities defaults to all of them",
              S.read_parts(write_csv(head + "A,Part,PP,1,16,,10,1"))[0]["active_cavities"] == 16)

        section("3. THE PLANT IT BUILDS")
        out = S.seed(db, tenant=T, name="Test Mouldings", machines=4, prefix="IMM",
                     parts=parts, effective_from=date(2026, 1, 1))
        check("the machines are created and named in order", out["machines"] == 4)
        names = sorted(m.name for m in db.query(models.Machine)
                       .filter(models.Machine.tenant_code == T).all())
        check("...zero-padded, so they sort correctly past nine",
              names == ["IMM-01", "IMM-02", "IMM-03", "IMM-04"], str(names))
        tools = {t.tool_no: t for t in db.query(models.ToolAsset)
                 .filter(models.ToolAsset.tenant_code == T).all()}
        check("a mould exists per part and names the part it makes",
              set(tools) == {"MLD-ELE-CLIP", "MLD-CAP-A"}
              and tools["MLD-ELE-CLIP"].part_code == "ELE-CLIP", str(set(tools)))
        check("...and each is fitted to a machine",
              all(t.machine_id is not None for t in tools.values()))
        check("the mould carries ACTIVE cavities, not the tool's full count",
              tools["MLD-ELE-CLIP"].cavities == 56, str(tools["MLD-ELE-CLIP"].cavities))
        admin = db.query(models.User).filter(models.User.username == out["admin"]).first()
        check("an Admin login exists for the workspace",
              admin is not None and admin.role == "Admin" and admin.tenant_code == T)
        check("...and the password is stored hashed, never in the clear",
              admin.password != "a-real-passphrase" and len(admin.password) > 20)

        section("4. NO PRODUCTION IS INVENTED")
        count = (db.query(models.ProductionRecord)
                   .filter(models.ProductionRecord.tenant_code == T).count())
        check("a plain seed writes NO production records at all", count == 0, str(count))
        check("...and leaves the machines Idle, not Running",
              all(m.status == "Idle" for m in db.query(models.Machine)
                  .filter(models.Machine.tenant_code == T).all()))

        section("5. A REHEARSAL DAY IS LABELLED (DEMO)")
        S.seed(db, tenant=T, name="Test Mouldings", machines=4, prefix="IMM",
               parts=parts, effective_from=date(2026, 1, 1), demo_production=True)
        count = (db.query(models.ProductionRecord)
                   .filter(models.ProductionRecord.tenant_code == T).count())
        check("--demo-production writes a day of hours", count == 20, str(count))
        touched = [m.name for m in db.query(models.Machine)
                   .filter(models.Machine.tenant_code == T).all()
                   if m.status == "Running"]
        check("...on the machines it was given", len(touched) == 2, str(touched))
        # The (DEMO) label belongs in the machine NAME, chosen by main() before any
        # machine exists -- applying it afterwards as a rename broke idempotency,
        # because the name is the key this seeder matches on.
        check("a rehearsal run names its machines DEMO- from the start",
              S.demo_prefix("IMM", True) == "DEMO-IMM", S.demo_prefix("IMM", True))
        check("...a real run is left alone",
              S.demo_prefix("IMM", False) == "IMM", S.demo_prefix("IMM", False))
        check("...and a prefix that already says DEMO is not doubled",
              S.demo_prefix("DEMO-IMM", True) == "DEMO-IMM", S.demo_prefix("DEMO-IMM", True))
        check("...and nothing renames a machine after the fact",
              all("(DEMO)" not in m.name for m in db.query(models.Machine)
                  .filter(models.Machine.tenant_code == T).all()))

        section("6. RE-RUNNING CORRECTS, IT DOES NOT DUPLICATE")
        before = (db.query(models.Machine).filter(models.Machine.tenant_code == T).count(),
                  db.query(models.ToolAsset).filter(models.ToolAsset.tenant_code == T).count(),
                  db.query(models.PartSpec).filter(models.PartSpec.tenant_code == T).count())
        corrected = [dict(p) for p in parts]
        corrected[0]["part_weight_g"] = 0.35          # a corrected weighing
        S.seed(db, tenant=T, name="Test Mouldings", machines=4, prefix="IMM",
               parts=corrected, effective_from=date(2026, 1, 1))
        after = (db.query(models.Machine).filter(models.Machine.tenant_code == T).count(),
                 db.query(models.ToolAsset).filter(models.ToolAsset.tenant_code == T).count(),
                 db.query(models.PartSpec).filter(models.PartSpec.tenant_code == T).count())
        check("nothing is duplicated on a second run", before == after, f"{before} -> {after}")
        spec = (db.query(models.PartSpec)
                  .filter(models.PartSpec.tenant_code == T,
                          models.PartSpec.part_code == "ELE-CLIP",
                          models.PartSpec.effective_from == date(2026, 1, 1)).first())
        check("...and the same date is corrected in place", spec.part_weight_g == 0.35,
              str(spec.part_weight_g))

        section("7. A NEW DATE SUPERSEDES, SO LAST MONTH DOES NOT MOVE")
        risen = [dict(p) for p in parts]
        risen[0]["price_per_piece"] = 0.12
        S.seed(db, tenant=T, name="Test Mouldings", machines=4, prefix="IMM",
               parts=risen, effective_from=date(2026, 6, 1))
        prices = sorted(s.price_per_piece for s in db.query(models.PartSpec)
                        .filter(models.PartSpec.tenant_code == T,
                                models.PartSpec.part_code == "ELE-CLIP").all())
        check("both prices are on record", prices == [0.09, 0.12], str(prices))

        section("8. IT STAYS IN ITS OWN WORKSPACE")
        S.seed(db, tenant=OTHER, name="Somebody Else", machines=2, prefix="PRESS",
               parts=parts, effective_from=date(2026, 1, 1))
        mine = db.query(models.Machine).filter(models.Machine.tenant_code == T).count()
        check("seeding another workspace leaves this one's machines alone",
              mine == 4, str(mine))
        theirs = [m.name for m in db.query(models.Machine)
                  .filter(models.Machine.tenant_code == OTHER).all()]
        check("...and the other workspace has only its own",
              sorted(theirs) == ["PRESS-01", "PRESS-02"], str(theirs))

        section("9. A RESERVED TENANT CODE IS REFUSED (ADR-0017)")
        try:
            S.seed(db, tenant="OEM:ACME", name="x", machines=1, prefix="IMM",
                   parts=parts, effective_from=date(2026, 1, 1))
            check("an OEM-sentinel code is refused", False, "it was accepted")
        except ValueError as e:
            check("an OEM-sentinel code is refused", "reserved" in str(e).lower(), str(e))

        wipe(db)
    finally:
        db.close()
        if os.path.exists(TMP):
            os.remove(TMP)


if __name__ == "__main__":
    main()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - moulding plant seeder")
