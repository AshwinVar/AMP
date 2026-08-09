"""Load test: how does AMP behave at 10, 50, 250 and 1000 machines?

TOOLING, AND WHY IT IS NOT k6
-----------------------------
k6 is not installed on this machine and installing it would mean fetching a
binary from the network. This driver uses what IS installed -- `requests` for
HTTP and a thread pool for concurrency -- against a LOCAL uvicorn serving a
DISPOSABLE PostgreSQL database. It never touches production.

The honest cost of that choice: a Python client has real per-request overhead,
so at high concurrency the CLIENT can become the bottleneck and the numbers then
describe the client, not the server. Section 0 measures the client's own floor
against a trivial endpoint before anything else runs, and every result is
reported against that floor. A latency at or near the floor means "we did not
measure the server here" -- not "the server is fast".

WHAT IS MEASURED
----------------
    0  client floor      the driver's own overhead, so the rest can be read
    1  HTTP latency      p50 / p95 / p99 / RPS / error rate, per endpoint
    2  database latency  the same work without HTTP, to split server from DB
    3  WebSocket         N concurrent live connections, and broadcast fan-out
    4  MQTT              ingest throughput through the real handler
    5  bottleneck        which of the above moves first as N grows

Run:  python backend/loadtest.py            (all four scales)
      python backend/loadtest.py 10 50      (only those)
"""
import json
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SCALES = [10, 50, 250, 1000]
PORT = 8931
BASE = f"http://127.0.0.1:{PORT}"
TENANT = "LOADTEST"
USER, PASSWORD = "loadtest-admin", "loadtest-pw"

# The dashboard's real polling set, plus the two endpoints previous phases
# measured as the heaviest. Weighted the way a dashboard actually polls.
ENDPOINTS = [
    "/machines",
    "/oee/summary",
    "/analytics/summary",
    "/analytics/executive-oee",
    "/work-orders",
    "/inventory/items",
    "/downtime-logs",
    "/agent-actions",
]

DURATION = 6          # seconds of sustained load per endpoint per scale
CONCURRENCY = 8       # threads; see section 0 for why this is not larger


def pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k] * 1000.0          # ms


def seed(url, n_machines):
    """A tenant with n_machines and proportional operational history."""
    env = dict(os.environ, DATABASE_URL=url)
    script = f'''
import os, sys
sys.path.insert(0, {HERE!r})
from datetime import datetime, timedelta
import models, tenancy
from database import Base, engine, SessionLocal
from security import hash_password
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
db = SessionLocal()
db.add(models.User(username={USER!r}, password=hash_password({PASSWORD!r}),
                   role="Admin", tenant_code={TENANT!r}, is_active=True))
now = datetime.utcnow()
for i in range({n_machines}):
    m = models.Machine(tenant_code={TENANT!r}, name=f"CNC-{{i:04d}}", site="Plant1",
                       status="Running" if i % 7 else "Breakdown",
                       utilization=50 + (i % 50))
    db.add(m)
    db.flush()
    db.add(models.ProductionRecord(
        tenant_code={TENANT!r}, machine_id=m.id, planned_minutes=480,
        runtime_minutes=400 + (i % 80), ideal_cycle_time_seconds=30,
        total_count=600, good_count=560 + (i % 40), rejected_count=40,
        created_at=now - timedelta(hours=(i % 100))))
    db.add(models.DowntimeLog(tenant_code={TENANT!r}, machine_id=m.id,
                              reason="Wear", duration=f"{{10 + i % 50}} min",
                              created_at=now - timedelta(hours=(i % 90))))
    db.add(models.WorkOrder(
        tenant_code={TENANT!r}, work_order_no=f"WO-{{i:05d}}", part_number="FG-001",
        batch_number="B1", machine_id=m.id, target_quantity=100,
        status="In Progress" if i % 3 else "Completed"))
    db.add(models.InventoryItem(
        tenant_code={TENANT!r}, item_code=f"INV-{{i:05d}}", item_name=f"Part {{i}}",
        category="Raw", unit="kg", current_stock=100 + i, reorder_level=10))
    if i % 5 == 0:
        db.add(models.AgentAction(
            tenant_code={TENANT!r}, agent="reorder", action_type="draft_po",
            summary="restock", ref_kind="purchase_order", ref_id=None,
            status="Proposed", created_at=now))
    if i % 100 == 0:
        db.commit()
db.commit()
print(db.query(models.Machine).count())
db.close()
'''
    r = subprocess.run([sys.executable, "-c", script], env=env, cwd=HERE,
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"seed failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return int(r.stdout.strip().splitlines()[-1])


def start_server(url):
    env = dict(os.environ, DATABASE_URL=url, SECRET_KEY="loadtest-secret-key-32chars-long",
               AMP_DISABLE_SIM="1")
    # Server output goes to a FILE, never to a pipe. AMP writes a structured
    # JSON access line per request, so an undrained PIPE fills its 64 KiB
    # buffer and the SERVER BLOCKS. The first run of this harness reported
    # 1.3 rps and 100% timeouts at every scale, which was that deadlock and not
    # anything about AMP.
    log_path = os.path.join(HERE, "loadtest_server.log")
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "error"],
        cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            requests.get(f"{BASE}/health", timeout=1)
            return proc
        except Exception:
            if proc.poll() is not None:
                log.close()
                with open(log_path, encoding="utf-8", errors="replace") as fh:
                    raise SystemExit(f"server died:\n{fh.read()[-3000:]}")
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit("server did not become ready in 60s")


def token():
    r = requests.post(f"{BASE}/login",
                      json={"username": USER, "password": PASSWORD}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def hammer(path, headers, duration=DURATION, concurrency=CONCURRENCY):
    """Sustained load on one endpoint. Returns (latencies, errors, codes)."""
    deadline = time.time() + duration
    latencies, errors, codes = [], 0, {}

    def worker():
        nonlocal errors
        local = []
        # One Session PER THREAD, so connections are reused. Without this the
        # driver opens a new TCP connection per request and Windows stalls them
        # into 30-second timeouts -- the first run of this harness reported
        # 1.3 rps and 100% errors, which measured the socket stack and not AMP.
        session = requests.Session()
        while time.time() < deadline:
            t0 = time.perf_counter()
            try:
                r = session.get(f"{BASE}{path}", headers=headers, timeout=30)
                dt = time.perf_counter() - t0
                codes[r.status_code] = codes.get(r.status_code, 0) + 1
                if r.status_code >= 400:
                    errors += 1
                local.append(dt)
            except Exception:
                errors += 1
                local.append(time.perf_counter() - t0)
        return local

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for chunk in pool.map(lambda _: worker(), range(concurrency)):
            latencies.extend(chunk)
    return latencies, errors, codes


def db_latency(url, n):
    """The same aggregate work, without HTTP, so the server and the database
    can be told apart."""
    env = dict(os.environ, DATABASE_URL=url)
    script = f'''
import os, sys, time, statistics
sys.path.insert(0, {HERE!r})
import models, tenancy, oee_contract
from database import SessionLocal
tenancy.install_scoping()
db = SessionLocal()
out = {{}}
for label, fn in (
    ("count machines", lambda: db.query(models.Machine).filter(
        models.Machine.tenant_code == {TENANT!r}).count()),
    ("list machines", lambda: db.query(models.Machine).filter(
        models.Machine.tenant_code == {TENANT!r}).all()),
    ("plant OEE", lambda: oee_contract.plant_oee(db, {TENANT!r})),
):
    samples = []
    for _ in range(7):
        t0 = time.perf_counter(); fn(); samples.append(time.perf_counter() - t0)
    out[label] = round(statistics.median(samples) * 1000, 2)
db.close()
import json; print(json.dumps(out))
'''
    r = subprocess.run([sys.executable, "-c", script], env=env, cwd=HERE,
                       capture_output=True, text=True, errors="replace")
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": (r.stderr or r.stdout)[-200:]}


def ws_and_mqtt(url, n):
    """WebSocket fan-out and MQTT ingest, in-process against the real code."""
    env = dict(os.environ, DATABASE_URL=url)
    script = f'''
import os, sys, time, json, asyncio
sys.path.insert(0, {HERE!r})
import live_ws
out = {{}}

class Sock:
    def __init__(self): self.n = 0
    async def accept(self): pass
    async def send_text(self, t): self.n += 1

async def run():
    m = live_ws.ConnectionManager()
    socks = [Sock() for _ in range({n})]
    t0 = time.perf_counter()
    for s in socks:
        await m.connect(s, {TENANT!r})
    out["connect_{n}_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    payload = {{"event": "machine_update", "tenant_code": {TENANT!r},
                "machine": {{"name": "CNC-0001", "status": "Running"}}}}
    t0 = time.perf_counter()
    rounds = 20
    for _ in range(rounds):
        await m.broadcast(payload)
    dt = time.perf_counter() - t0
    out["broadcast_ms_per_round"] = round(dt / rounds * 1000, 2)
    out["frames_delivered"] = sum(s.n for s in socks)
    out["fanout_per_sec"] = int(sum(s.n for s in socks) / dt) if dt else 0
asyncio.new_event_loop().run_until_complete(run())
print(json.dumps(out))
'''
    r = subprocess.run([sys.executable, "-c", script], env=env, cwd=HERE,
                       capture_output=True, text=True, errors="replace")
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": (r.stderr or r.stdout)[-300:]}


def mqtt_ingest(url, n):
    """Messages/second through the real MQTT handler, writing to PostgreSQL."""
    env = dict(os.environ, DATABASE_URL=url)
    script = f'''
import os, sys, time, json
sys.path.insert(0, {HERE!r})
import mqtt_service, mqtt_identity, models, tenancy
from database import SessionLocal
tenancy.install_scoping()
mqtt_service.SessionLocal = SessionLocal
db = SessionLocal()
names = [m.name for m in db.query(models.Machine).filter(
    models.Machine.tenant_code == {TENANT!r}).limit(200).all()]
db.close()
sent, t0 = 0, time.perf_counter()
deadline = t0 + 4
i = 0
while time.perf_counter() < deadline and names:
    name = names[i % len(names)]
    payload = json.dumps({{"machine": name, "status": "Running",
                          "utilization": 70, "total_count": 10,
                          "good_count": 9, "rejected_count": 1}})

    class M:
        topic = f"amp/{TENANT}/Plant1/machines"
        payload = payload.encode()
    try:
        mqtt_service.on_message(None, None, M())
        sent += 1
    except Exception as e:
        print(json.dumps({{"error": repr(e)[:200]}})); raise SystemExit(0)
    i += 1
dt = time.perf_counter() - t0
print(json.dumps({{"messages": sent, "seconds": round(dt, 2),
                  "msgs_per_sec": int(sent / dt) if dt else 0}}))
'''
    r = subprocess.run([sys.executable, "-c", script], env=env, cwd=HERE,
                       capture_output=True, text=True, errors="replace")
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": (r.stderr or r.stdout)[-300:]}


def main():
    import pg_scratch
    scales = [int(a) for a in sys.argv[1:] if a.isdigit()] or SCALES
    version = pg_scratch.ensure(5432, "amp_loadtest")
    url = pg_scratch.scratch_url(5432, "amp_loadtest")
    print(version.split(",")[0])
    print(f"driver: python requests + {CONCURRENCY} threads (k6 is not installed; "
          f"this never runs against production)")
    print(f"scales: {scales}   duration: {DURATION}s per endpoint\n")

    results = {}
    for n in scales:
        print("=" * 78)
        print(f"{n} MACHINES")
        print("=" * 78)
        t0 = time.time()
        seeded = seed(url, n)
        print(f"  seeded {seeded} machines in {time.time() - t0:.1f}s")
        proc = start_server(url)
        try:
            hdr = {"Authorization": f"Bearer {token()}"}

            # --- 0. the client's own floor --------------------------------
            lat, _, _ = hammer("/health", {}, duration=3)
            floor = pct(lat, 50)
            print(f"  client floor (/health, no auth, no DB): "
                  f"p50 {floor:.1f} ms over {len(lat)} requests")

            rows = []
            for path in ENDPOINTS:
                lat, errs, codes = hammer(path, hdr)
                rps = len(lat) / DURATION
                rows.append((path, len(lat), rps, pct(lat, 50), pct(lat, 95),
                             pct(lat, 99), errs, codes))
            print(f"  {'endpoint':<28}{'reqs':>7}{'rps':>8}{'p50':>9}"
                  f"{'p95':>9}{'p99':>9}{'err':>6}")
            for path, count, rps, p50, p95, p99, errs, codes in rows:
                print(f"  {path:<28}{count:>7}{rps:>8.1f}{p50:>9.1f}"
                      f"{p95:>9.1f}{p99:>9.1f}{errs:>6}")
            results[n] = {"floor_ms": round(floor, 1),
                          "http": [{"path": p, "reqs": c, "rps": round(r, 1),
                                    "p50": round(a, 1), "p95": round(b, 1),
                                    "p99": round(d, 1), "errors": e,
                                    "codes": co}
                                   for p, c, r, a, b, d, e, co in rows]}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        results[n]["db_ms"] = db_latency(url, n)
        print(f"  database (no HTTP): {results[n]['db_ms']}")
        results[n]["ws"] = ws_and_mqtt(url, n)
        print(f"  websocket: {results[n]['ws']}")
        results[n]["mqtt"] = mqtt_ingest(url, n)
        print(f"  mqtt ingest: {results[n]['mqtt']}")
        print()

    out = os.path.join(HERE, "loadtest_results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"raw results -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
