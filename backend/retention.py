"""Data retention — bounded growth for the append-only tables.

WHY THIS EXISTS
Several tables in this schema are append-only and nothing has ever removed a row
from them. ``analytics_routes.py`` says so out loud about the worst offender:
"iot_telemetry is an unbounded, never-pruned growing table". In production the
in-process simulator (``main._simulation_loop``) ticks every 45 seconds and each
tick appends telemetry, so the table grows by roughly two thousand rows a day per
animated tenant whether or not anyone is looking at the dashboard.
``event_log``, ``machine_events``, ``industrial_signals``, ``audit_logs``,
``notifications`` and ``inventory_transactions`` grow the same way, without limit.

The damage is not only disk. Every one of these tables is polled by a read-model
on a ~30 second refresh, and the indexes added in ``main.py`` only keep the
*windowed* reads fast — the lifetime aggregates (``/analytics/iot-command``'s
signal total, ``/analytics/industrial-iot``, the machine-state GROUP BY) scan the
whole table and get slower every week. There has been no retention job of any
kind, so the only way this ends is a full disk or a query timeout.

WHAT THIS MODULE IS
A declarative retention policy table plus a batched, dry-by-default pruner. It is
an OPERATIONAL job, not a request handler: it runs from the CLI (or a scheduler)
against every tenant at once. Nothing here is wired into the API — see the module
CLI at the bottom.

Deliberately conservative:
  * Dry run by default. ``--apply`` is the only way to delete anything.
  * Compliance-sensitive tables (``audit_logs``, ``event_log``) are EXEMPT, and
    ``--days`` cannot resurrect them — see the policy table for the argument.
  * Deletes are batched and committed per batch, so a first run against a table
    that has been growing for a year cannot hold one enormous transaction or take
    a long lock.
  * A NULL timestamp is never treated as "old" (see ``_expired``).

Pure standard library plus SQLAlchemy, which the backend already depends on. No
new dependency.

    python backend/retention.py                      # dry run (report only)
    python backend/retention.py --apply              # execute (after approval)
    python backend/retention.py --days 30            # override every window
    python backend/retention.py --table iot_telemetry --apply
"""
import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, inspect as sa_inspect

import models
import tenancy
from database import SessionLocal

# Sentinel for "this table is never pruned". Spelled out rather than a bare None
# at the call sites so a policy line reads as a decision, not an omission.
KEEP_FOREVER = None

# Rows per delete batch. Each batch is its own transaction (see _prune_one), so
# this is the largest lock the job ever takes. 1000 is small enough that even a
# first run against a multi-million-row telemetry table stays interactive, and
# large enough that the per-batch round trip is not the bottleneck.
DEFAULT_BATCH_SIZE = 1000


@dataclass(frozen=True)
class RetentionPolicy:
    """One table's retention rule.

    ``timestamp_column`` is named per policy because these columns are NOT
    uniformly ``created_at`` — ``event_log`` carries both ``occurred_at`` (the
    domain time, and the indexed one) and ``created_at`` (the write time), and
    other tables in this schema use ``started_at`` or ``planned_date``. Guessing
    the column name is how a retention job silently prunes on the wrong clock.

    ``days`` is KEEP_FOREVER for exempt tables. ``why`` is not decoration: it is
    the review artefact. Anyone changing a number here has to change the argument
    with it.
    """
    model: type
    timestamp_column: str
    days: Optional[int]
    why: str


# ── The policy table ───────────────────────────────────────────────────────
# Ordered loosely by how fast each table grows. Only these tables are ever
# touched; a table absent from this list is untouched by definition, which is why
# the list is short and every line is argued.
POLICIES = (
    RetentionPolicy(
        models.IoTTelemetry, "created_at", 14,
        # The fastest-growing table in the schema: one row per signal per 45s sim
        # tick, forever. Everything that reads it reads a SHORT window —
        # ai/connectivity uses LOOKBACK_HOURS = 24, and /analytics/iot-command
        # shows the last 300 rows — so 14 days is already a fortnight of headroom
        # over the longest consumer. Two knock-on effects, accepted deliberately:
        # (1) /analytics/iot-command's "Signals" KPI is a lifetime COUNT, so it
        # becomes "signals retained" rather than "signals ever seen"; that is a
        # more honest number for a live command centre anyway. (2) connectivity's
        # DARK-vs-STALE probe asks whether a machine EVER reported; a machine
        # silent for more than 14 days will flip from STALE to DARK once its last
        # row ages out. Both are cosmetic next to the table growing without end.
        "highest-volume table (every sim tick); longest consumer window is 24h",
    ),
    RetentionPolicy(
        models.IndustrialSignal, "created_at", 14,
        # The PLC-side twin of iot_telemetry, written by industrial_adapters on
        # the same 45s tick. Same consumers (ai/connectivity, 24h lookback) and
        # the same display-only lifetime count on /analytics/industrial-iot, so it
        # gets the same window. Kept as a separate policy line rather than folded
        # in, because the two tables have independent write paths and could
        # justifiably diverge later.
        "PLC signal history on the same tick; longest consumer window is 24h",
    ),
    RetentionPolicy(
        models.MachineEvent, "created_at", 180,
        # The status-change timeline. Read by ai/prediction over
        # RISK_WINDOW_DAYS = 30 and displayed as the last 200 events, so 30 days
        # would technically suffice — but /analytics/machine-state-summary
        # deliberately tallies status counts over the WHOLE history, and this is
        # the closest thing the platform has to a machine's service record. Six
        # months keeps a meaningful seasonal comparison and still bounds the
        # table, which is the point.
        "machine service record; 6x the 30-day predictive window",
    ),
    RetentionPolicy(
        models.Notification, "created_at", 90,
        # Operational chatter with a short useful life: the UI lists the newest
        # 500 and the only aggregate is an unread count. A notification nobody
        # opened in a quarter is not going to be opened. Long enough that a
        # seasonal user still finds last quarter's alerts.
        "transient operator messages; UI reads the newest 500 only",
    ),
    RetentionPolicy(
        models.InventoryTransaction, "created_at", 1095,
        # The stock ledger — every issue, receipt and adjustment. NOT log noise:
        # ai/trace reconstructs a work order's material history from it with NO
        # time filter, and it is the only in-schema record of why a stock figure
        # is what it is, so it carries real financial and traceability weight.
        # Three years is a deliberate compromise: long enough to cover typical
        # UK record-keeping expectations and any plausible traceability question,
        # bounded enough that the table cannot grow for the platform's lifetime.
        # If a tenant needs longer, this is the line to raise — do not shorten it
        # to make the numbers look better.
        "stock ledger with financial and traceability weight; 3 years",
    ),
    RetentionPolicy(
        models.AIRecommendation, "created_at", 365,
        # The AI advice log. A recommendation is a claim the platform made about
        # the plant on a given day, so it is not disposable chatter: a year lets
        # you ask "did the maintenance agent warn us before that failure?" — the
        # single most valuable question anyone asks of an AI system after an
        # incident, and the one you cannot answer if you pruned the evidence.
        # Beyond a year the thresholds that produced it have usually moved and
        # the row is archaeology rather than evidence.
        "AI advice log; a year of 'did it warn us?' evidence",
    ),
    RetentionPolicy(
        models.AgentAction, "decided_at", 365,
        # The agent oversight trail (ADR-0005): what an agent proposed, who
        # approved or rejected it, and when. Pruned on decided_at, NOT created_at
        # — an action still sitting Proposed in someone's approval queue has no
        # decision date, so a NULL timestamp keeps it forever by the module's
        # own NULL rule. That is the correct behaviour and it falls out of the
        # design rather than needing a special case: an undecided proposal is
        # pending work, and a retention job must never quietly clear a queue.
        "agent oversight trail; undecided proposals never expire (NULL decided_at)",
    ),
    RetentionPolicy(
        models.ReportRequest, "created_at", 180,
        # Who asked for which report, when. The reports themselves are generated
        # on demand and never stored, so this is a request log, not an archive —
        # six months is ample to see usage patterns and to answer a billing or
        # access question.
        "report request log; the reports themselves are never stored",
    ),
    RetentionPolicy(
        models.AuditLog, "created_at", KEEP_FOREVER,
        # EXEMPT — the security audit trail. It exists precisely to answer "who
        # did what, when" long after the fact, usually about an incident nobody
        # knew was an incident at the time; a retention window on it is an
        # attacker's cleanup script with a schedule. offboard_tenant.py already
        # treats it as immutable history and refuses to purge it even when a
        # tenant leaves, and its tenant_code is NULLABLE by design (system-context
        # rows stay NULL and hidden), so pruning it would also destroy the exact
        # rows the approved backfill in backfill_enterprise_tenants.py is waiting
        # to resolve. If a real legal retention limit ever lands, it belongs here
        # as a reviewed change with the statute named — not as a --days flag.
        "security audit trail — deliberately unbounded; see offboard_tenant",
    ),
    RetentionPolicy(
        models.EventLog, "occurred_at", KEEP_FOREVER,
        # EXEMPT — the append-only domain event log (ADR-0001). It is the
        # substrate the analytics, AI and digital twin are rebuilt from, and
        # main.py's single-shot reseed guard reads it to decide whether a
        # RESEED_FACTORY flag has already been consumed: pruning old FactoryReseeded
        # rows would let a stale flag wipe production a second time, which is the
        # 2026-07-18 incident that guard was written for. Note the timestamp
        # column is occurred_at, not created_at.
        "event-sourced history (ADR-0001) + reseed-flag guard depend on all of it",
    ),
)


def table_names():
    """Every table the policy table covers, in policy order."""
    return [p.model.__tablename__ for p in POLICIES]


def _pk_column(model):
    """The model's single primary-key attribute. Batching chunks by primary key,
    so the pruner needs the column rather than assuming ``id`` — every model here
    happens to use ``id``, but reading it from the mapper means a future table
    with a different key does not silently prune in one giant batch.

    Returns the MAPPED attribute (``Model.id``), not the raw Table column: an ORM
    attribute keeps every query the pruner builds a genuine ORM query, so the
    counts, the key scan and the delete all behave identically with respect to
    the ADR-0002 scoping hook rather than some being core statements it ignores.
    """
    mapper = sa_inspect(model)
    return mapper.get_property_by_column(mapper.primary_key[0]).class_attribute


def _tenant_column_nullable(model):
    """True when the model's ``tenant_code`` can be NULL — i.e. it can hold rows
    that NO tenant scope can see. See the tenancy note in ``prune``."""
    column = model.__table__.columns.get("tenant_code")
    return bool(column is not None and column.nullable)


def _expired(ts_column, cutoff):
    """The "old enough to delete" predicate, in one place so the count and the
    delete can never drift apart.

    ``ts < cutoff`` is NULL-unknown in SQL, so a row with a NULL timestamp is not
    matched and is therefore never deleted. That is the intended behaviour, not
    an accident of three-valued logic: a NULL timestamp means the row's age is
    UNKNOWN, not infinite. Every model here defaults its timestamp in Python, so
    a NULL can only come from a raw-SQL insert or an old migration — exactly the
    rows whose provenance is least understood, and the worst possible candidates
    for silent deletion. The report counts them separately so an operator can see
    they exist and fix them at the source.
    """
    return ts_column < cutoff


def _blank_entry(policy, retention_days):
    return {
        "table": policy.model.__tablename__,
        "retention_days": retention_days,
        "exempt": policy.days is KEEP_FOREVER,
        "cutoff": None,
        "total": 0,
        "expired": 0,
        "null_timestamps": 0,
        "deleted": 0,
        "batches": 0,
        "refused": None,
        "error": None,
    }


def _prune_one(db, policy, dry_run, days, batch_size, now):
    """Report (and optionally delete) one table's expired rows. Never raises: a
    failure on one table is recorded and the sweep continues, because a nightly
    job that aborts halfway is a job nobody trusts to leave running."""
    # An exempt table is reported (its size is worth knowing) and never touched.
    # The --days override is applied to policy.days ONLY for tables that already
    # have a window: an exempt table stays exempt whatever the operator types, so
    # a mistyped flag cannot delete the audit trail. Changing that requires
    # editing the policy table above, which is a reviewed change.
    if policy.days is KEEP_FOREVER:
        entry = _blank_entry(policy, KEEP_FOREVER)
        try:
            entry["total"] = db.query(func.count(_pk_column(policy.model))).scalar() or 0
        except Exception as e:      # a missing table must not fail the sweep
            db.rollback()
            entry["error"] = str(e)
        return entry

    retention_days = policy.days if days is None else days
    entry = _blank_entry(policy, retention_days)

    # Safety guard, defence in depth behind the policy table: refuse to prune a
    # model whose tenant_code is nullable. Such a table can hold rows that no
    # tenant scope can see (tenancy.py parks ambiguous legacy rows as NULL on
    # purpose), and this job — which runs unscoped — would be the one thing able
    # to delete them. If a future edit gives audit_logs a window, this stops it.
    if _tenant_column_nullable(policy.model):
        entry["refused"] = ("nullable tenant_code — this table can hold rows hidden "
                            "from every tenant scope; prune it only via a reviewed policy change")
        return entry

    cutoff = now - timedelta(days=retention_days)
    entry["cutoff"] = cutoff
    model = policy.model
    ts = getattr(model, policy.timestamp_column)
    pk = _pk_column(model)

    try:
        # Counted before any delete so the dry-run report and the apply run answer
        # the same question. `total` costs a full COUNT; on a nightly job against a
        # table this far behind, knowing "900k of 1M" versus "900k of 900k" is
        # worth the scan.
        entry["total"] = db.query(func.count(pk)).scalar() or 0
        entry["null_timestamps"] = db.query(func.count(pk)).filter(ts.is_(None)).scalar() or 0
        entry["expired"] = db.query(func.count(pk)).filter(_expired(ts, cutoff)).scalar() or 0

        if dry_run:
            return entry

        # Batched delete. Each pass selects at most `batch_size` primary keys and
        # deletes exactly those, then COMMITS. Three properties matter:
        #   * no single enormous transaction, and no long lock, on a first run
        #     against a table that has never been pruned;
        #   * the job is resumable — an interrupted run leaves the batches it
        #     already committed deleted, and the next run carries on;
        #   * the DELETE repeats the retention predicate alongside the key list,
        #     so even a stale key list cannot reach a row inside the window.
        # Every table in the policy list is an FK child (telemetry, events,
        # signals, notifications, transactions); nothing references them, so these
        # deletes cannot orphan a parent row.
        while True:
            ids = [row[0] for row in
                   db.query(pk).filter(_expired(ts, cutoff)).order_by(pk).limit(batch_size).all()]
            if not ids:
                break
            removed = (db.query(model)
                       .filter(pk.in_(ids), _expired(ts, cutoff))
                       .delete(synchronize_session=False))
            db.commit()
            entry["deleted"] += removed
            entry["batches"] += 1
            if len(ids) < batch_size:
                break
    except Exception as e:
        db.rollback()
        entry["error"] = str(e)
    return entry


def prune(db, dry_run=True, days=None, batch_size=DEFAULT_BATCH_SIZE, now=None, tables=None):
    """Apply the retention policy across every policy table.

    Returns ``{table_name: report}``. With ``dry_run=True`` (the default) nothing
    is deleted and the report says what WOULD go. ``days`` overrides the window
    for every non-exempt table; ``tables`` restricts the sweep to named tables.

    TENANCY (this is an operational job, not a request)
    Retention is deliberately tenant-AGNOSTIC: one nightly run has to bound the
    tables for every tenant, and there is no caller whose JWT it could scope to.
    The interaction with ADR-0002 auto-scoping needs care, because that hook is
    SELECT-only:

      * ``tenancy.install_scoping`` adds ``tenant_code = :tenant`` to SELECTs via
        ``with_loader_criteria`` whenever a tenant is bound. It does NOT touch a
        bulk DELETE. So a prune that ran with a tenant accidentally still bound
        would COUNT one tenant's expired rows and — if it used a bulk delete on a
        bare predicate — remove every tenant's. The report and the deletion would
        disagree about scope, which is the worst possible failure for a job whose
        entire safety story is "the dry run told you what would happen".
      * So ``prune`` explicitly unbinds the tenant for its whole duration. Both
        the count and the delete then see all tenants, and they agree. Under the
        CLI nothing is bound anyway; this matters if prune is ever called from an
        admin endpoint or a scheduler that inherits a request context.
      * Running unscoped means the job CAN see rows no tenant can — most
        importantly the NULL-``tenant_code`` rows that tenancy.py parks on purpose
        so an ambiguous legacy row is hidden rather than misassigned. Deleting
        those would destroy the evidence the approved backfill needs. Rather than
        rely on the policy table staying correct, ``_prune_one`` REFUSES any model
        with a nullable ``tenant_code``. Every prunable table here is
        ``tenant_code NOT NULL DEFAULT 'DEFAULT'``; the nullable ones (audit_logs
        and the enterprise-inventory domain) are exempt or simply not listed.
      * Deletion is always by explicit primary key, taken from the same predicate
        that produced the count — never a broad ``DELETE ... WHERE ts < cutoff``.
    """
    if days is not None and days < 0:
        raise ValueError("days must be >= 0")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    now = now or datetime.utcnow()
    wanted = set(tables) if tables else None

    # Unbind any inherited tenant for the duration — see the TENANCY note above.
    scope = tenancy.set_current_tenant(None)
    try:
        report = {}
        for policy in POLICIES:
            name = policy.model.__tablename__
            if wanted is not None and name not in wanted:
                continue
            report[name] = _prune_one(db, policy, dry_run, days, batch_size, now)
        return report
    finally:
        tenancy.reset_current_tenant(scope)


# ── CLI (approval-gated, same shape as backfill_enterprise_tenants.py) ─────

def format_entry(entry):
    """One report line, human-readable. Public because a scheduler that calls
    ``prune`` directly (rather than through this CLI) needs the same wording in
    its logs — two renderings of the same report is how they drift."""
    if entry["error"]:
        return f"ERROR — {entry['error']}"
    if entry["refused"]:
        return f"REFUSED — {entry['refused']}"
    if entry["exempt"]:
        return f"EXEMPT (keep forever) — {entry['total']:,} rows, none removed"
    line = (f"keep {entry['retention_days']}d (cutoff {entry['cutoff']:%Y-%m-%d %H:%M} UTC) — "
            f"{entry['expired']:,} of {entry['total']:,} rows expired")
    if entry["null_timestamps"]:
        line += f"; {entry['null_timestamps']:,} with a NULL timestamp KEPT (unknown age)"
    if entry["batches"]:
        line += f"; deleted {entry['deleted']:,} in {entry['batches']} batch(es)"
    return line


def main(apply=False, days=None, batch_size=DEFAULT_BATCH_SIZE, tables=None):
    db = SessionLocal()
    try:
        report = prune(db, dry_run=not apply, days=days, batch_size=batch_size, tables=tables)
        print("=== data retention plan ===")
        for name, entry in report.items():
            print(f"  {name}:")
            print(f"      {format_entry(entry)}")
        expired = sum(e["expired"] for e in report.values())
        deleted = sum(e["deleted"] for e in report.values())
        problems = [n for n, e in report.items() if e["error"] or e["refused"]]
        print(f"  TOTAL: {expired:,} rows past their window across "
              f"{len(report)} table(s)")
        if problems:
            print(f"  ATTENTION: {', '.join(problems)} were not processed (see above)")
        if not apply:
            print("DRY RUN — nothing deleted. Re-run with --apply once approved.")
        else:
            print(f"APPLIED — {deleted:,} rows deleted.")
        return 1 if any(e["error"] for e in report.values()) else 0
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prune the append-only tables to their retention windows. "
                    "Dry run unless --apply is given.")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: report only)")
    parser.add_argument("--days", type=int, default=None,
                        help="override the retention window for every non-exempt "
                             "table; exempt tables stay exempt")
    parser.add_argument("--table", action="append", dest="tables", metavar="NAME",
                        help=f"restrict to a table (repeatable). One of: {', '.join(table_names())}")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"rows deleted per transaction (default {DEFAULT_BATCH_SIZE})")
    args = parser.parse_args()
    sys.exit(main(apply=args.apply, days=args.days,
                  batch_size=args.batch_size, tables=args.tables))
