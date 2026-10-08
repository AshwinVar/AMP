"""Stand up an injection-moulding workspace: machines, moulds, parts and prices.

WHY THIS IS NOT A ONE-CUSTOMER SCRIPT. AMP is a multi-tenant platform and a
tenant is never special-cased in its code (that rule is what keeps the product
generic). So this takes the plant as ARGUMENTS and the part master as a CSV: the
shape of an injection-moulding plant is the same everywhere — a row of presses,
a mould on each, a part per mould, a weight and a price per part — and only the
numbers differ. The first plant it was written for supplied its own part table;
that table is data passed in, not a branch in here.

WHAT IT CREATES
    a tenant                   CompanyTenant + TenantConfig
    an Admin login             password from SEED_PASSWORD, never a default
    N machines                 named by --prefix, e.g. IMM-01 .. IMM-14
    a mould per part           ToolAsset, fitted to a machine in order
    a part spec per part       weight, cavities, cycle time, price, from-date

WHAT IT DELIBERATELY DOES NOT CREATE. Production records. The point of this
workspace is to show what a real machine reports, and seeded output would be
indistinguishable on screen from the real thing — the first question anyone asks
of a chart is "is that us?", and it must always be yes. `--demo-production` is
available for a rehearsal on a laptop with no machine attached, and it is OFF by
default and labels every machine it touches.

IDEMPOTENT. Re-running updates the plant in place rather than duplicating it, so
it is safe to re-run after correcting a weight or a price. Part specs are
effective-dated: a corrected price supersedes rather than overwrites, which is
why a second run with a different price does not restate yesterday.

    SEED_PASSWORD=... python backend/seed_moulding_plant.py \
        --tenant SHRINIDHI --name "Shrinidhi Plastics" \
        --machines 14 --parts parts.csv

The CSV columns are the part master's own fields, with the header:
    part_code,part_name,material,part_weight_g,cavities,active_cavities,
    ideal_cycle_time_s,price_per_piece
"""
import argparse
import csv
import io
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import models                                    # noqa: E402
from currency import money                       # noqa: E402
from database import Base, SessionLocal, engine  # noqa: E402
from security import hash_password               # noqa: E402
from tenancy import assert_tenant_code_available  # noqa: E402


class Refused(Exception):
    """A refusal with a sentence a person can act on."""


def _password():
    pw = os.environ.get("SEED_PASSWORD", "").strip()
    if len(pw) < 8:
        raise Refused(
            "set SEED_PASSWORD (8+ characters) before seeding. This script ships "
            "no default password: a real customer workspace reachable from the "
            "internet with a guessable login is a real breach, not a demo.")
    return pw


def _float(row, field, where, *, allow_zero=False):
    raw = (row.get(field) or "").strip()
    if not raw:
        if allow_zero:
            return 0.0
        raise Refused(f"{where}: {field} is empty. Every part needs one.")
    try:
        value = float(raw)
    except ValueError:
        raise Refused(f"{where}: {field} is {raw!r}, which is not a number.")
    if value != value or value in (float("inf"), float("-inf")):
        raise Refused(f"{where}: {field} is not a finite number.")
    if value < 0 or (value == 0 and not allow_zero):
        raise Refused(
            f"{where}: {field} is {value}. A weight or cycle time of zero makes "
            f"every figure derived from it meaningless, so it is refused here "
            f"rather than discovered on a chart.")
    return value


def read_parts(path):
    """The part master, validated before a single row is written.

    The same refusals the admin screen makes, applied at the door, because a CSV
    is the easiest way to enter fifty wrong numbers at once.
    """
    with io.open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise Refused(f"{path} has a header and no parts.")

    parts, seen = [], set()
    for i, row in enumerate(rows, start=2):          # row 1 is the header
        where = f"{os.path.basename(path)} line {i}"
        code = (row.get("part_code") or "").strip()
        if not code:
            raise Refused(f"{where}: part_code is empty.")
        if code in seen:
            raise Refused(
                f"{where}: part_code {code!r} appears twice. One row per part — "
                f"a price change is a new effective date, not a second row.")
        seen.add(code)

        cavities = int(_float(row, "cavities", where))
        active_raw = (row.get("active_cavities") or "").strip()
        active = int(float(active_raw)) if active_raw else cavities
        if active <= 0:
            raise Refused(f"{where}: active_cavities must be at least 1.")
        if active > cavities:
            raise Refused(
                f"{where}: active_cavities ({active}) exceeds cavities "
                f"({cavities}). A mould cannot run more cavities than it has, "
                f"and this typo overstates output for the life of the tool.")

        parts.append({
            "part_code": code,
            "part_name": (row.get("part_name") or code).strip(),
            "material": (row.get("material") or "").strip(),
            "part_weight_g": _float(row, "part_weight_g", where),
            "cavities": cavities,
            "active_cavities": active,
            "ideal_cycle_time_s": _float(row, "ideal_cycle_time_s", where),
            # Zero is allowed and means unpriced: a plant may track output before
            # it agrees a price, and the board reports those parts as unpriced.
            "price_per_piece": _float(row, "price_per_piece", where, allow_zero=True),
        })
        if not parts[-1]["material"]:
            raise Refused(f"{where}: material is empty. It is what kilograms are "
                          f"reported against.")
    return parts


def demo_prefix(prefix, demo_production):
    """The machine-name prefix a run should use.

    A rehearsal plant is named as one, and the name is decided BEFORE any machine
    exists so a re-run matches the same machines. This used to be a rename
    applied after seeding, which broke idempotency: the machine name is the key
    this seeder matches on, so "IMM-01" becoming "IMM-01 (DEMO)" made the next
    run build a second IMM-01.
    """
    if demo_production and "DEMO" not in prefix.upper():
        return "DEMO-" + prefix
    return prefix


def seed(db, *, tenant, name, machines, prefix, parts, effective_from,
         demo_production=False):
    assert_tenant_code_available(tenant)          # ADR-0017 reserved namespace
    pw = _password()

    # ── The workspace ────────────────────────────────────────────────
    company = (db.query(models.CompanyTenant)
                 .filter(models.CompanyTenant.company_code == tenant).first())
    if company is None:
        company = models.CompanyTenant(company_code=tenant, company_name=name,
                                       industry="Injection moulding")
        db.add(company)
    else:
        company.company_name = name
    if not db.query(models.TenantConfig).filter(
            models.TenantConfig.tenant_code == tenant).first():
        db.add(models.TenantConfig(tenant_code=tenant))

    admin = tenant.lower() + "-admin"
    user = db.query(models.User).filter(models.User.username == admin).first()
    if user is None:
        db.add(models.User(username=admin, password=hash_password(pw),
                           role="Admin", tenant_code=tenant, is_active=True))
    else:
        user.password = hash_password(pw)
        user.tenant_code = tenant
        user.is_active = True
    db.flush()

    # ── The presses ──────────────────────────────────────────────────
    existing = {m.name: m for m in db.query(models.Machine)
                .filter(models.Machine.tenant_code == tenant).all()}
    presses = []
    for n in range(1, machines + 1):
        mname = f"{prefix}-{n:02d}"
        m = existing.get(mname)
        if m is None:
            m = models.Machine(tenant_code=tenant, site="", name=mname,
                               status="Idle", line="Moulding")
            db.add(m)
        presses.append(m)
    db.flush()

    # ── The part master, effective-dated ─────────────────────────────
    for p in parts:
        already = (db.query(models.PartSpec)
                     .filter(models.PartSpec.tenant_code == tenant,
                             models.PartSpec.part_code == p["part_code"],
                             models.PartSpec.effective_from == effective_from)
                     .first())
        if already is not None:
            # Same part, same date: correct it in place. A DIFFERENT date makes a
            # new row, which is how a price change is recorded.
            for field, value in p.items():
                setattr(already, field, value)
            continue
        db.add(models.PartSpec(tenant_code=tenant, effective_from=effective_from, **p))

    # ── A mould per part, fitted to a press in order ─────────────────
    tools = {t.tool_no: t for t in db.query(models.ToolAsset)
             .filter(models.ToolAsset.tenant_code == tenant).all()}
    for i, p in enumerate(parts):
        tool_no = "MLD-" + p["part_code"]
        press = presses[i] if i < len(presses) else None
        t = tools.get(tool_no)
        if t is None:
            t = models.ToolAsset(tenant_code=tenant, tool_no=tool_no,
                                 name=p["part_name"] + " mould", status="Active")
            db.add(t)
        t.cavities = p["active_cavities"]
        t.part_code = p["part_code"]
        t.machine_id = press.id if press is not None else None

    if demo_production:
        _demo_production(db, tenant, presses, parts)

    db.commit()
    return {"machines": len(presses), "parts": len(parts),
            "moulds": len(parts), "admin": admin}


def _demo_production(db, tenant, presses, parts):
    """A rehearsal day, for a laptop with no machine attached.

    OFF by default and never mistaken for real: a rehearsal run names its
    machines DEMO-... from the start (see main()), so every chart drawn from this
    is labelled on screen by the machine it names. A seeded figure that looks
    exactly like a measured one is how a demo becomes a lie nobody meant to tell.

    THE LABEL IS IN THE NAME, NOT APPLIED AFTERWARDS. This function used to
    rename each machine to "IMM-01 (DEMO)". The machine name is its key here, so
    the next run could not find it and built a second IMM-01 -- four machines
    became six. A marker that mutates identity is not a marker.
    """
    today = datetime.combine(date.today(), datetime.min.time())
    for press, part in zip(presses, parts):
        press.status = "Running"
        per_hour = int((3600.0 / part["ideal_cycle_time_s"]) * part["active_cavities"])
        for hour in range(8, 18):
            # Deliberately uneven: a flat line looks synthetic, and the board's
            # whole job is to make a bad hour visible next to a good one.
            factor = 0.55 if hour in (12, 13) else 0.95
            made = int(per_hour * factor)
            db.add(models.ProductionRecord(
                tenant_code=tenant, machine_id=press.id,
                total_count=made, good_count=int(made * 0.98),
                rejected_count=made - int(made * 0.98),
                planned_minutes=60, runtime_minutes=int(60 * factor),
                ideal_cycle_time_seconds=int(part["ideal_cycle_time_s"]),
                created_at=today + timedelta(hours=hour)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", required=True, help="workspace code, e.g. SHRINIDHI")
    ap.add_argument("--name", required=True, help="company name as it should read")
    ap.add_argument("--machines", type=int, required=True, help="how many presses")
    ap.add_argument("--prefix", default="IMM", help="machine name prefix (default IMM)")
    ap.add_argument("--parts", required=True, help="part-master CSV")
    ap.add_argument("--from", dest="effective_from", default="",
                    help="date the specs take effect (default today)")
    ap.add_argument("--demo-production", action="store_true",
                    help="also write a rehearsal day; marks machines (DEMO)")
    args = ap.parse_args(argv)

    if args.machines < 1:
        raise Refused("--machines must be at least 1.")
    effective = (datetime.strptime(args.effective_from, "%Y-%m-%d").date()
                 if args.effective_from else date.today())

    parts = read_parts(args.parts)

    prefix = demo_prefix(args.prefix, args.demo_production)

    if len(parts) > args.machines:
        print(f"note: {len(parts)} parts for {args.machines} machines — the first "
              f"{args.machines} moulds are fitted and the rest stay in the tool room.")

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        out = seed(db, tenant=args.tenant, name=args.name, machines=args.machines,
                   prefix=prefix, parts=parts, effective_from=effective,
                   demo_production=args.demo_production)
    finally:
        db.close()

    print(f"\n{args.name} ({args.tenant}) is ready:")
    print(f"  {out['machines']} machines, {out['moulds']} moulds, {out['parts']} parts")
    print(f"  admin login: {out['admin']}  (password: the SEED_PASSWORD you set)")
    rated = [p for p in parts if p["price_per_piece"] > 0]
    if rated:
        best = max(rated, key=lambda p: (3600.0 / p["ideal_cycle_time_s"])
                   * p["active_cavities"] * p["price_per_piece"])
        rate = (3600.0 / best["ideal_cycle_time_s"]) * best["active_cavities"] \
            * best["price_per_piece"]
        print(f"  highest-value part: {best['part_name']} at "
              f"{money(round(rate))}/hour at full rate")
    unpriced = [p["part_code"] for p in parts if p["price_per_piece"] <= 0]
    if unpriced:
        print(f"  unpriced, so counted but not valued: {', '.join(unpriced)}")
    print("\n  Power and packing have no source and will read 'not measured' "
          "until a meter and a packing count exist.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refused as e:
        print(f"REFUSED: {e}")
        sys.exit(2)
