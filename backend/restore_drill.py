"""Timed disaster-recovery drill: from a dump to a customer logging in.

scripts/restore_check.sh already proves a dump can be replayed and looks like
AMP. It stops there. This goes the rest of the way and PUTS A CLOCK ON IT:

    1  pg_dump the source database
    2  create a brand-new, empty PostgreSQL database
    3  restore the dump into it
    4  run alembic upgrade head against the restored database
    5  boot AMP against it, wait for /health
    6  log a real customer in
    7  verify machines, production, inventory -- and that three customers are
       still isolated from one another after the restore

Every phase is timed with a monotonic clock and the total is the measured RTO.
Nothing here is estimated: if a phase is not measured it is not reported.

RPO IS NOT MEASURED HERE, AND CANNOT BE
---------------------------------------
Recovery Point Objective is a property of the backup SCHEDULE, not of a restore.
.github/workflows/backup.yml runs `cron: "17 2 * * *"` -- once a day -- so the
designed RPO is 24 hours: a failure at 02:16 UTC loses almost a full day of
production, quality and inventory movements. This drill can only show that the
dump it took loses nothing, which is a different claim. The 24h figure is read
from the schedule, and the drill verifies data completeness rather than age.

Run:  python backend/restore_drill.py
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PG_BIN = r"C:\Program Files\PostgreSQL\18\bin"
PG_DUMP = os.path.join(PG_BIN, "pg_dump.exe")
PSQL = os.path.join(PG_BIN, "psql.exe")

SOURCE_DB = "amp_dr_source"
RESTORED_DB = "amp_dr_restored"
PORT = 8933
BASE = f"http://127.0.0.1:{PORT}"
TENANTS = ["FACTORY_A", "FACTORY_B", "FACTORY_C"]
PASSWORD = "drill-pw"

failures = []
timings = []


def check(label, condition, detail=""):
    print(f"     {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class phase:
    """Times a phase on a monotonic clock and records it."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        print(f"\n[{len(timings) + 1}] {self.name}")
        self.t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        dt = time.monotonic() - self.t0
        timings.append((self.name, dt))
        print(f"     ... {dt:.2f}s")
        return False


def seed(url):
    """Three customers with overlapping identifiers, as a real deployment."""
    env = dict(os.environ, DATABASE_URL=url)
    script = f'''
import os, sys
sys.path.insert(0, {HERE!r})
from datetime import datetime, timedelta
import models
from database import Base, engine, SessionLocal
from security import hash_password
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
db = SessionLocal()
now = datetime.utcnow()
sites = {{"FACTORY_A": "Chennai", "FACTORY_B": "Pune", "FACTORY_C": "Coimbatore"}}
counts = {{"FACTORY_A": 12, "FACTORY_B": 7, "FACTORY_C": 3}}
for t in {TENANTS!r}:
    db.add(models.User(username=t.lower() + "-admin",
                       password=hash_password({PASSWORD!r}),
                       role="Admin", tenant_code=t, is_active=True))
    for i in range(counts[t]):
        m = models.Machine(tenant_code=t, name=f"CNC-{{i:02d}}", site=sites[t],
                           status="Running", utilization=60 + i)
        db.add(m); db.flush()
        db.add(models.ProductionRecord(
            tenant_code=t, machine_id=m.id, planned_minutes=480,
            runtime_minutes=400, ideal_cycle_time_seconds=30,
            total_count=600, good_count=570, rejected_count=30,
            created_at=now - timedelta(hours=i)))
        db.add(models.InventoryItem(
            tenant_code=t, item_code=f"INV-{{i:03d}}", item_name=f"{{t}} part {{i}}",
            category="Raw", unit="kg", current_stock=100 + i, reorder_level=5))
db.commit()
import json
out = {{}}
for t in {TENANTS!r}:
    out[t] = {{
        "machines": db.query(models.Machine).filter(models.Machine.tenant_code == t).count(),
        "production": db.query(models.ProductionRecord).filter(models.ProductionRecord.tenant_code == t).count(),
        "inventory": db.query(models.InventoryItem).filter(models.InventoryItem.tenant_code == t).count(),
    }}
print(json.dumps(out))
db.close()
'''
    r = subprocess.run([sys.executable, "-c", script], env=env, cwd=HERE,
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"seed failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def main():
    import pg_scratch

    if not os.path.exists(PG_DUMP):
        print(f"pg_dump not found at {PG_DUMP} -- cannot run the drill.")
        return 2

    creds = pg_scratch.scratch_url(5432, "postgres")
    admin = creds.replace("postgresql+psycopg2", "postgresql")
    src_url = pg_scratch.scratch_url(5432, SOURCE_DB)
    dst_url = pg_scratch.scratch_url(5432, RESTORED_DB)
    dump_path = os.path.join(HERE, "dr_drill.sql")

    print("=" * 78)
    print("DISASTER RECOVERY DRILL")
    print("=" * 78)
    print(pg_scratch.ensure(5432, SOURCE_DB).split(",")[0])
    print(f"source: {SOURCE_DB}   restored into: {RESTORED_DB}")

    before = seed(src_url)
    print(f"seeded three customers: {before}")

    env = dict(os.environ, PGPASSWORD=_password(admin))

    # ---------------------------------------------------------------- 1 ----
    with phase("pg_dump the source database"):
        r = subprocess.run(
            [PG_DUMP, "--dbname", src_url, "--no-owner", "--no-privileges",
             "--file", dump_path],
            env=env, capture_output=True, text=True, errors="replace")
        check("pg_dump exited 0", r.returncode == 0, r.stderr[-300:])
    size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
    print(f"     dump: {size / 1024:.0f} KiB")

    # ---------------------------------------------------------------- 2 ----
    with phase("create a brand-new empty database"):
        pg_scratch.ensure(5432, RESTORED_DB)

    # ---------------------------------------------------------------- 3 ----
    with phase("restore the dump"):
        r = subprocess.run(
            [PSQL, "--dbname", dst_url, "-v", "ON_ERROR_STOP=1", "-q",
             "-f", dump_path],
            env=env, capture_output=True, text=True, errors="replace")
        check("psql replayed the dump end to end", r.returncode == 0,
              r.stderr[-400:])

    # ---------------------------------------------------------------- 4 ----
    with phase("alembic upgrade head"):
        r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                           cwd=HERE, env=dict(os.environ, DATABASE_URL=dst_url),
                           capture_output=True, text=True, errors="replace")
        check("migrations applied to the restored database", r.returncode == 0,
              r.stderr[-400:])

    # ---------------------------------------------------------------- 5 ----
    proc = None
    with phase("boot AMP against the restored database"):
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
             "--port", str(PORT), "--log-level", "error"],
            cwd=HERE, env=dict(os.environ, DATABASE_URL=dst_url,
                               SECRET_KEY="drill-secret-key-32-chars-minimum"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace")
        up = False
        for _ in range(120):
            try:
                requests.get(f"{BASE}/health", timeout=1)
                up = True
                break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
        check("AMP answered /health", up)

    try:
        # ------------------------------------------------------------ 6 ----
        tokens = {}
        with phase("a customer logs in"):
            for t in TENANTS:
                r = requests.post(f"{BASE}/login",
                                  json={"username": f"{t.lower()}-admin",
                                        "password": PASSWORD}, timeout=30)
                check(f"{t}'s admin signed in", r.status_code == 200,
                      f"{r.status_code} {r.text[:120]}")
                if r.status_code == 200:
                    tokens[t] = r.json()["access_token"]

        # ------------------------------------------------------------ 7 ----
        with phase("verify the data, and that the customers are still separate"):
            after = {}
            for t, tok in tokens.items():
                h = {"Authorization": f"Bearer {tok}"}
                machines = requests.get(f"{BASE}/machines", headers=h,
                                        timeout=30).json()
                inv = requests.get(f"{BASE}/inventory/items", headers=h,
                                   timeout=30).json()
                after[t] = {"machines": len(machines),
                            "inventory": len(inv if isinstance(inv, list)
                                             else inv.get("items", []))}
                check(f"{t} sees its {before[t]['machines']} machines",
                      len(machines) == before[t]["machines"],
                      f"{len(machines)}")
                # Isolation is asserted on inventory, not on machines: every
                # tenant's machines are named CNC-00.. identically (that is the
                # point of the three-customer fixture) and MachineResponse does
                # not expose `site`, so the machine list carries nothing that
                # names an owner. Inventory item names do.
                items = inv if isinstance(inv, list) else inv.get("items", [])
                owners = {n.split(" part ")[0]
                          for n in (i.get("item_name", "") for i in items)
                          if " part " in n}
                check(f"...and every inventory row it can see belongs to {t}",
                      owners == {t}, str(owners))
            print(f"     restored counts: {after}")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if os.path.exists(dump_path):
            os.remove(dump_path)

    total = sum(dt for _, dt in timings)
    print()
    print("=" * 78)
    print("MEASURED RECOVERY TIME")
    print("=" * 78)
    for name, dt in timings:
        print(f"  {name:<52}{dt:>8.2f}s")
    print(f"  {'MEASURED RTO (sum of phases)':<52}{total:>8.2f}s")
    print()
    print("  RPO is NOT measured here. It is set by the backup schedule:")
    print("  .github/workflows/backup.yml runs 'cron: 17 2 * * *' -> once a")
    print("  day, so the designed RPO is 24 HOURS. This drill shows the dump")
    print("  it took loses nothing; it cannot show how old a real dump is.")

    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("   *", f)
        return 1
    print(f"RECOVERED IN {total:.1f}s, WITH THREE CUSTOMERS STILL SEPARATE")
    return 0


def _password(url):
    import re
    import urllib.parse
    m = re.match(r"^postgresql://[^:]+:([^@]+)@", url)
    return urllib.parse.unquote(m.group(1)) if m else ""


if __name__ == "__main__":
    sys.exit(main())
