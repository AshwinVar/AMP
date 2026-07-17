"""Production summary — a fleet-wide read-model over output (ADR-0007).

Answers "how much are we making, and how good is it?": over the last week it
rolls up units produced (good vs rejected), the good rate, the top producing
machines, and a daily throughput series. A read-model over production_records —
auto-scoped to the tenant by the query layer (ADR-0002); it adds no storage.
"""
from collections import defaultdict
from datetime import datetime, timedelta

import models

name = "production"

WINDOW_DAYS = 7
TOP_N = 5


def _pct(part, whole):
    return round(part / whole * 100) if whole else 0


def build_production_summary(db, tenant: str) -> dict:
    """Throughput and output quality over the last 7 days, plus the top
    producing machines and a daily good-count series. production_records and
    machines are auto-scoped (ADR-0002)."""
    today = datetime.utcnow().date()
    window = [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    window_set = set(window)
    records = [
        r for r in db.query(models.ProductionRecord).all()
        if r.created_at and r.created_at.date() in window_set
    ]

    total = sum(r.total_count or 0 for r in records)
    good = sum(r.good_count or 0 for r in records)
    rejected = sum(r.rejected_count or 0 for r in records)

    all_machines = db.query(models.Machine).all()
    names = {m.id: m.name for m in all_machines}
    line_of = {m.id: (m.line or "") for m in all_machines}

    per_machine: dict = defaultdict(int)
    line_good: dict = defaultdict(int)
    line_total: dict = defaultdict(int)
    for r in records:
        if r.machine_id is not None:
            per_machine[r.machine_id] += r.good_count or 0
        ln = line_of.get(r.machine_id, "")
        if ln:
            line_good[ln] += r.good_count or 0
            line_total[ln] += r.total_count or 0
    by_machine = sorted(
        ({"machine_id": mid, "name": names.get(mid, f"#{mid}"), "good": g} for mid, g in per_machine.items()),
        key=lambda m: m["good"], reverse=True,
    )[:TOP_N]

    per_day: dict = defaultdict(int)
    for r in records:
        per_day[r.created_at.date()] += r.good_count or 0
    daily = [{"date": d.isoformat(), "count": per_day.get(d, 0)} for d in window]

    by_line = [
        {"line": ln, "good": line_good[ln], "total": line_total[ln], "good_rate": _pct(line_good[ln], line_total[ln])}
        for ln in sorted(line_good)
    ]

    return {
        "days": WINDOW_DAYS,
        "runs": len(records),
        "total": total,
        "good": good,
        "rejected": rejected,
        "good_rate": _pct(good, total),
        "by_machine": by_machine,
        "by_line": by_line,
        "daily": daily,
    }
