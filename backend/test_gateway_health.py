"""Gateway health: which half is broken, and never a guess about the other.

WHY THIS SUITE EXISTS
---------------------
Three faults draw the same empty chart: the press is idle, the press's contact
is unplugged from the gateway, and the Raspberry Pi in the panel is off. The
customer has fourteen presses and one unattended Pi, so the second and third are
otherwise discovered the day after, when the figures come up short.

WHAT IS PINNED
  * a gateway that is reporting + a machine that is not  -> the MACHINE is at
    fault, named, amber
  * a gateway that is NOT reporting -> every machine under it is UNKNOWN, never
    "silent". AMP cannot see them at all, and saying "IMM-07 has stopped" would
    be asserting something it has no evidence for
  * a gateway registered but never heard from is GREY, not red: nothing is
    broken, it was never commissioned, and sending somebody to a panel to look
    at a Pi that was never installed is how an alert stops being believed
  * power or network is NOT claimed live — a Pi with no power and a Pi off the
    WiFi produce identical evidence here (nothing), so the sentence names all
    three possibilities and promises the answer when it returns
  * ...and once it returns, the answer IS given, from the edge's own restart
    gap, and it survives the machine going green again
  * the banner and the worst light are one decision: the first version computed
    them separately and said "all machines are reporting" under a red badge
  * the silence threshold is shared with the notification that fires on it
  * another workspace's gateway is not this workspace's problem

Run: DATABASE_URL="sqlite:///./ci_gwhealth.db" python backend/test_gateway_health.py
"""
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_gwhealth.db")

import database   # noqa: E402
import models     # noqa: E402
from ai import gateway_health as gh   # noqa: E402

failures = []
T = "GWHEALTH-TEST"
OTHER = "GWHEALTH-OTHER"
NOW = datetime(2026, 10, 13, 12, 0)


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t):
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def wipe(db):
    for M in (models.ProductionRecord, models.MachineTelemetrySpan,
              models.GatewayCredential, models.Notification, models.Machine):
        for tenant in (T, OTHER):
            db.query(M).filter(M.tenant_code == tenant).delete()
    db.commit()


def machine(db, name, tenant=T, site=""):
    m = models.Machine(tenant_code=tenant, site=site, name=name, status="Running")
    db.add(m)
    db.flush()
    return m


def gateway(db, seen_minutes_ago, tenant=T, label="Shrinidhi Pi", site=""):
    db.add(models.GatewayCredential(
        tenant_code=tenant, site=site, gateway_id=f"gw-{label.lower().replace(' ', '-')}",
        secret="x", label=label, is_active=True,
        last_seen_at=None if seen_minutes_ago is None else NOW - timedelta(minutes=seen_minutes_ago)))
    db.commit()


def heard(db, m, minutes_ago, tenant=T):
    db.add(models.ProductionRecord(
        tenant_code=tenant, machine_id=m.id, total_count=10, good_count=10,
        rejected_count=0, planned_minutes=5, runtime_minutes=5,
        ideal_cycle_time_seconds=14, created_at=NOW - timedelta(minutes=minutes_ago)))
    db.commit()


def by_name(out):
    return {r["machine"]: r for r in out["machines"]}


def build(db, tenant=T):
    return gh.build_gateway_health(db, tenant, now=NOW)


def main():   # noqa: C901 - one section per pinned property, read top to bottom
    models.Base.metadata.create_all(bind=database.engine)
    db = database.SessionLocal()
    try:
        section("1. EVERYTHING REPORTING IS GREEN, AND SAYS SO ONCE")
        wipe(db)
        a, b = machine(db, "IMM-01"), machine(db, "IMM-02")
        gateway(db, 2)
        heard(db, a, 3)
        heard(db, b, 4)
        out = build(db)
        check("every machine is reporting", out["reporting"] == 2 and out["total"] == 2)
        check("the plant light is green",
              out["worst"] == gh.REPORTING and out["light"] == "green", out["worst"])
        check("the banner says so", "All 2 machines are reporting" in out["headline"],
              out["headline"])

        section("2. THE GATEWAY IS FINE, ONE MACHINE'S LINK IS NOT")
        db.query(models.ProductionRecord).filter(
            models.ProductionRecord.tenant_code == T,
            models.ProductionRecord.machine_id == b.id).delete()
        db.commit()
        heard(db, b, 40)
        out = build(db)
        rows = by_name(out)
        check("the reporting machine stays green", rows["IMM-01"]["light"] == "green")
        check("the silent one is amber, and named as the MACHINE's fault",
              rows["IMM-02"]["state"] == gh.MACHINE_SILENT
              and rows["IMM-02"]["light"] == "amber", str(rows["IMM-02"]["state"]))
        check("...the reason says the gateway is fine and the link is not",
              "gateway is reporting" in rows["IMM-02"]["reason"]
              and "own link is not" in rows["IMM-02"]["reason"], rows["IMM-02"]["reason"])
        check("...and the fix points at the machine, not the Pi",
              "contact wiring" in rows["IMM-02"]["fix"], rows["IMM-02"]["fix"])
        check("the plant light is amber", out["worst"] == gh.MACHINE_SILENT)

        section("3. A DEAD GATEWAY MAKES ITS MACHINES UNKNOWN, NEVER SILENT")
        # THE rule. While the Pi is unreachable AMP can see nothing behind it,
        # so "IMM-02 has stopped" is a claim with no evidence under it.
        db.query(models.GatewayCredential).filter(
            models.GatewayCredential.tenant_code == T).update(
            {"last_seen_at": NOW - timedelta(minutes=27)})
        db.commit()
        out = build(db)
        rows = by_name(out)
        check("every machine reads GATEWAY UNREACHABLE",
              all(r["state"] == gh.GATEWAY_DOWN for r in out["machines"]),
              str([(r["machine"], r["state"]) for r in out["machines"]]))
        check("...including the one that WAS silent on its own",
              rows["IMM-02"]["state"] == gh.GATEWAY_DOWN,
              "a machine was still blamed while the gateway was down")
        check("...and none of them is called silent",
              not any(r["state"] == gh.MACHINE_SILENT for r in out["machines"]))
        check("the plant light is red", out["worst"] == gh.GATEWAY_DOWN
              and out["light"] == "red", out["worst"])

        section("4. POWER OR NETWORK IS NOT GUESSED WHILE IT IS OUT")
        head = out["headline"]
        for claim in ("no power", "off the network", "amp-edge service"):
            check(f"the banner names '{claim}' as a possibility", claim in head, head)
        check("...and says AMP cannot tell which from here",
              "look the same" in head, head)
        check("...and promises the answer when it returns", "once it returns" in head, head)
        check("the fix sends somebody to check all three",
              "power" in rows["IMM-01"]["fix"] and "WiFi" in rows["IMM-01"]["fix"],
              rows["IMM-01"]["fix"])

        section("5. ONCE IT RETURNS, AMP SAYS WHICH IT WAS")
        # The edge heartbeats alive_at to disk while it runs, so a gateway that
        # kept power through a network outage returns with nothing missing, and
        # one that lost power returns having armed a gap. The gap is the
        # evidence; its absence after a silence means the counts survived.
        db.query(models.GatewayCredential).filter(
            models.GatewayCredential.tenant_code == T).update(
            {"last_seen_at": NOW - timedelta(minutes=1)})
        heard(db, b, 1)
        db.commit()
        out = build(db)
        check("a network-only outage leaves no cause to report",
              by_name(out)["IMM-01"]["resolved_cause"] is None,
              str(by_name(out)["IMM-01"]["resolved_cause"]))
        db.add(models.Notification(
            tenant_code=T, notification_type="Gateway", severity="Warning",
            title="Data gap at IMM-01", message="A break in the record.",
            status="Unread", created_at=NOW))
        db.commit()
        out = build(db)
        rows = by_name(out)
        check("a power loss DOES leave a cause",
              rows["IMM-01"]["resolved_cause"] is not None)
        check("...and it says the counts were never taken",
              "never taken" in (rows["IMM-01"]["resolved_cause"] or ""),
              str(rows["IMM-01"]["resolved_cause"]))
        # The question is asked AFTER the gateway comes back, so gating the
        # answer on the machine still being down means nobody ever reads it.
        check("the cause survives the machine going green again",
              rows["IMM-01"]["state"] == gh.REPORTING
              and rows["IMM-01"]["resolved_cause"] is not None,
              f"state={rows['IMM-01']['state']}")
        check("...and the banner carries it too",
              "never taken" in out["headline"], out["headline"])
        check("a machine with no gap of its own gets no cause",
              rows["IMM-02"]["resolved_cause"] is None)

        section("6. REGISTERED BUT NEVER COMMISSIONED IS GREY, NOT RED")
        wipe(db)
        machine(db, "IMM-09")
        gateway(db, None, label="New Pi")
        out = build(db)
        rows = by_name(out)
        check("the gateway is NEVER REPORTED, not unreachable",
              out["gateways"][0]["state"] == gh.NEVER, out["gateways"][0]["state"])
        check("the machine is grey, not red",
              rows["IMM-09"]["light"] == "grey"
              and rows["IMM-09"]["state"] == gh.NEVER, rows["IMM-09"]["light"])
        check("...and the fix is to commission it, not to go and look at it",
              "ommission" in rows["IMM-09"]["fix"], rows["IMM-09"]["fix"])
        check("the banner says it was never wired up",
              "never reported" in out["headline"], out["headline"])

        section("7. THE BANNER AND THE BADGE ARE ONE DECISION")
        # The first version computed the headline from the gateways and the
        # worst light from the rows, and printed "All 1 machines are reporting"
        # under a red badge.
        for label, setup in (
            ("a dead gateway", lambda: db.query(models.GatewayCredential).filter(
                models.GatewayCredential.tenant_code == T).update(
                {"last_seen_at": NOW - timedelta(minutes=99)})),
            ("a never-seen gateway", lambda: db.query(models.GatewayCredential).filter(
                models.GatewayCredential.tenant_code == T).update({"last_seen_at": None})),
        ):
            wipe(db)
            m = machine(db, "IMM-09")
            gateway(db, 1)
            heard(db, m, 1)
            setup()
            db.commit()
            out = build(db)
            contradiction = (out["worst"] != gh.REPORTING
                             and "machines are reporting" in out["headline"])
            check(f"{label}: the banner never contradicts the light",
                  not contradiction, f"{out['worst']} vs {out['headline']}")

        section("8. ANOTHER WORKSPACE'S GATEWAY IS NOT THIS ONE'S PROBLEM")
        # GatewayCredential sits outside the ADR-0002 scoping hook, so every
        # read of it filters tenant_code by hand; this is that filter's test.
        wipe(db)
        mine = machine(db, "IMM-01")
        gateway(db, 1, label="Mine")
        heard(db, mine, 1)
        theirs = machine(db, "IMM-01", tenant=OTHER)
        gateway(db, 999, tenant=OTHER, label="Theirs")
        heard(db, theirs, 999, tenant=OTHER)
        out = build(db)
        check("only this workspace's gateway is listed",
              [g["label"] for g in out["gateways"]] == ["Mine"],
              str([g["label"] for g in out["gateways"]]))
        check("...so their dead Pi does not turn this plant red",
              out["worst"] == gh.REPORTING, out["worst"])
        check("...and only this workspace's machines are counted", out["total"] == 1)
        other = build(db, tenant=OTHER)
        check("and the other workspace sees its own, correctly",
              other["worst"] == gh.GATEWAY_DOWN, other["worst"])

        section("9. THE THRESHOLD IS SHARED WITH THE ALERT THAT FIRES ON IT")
        import factory_ops_routes   # noqa: PLC0415
        check("the notification and the board read one constant",
              factory_ops_routes.GATEWAY_SILENCE is gh.GATEWAY_SILENCE,
              "two thresholds for one silence: the screen and the feed can disagree")
        check("...and it is fifteen minutes, three publish windows",
              gh.GATEWAY_SILENCE == timedelta(minutes=15), str(gh.GATEWAY_SILENCE))
        wipe(db)
    finally:
        db.close()


# ── Collected by pytest as well as run as a script ────────────────────
#
# CI's per-file runner (`python test_gateway_health.py`) is the contract and is
# unchanged. The separate coverage job collects every suite into ONE pytest
# process, and pytest only collects module-level `test_*` functions.
def test_everything():
    """The whole suite as one case, failing with whatever it recorded."""
    code = None
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code
    assert not failures, "\n  " + "\n  ".join(str(f) for f in failures)
    assert code in (0, None), f"the suite exited with {code}"


if __name__ == "__main__":
    main()
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - gateway health")
    print("=" * 74)
