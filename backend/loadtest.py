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
    6  verdict           what the above MEANS, stated rather than left to a diff

WHY SECTION 6 EXISTS
--------------------
The promise above -- "every result is reported against that floor" -- was made
by this docstring and not kept by the code, which printed the floor once and
then printed raw milliseconds. Re-running an unchanged codebase months later
produced numbers ~1.5x worse across the board and looked exactly like a severe
regression. It was not one: the client floor and the MQTT loop, neither of which
touches AMP's request path, had slowed by the same factor. The machine was
busier. So each p50 now carries an `xfloor` column, and the run ends by
comparing itself to the committed baseline in those normalised terms.

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


# How much a floor-normalised ratio may move before it counts as real. A ratio
# above 1.25 means this run was that much slower once the machine is accounted
# for. Calibration: re-running an UNCHANGED codebase on a different day moved
# these by -11% to +22% across sixteen endpoint/scale pairs, so anything inside
# +/-25% is this harness's noise and must not be reported as a regression.
NOISE = 0.25

# A p50 this close to the client floor means the driver, not the server, was the
# thing being measured -- the docstring's warning, made checkable.
FLOOR_SUSPECT = 1.5

# How close a p50 must come to C/RPS before it is called queueing rather than
# service time. Measured: across 32 endpoint/scale pairs the ratio was 1.01-1.09
# for the four saturated endpoints and 0.61-0.81 for the eight that were not, so
# the two populations are cleanly separated and 0.10 sits in the gap.
QUEUE_TOL = 0.10


def load_baseline():
    """The previous run's numbers, read BEFORE this run overwrites them."""
    try:
        with open(os.path.join(HERE, "loadtest_results.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def merge_results(baseline, results, stamp):
    """Fold this run's scales into the previous file instead of replacing it.

    `python loadtest.py 10 50` used to write a two-scale file over a four-scale
    one, silently destroying the 250- and 1000-machine measurements -- the only
    evidence in the repo that latency grows with factory size even though query
    count does not. A scale this run did not touch keeps whatever the last run
    recorded for it, and `measured` says which run that was, so a merged file
    stays interpretable rather than merely complete.
    """
    results = {str(k): dict(v, measured=stamp) for k, v in results.items()}
    merged = {str(k): v for k, v in baseline.items()}
    merged.update(results)
    kept = sorted(set(merged) - set(results), key=int)
    return {k: merged[k] for k in sorted(merged, key=int)}, kept


def verdict(baseline, results):
    """State what the numbers mean, rather than leaving two columns to diff.

    The trap this exists to close: a run where EVERY endpoint is ~1.5x slower
    looks like a catastrophic regression and is usually just a busier laptop.
    The client floor and the MQTT loop do not touch AMP's request path, so when
    they move by the same factor as the endpoints, the machine moved -- not the
    application. Normalising by each run's own floor cancels that out.
    """
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    # In-process `results` is keyed by int; a baseline read back from JSON is
    # keyed by str. Left alone, the intersection below is silently empty and the
    # comparison reports "no baseline" on every run that actually has one.
    baseline = {str(k): v for k, v in baseline.items()}
    results = {str(k): v for k, v in results.items()}

    # --- did anything get measured at all? ---------------------------------
    near_floor = [(n, e["path"], e["p50"], r["floor_ms"])
                  for n, r in results.items() for e in r.get("http", [])
                  if r.get("floor_ms") and e["p50"] < r["floor_ms"] * FLOOR_SUSPECT]
    if near_floor:
        print(f"  NOT MEASURED: {len(near_floor)} endpoint(s) came in under "
              f"{FLOOR_SUSPECT}x the client floor. These describe the driver, "
              f"not the server:")
        for n, path, p50, floor in near_floor:
            print(f"    {n:>5} machines  {path:<32} "
                  f"p50 {p50:.1f} ms vs floor {floor:.1f} ms")
    else:
        print(f"  every endpoint came in above {FLOOR_SUSPECT}x the client floor, "
              f"so each figure is measuring the server.")

    # --- is p50 measuring the server, or the queue in front of it? ---------
    # Little's Law: with C clients kept busy against a saturated server,
    # latency = C / throughput. When a measured p50 matches C/RPS, that p50 is
    # very nearly ALL queueing -- it is what the LAST of C simultaneous callers
    # waits, not what one user waits. The service time 1000/RPS is the latter,
    # and the two differ by most exactly where the numbers look worst.
    print()
    queued = []
    for n, r in sorted(results.items(), key=lambda kv: int(kv[0])):
        c = r.get("concurrency") or CONCURRENCY
        for e in r.get("http", []):
            if not e.get("rps"):
                continue
            predicted = 1000.0 * c / e["rps"]
            if predicted and e["p50"] / predicted >= 1 - QUEUE_TOL:
                queued.append((n, e["path"], e["p50"], 1000.0 / e["rps"], c))
    if queued:
        print(f"  SATURATED -- these p50s are queueing, not service time. The "
              f"driver keeps {queued[0][4]} requests in flight at all times, so "
              f"p50 ~ {queued[0][4]}/RPS by Little's Law:")
        print(f"    {'':>5}          {'endpoint':<30}{'p50':>10}{'1 user waits':>14}")
        for n, path, p50, svc, c in sorted(queued, key=lambda q: -q[2])[:8]:
            print(f"    {n:>5} machines {path:<30}{p50:>9.1f}ms{svc:>13.1f}ms")
        print(f"  Quote the p50 for CAPACITY (what {queued[0][4]} concurrent "
              f"callers see) and the service time for LATENCY (what one user "
              f"sees). They are not interchangeable.")
    else:
        print(f"  no endpoint was saturated: every p50 is comfortably below "
              f"C/RPS, so these latencies are service time, not queueing.")

    # --- does the factory's SIZE cost anything? ----------------------------
    scales = sorted(results, key=int)
    if len(scales) > 1:
        lo, hi = scales[0], scales[-1]
        base = {e["path"]: e["p50"] for e in results[lo].get("http", [])}
        grew = []
        for e in results[hi].get("http", []):
            was = base.get(e["path"])
            if was and e["p50"] / was >= 2.0:
                grew.append((e["path"], was, e["p50"], e["p50"] / was))
        print()
        # A merged file can hold scales from different runs on different days.
        # Comparing across them re-introduces exactly the machine-drift error
        # the normalisation above removes, so say so rather than imply the
        # multiplier is clean.
        when_lo = results[lo].get("measured")
        when_hi = results[hi].get("measured")
        mixed = when_lo and when_hi and when_lo != when_hi
        if grew:
            print(f"  SCALES WITH FACTORY SIZE ({lo} -> {hi} machines), worst first:")
            if mixed:
                print(f"    CAUTION: {lo} was measured {when_lo} and {hi} "
                      f"{when_hi}. Different runs, so these multipliers carry "
                      f"the drift between two machines as well as the growth. "
                      f"Re-run both scales together for a clean figure.")
            for path, was, now, mult in sorted(grew, key=lambda g: -g[3]):
                print(f"    {path:<34} {was:>7.1f} -> {now:>7.1f} ms   {mult:>5.1f}x")
            print(f"  Query COUNT is flat at every size (dashboard_perf.py), so this")
            print(f"  is per-ROW work inside a constant number of queries -- not N+1.")
        else:
            print(f"  NONE of the endpoints doubled between {lo} and {hi} machines.")

    # --- has AMP itself got slower since the last committed run? -----------
    shared = sorted(set(baseline) & set(results), key=int)
    print()
    if not shared:
        print("  no comparable baseline in loadtest_results.json; nothing to compare.")
        return
    for n in shared:
        old, new = baseline[n], results[n]
        of, nf = old.get("floor_ms"), new.get("floor_ms")
        if not of or not nf:
            continue
        drift = nf / of
        prev = {e["path"]: e for e in old.get("http", [])}
        rows = []
        for e in new.get("http", []):
            p = prev.get(e["path"])
            if p and p["p50"]:
                rows.append((e["path"], (e["p50"] / nf) / (p["p50"] / of),
                             e["p50"] / p["p50"]))
        regressed = [r for r in rows if r[1] > 1 + NOISE]
        raw = statistics.median([r[2] for r in rows]) if rows else 1.0
        print(f"  {n} machines vs the committed baseline")
        print(f"    machine drift  : client floor {of} -> {nf} ms  ({drift:.2f}x)")
        print(f"    raw p50 change : {raw:.2f}x median -- BEFORE accounting for that")
        if regressed:
            print(f"    REGRESSED (floor-normalised, beyond +{NOISE:.0%}):")
            for path, norm, _ in sorted(regressed, key=lambda r: -r[1]):
                print(f"      {path:<32} {norm:.2f}x")
        else:
            print(f"    NO REGRESSION: floor-normalised, every endpoint is within "
                  f"+/-{NOISE:.0%}.")
            if raw > 1 + NOISE:
                print(f"      The raw {raw:.2f}x is the MACHINE being slower, not AMP.")


def main():
    import pg_scratch
    scales = [int(a) for a in sys.argv[1:] if a.isdigit()] or SCALES
    baseline = load_baseline()
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
            # The docstring promises "every result is reported against that
            # floor". The xfloor column is that promise kept: p50 as a multiple
            # of this run's own client overhead, which is the only figure that
            # survives being compared across machines or across days.
            # `svc` is 1000/RPS: the server's SERVICE time, what one user waits
            # with nobody ahead of them. `p50` is measured under CONCURRENCY
            # threads all hammering at once, so it also contains queueing. The
            # two differ by up to 8x here and confusing them turns a 68 ms
            # endpoint into a 575 ms panic. See the verdict's saturation check.
            print(f"  {'endpoint':<28}{'reqs':>7}{'rps':>8}{'svc':>8}{'p50':>9}"
                  f"{'p95':>9}{'p99':>9}{'err':>6}{'xfloor':>8}")
            for path, count, rps, p50, p95, p99, errs, codes in rows:
                print(f"  {path:<28}{count:>7}{rps:>8.1f}"
                      f"{(1000.0 / rps if rps else 0):>8.1f}{p50:>9.1f}"
                      f"{p95:>9.1f}{p99:>9.1f}{errs:>6}"
                      f"{(p50 / floor if floor else 0):>7.1f}x")
            results[n] = {"floor_ms": round(floor, 1),
                          "concurrency": CONCURRENCY,
                          "http": [{"path": p, "reqs": c, "rps": round(r, 1),
                                    "service_ms": round(1000.0 / r, 1) if r else None,
                                    "p50": round(a, 1), "p95": round(b, 1),
                                    "p99": round(d, 1), "errors": e,
                                    "xfloor": round(a / floor, 1) if floor else None,
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

    verdict(baseline, results)

    # MERGE, do not replace -- see merge_results(). Stamp with a TIME, not just
    # a date: two runs an hour apart on a differently-loaded machine produce
    # genuinely incomparable scales, and a date-only stamp makes them look like
    # one run to the mixed-provenance check in verdict().
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    merged, kept = merge_results(baseline, results, stamp)
    out = os.path.join(HERE, "loadtest_results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
    print(f"\nraw results -> {out}")
    if kept:
        print(f"  scales not re-run, kept from the previous file: "
              f"{', '.join(kept)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
