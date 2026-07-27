"""AI-insights analytics endpoint tests (/analytics/ai-insights).

The endpoint reports a rollup of the ai_recommendations table: a total count plus
status buckets (open / acknowledged / closed) and severity buckets (critical /
high / medium / low). Things it must get right, pinned here (the route test only
covers registration):

  * The counts are aggregated in SQL (COUNT + GROUP BY), not by hydrating the whole
    growing ai_recommendations table into Python and bucketing it with list
    comprehensions (rule-4). The displayed numbers are unchanged; only the basis is.
  * `total` counts EVERY row, including one whose status/severity is NULL (both
    columns are nullable, default-only) or is a value outside the tracked bucket
    lists — those land in no bucket, so `total` stays >= the sum of either
    partition. The old Python loop kept exactly this property (len(rows) counted a
    NULL-status row that `None == "Open"` never bucketed); the GROUP BY form keeps
    it too (a NULL/other key is simply never .get()'d).
  * An empty table reports all-zero counts, never a crash.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_ai_insights_analytics.py
"""
import inspect

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from analytics_routes import get_ai_insights

USER = {"tenant": "DEFAULT"}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _rec(status="Open", severity="Medium", rtype="predictive_maintenance"):
    return models.AIRecommendation(
        tenant_code="DEFAULT", recommendation_type=rtype,
        title="t", message="m", status=status, severity=severity,
    )


def test_counts_by_status_and_severity():
    db = _fresh_session()
    # Six rows with independently-known (status, severity) so each bucket total is
    # derivable by hand, not read back from the endpoint:
    #   status   -> Open=3, Acknowledged=2, Closed=1        (sums to 6)
    #   severity -> Critical=1, High=2, Medium=2, Low=1     (sums to 6)
    db.add_all([
        _rec("Open", "Critical"),
        _rec("Open", "High"),
        _rec("Open", "Medium"),
        _rec("Acknowledged", "High"),
        _rec("Acknowledged", "Low"),
        _rec("Closed", "Medium"),
    ])
    db.commit()

    out = get_ai_insights(db=db, current_user=USER)

    assert out["total"] == 6, out
    assert out["open"] == 3, out
    assert out["acknowledged"] == 2, out
    assert out["closed"] == 1, out
    assert out["critical"] == 1, out
    assert out["high"] == 2, out
    assert out["medium"] == 2, out
    assert out["low"] == 1, out
    # both partitions reconcile with the headline total (no row lost, none double-counted)
    assert out["open"] + out["acknowledged"] + out["closed"] == out["total"], out
    assert out["critical"] + out["high"] + out["medium"] + out["low"] == out["total"], out
    print("PASS ai-insights: status (3/2/1) and severity (1/2/2/1) buckets reconcile to total=6")


def test_empty_table_all_zeros():
    db = _fresh_session()
    out = get_ai_insights(db=db, current_user=USER)
    assert out == {
        "total": 0, "open": 0, "acknowledged": 0, "closed": 0,
        "critical": 0, "high": 0, "medium": 0, "low": 0,
    }, out
    print("PASS ai-insights: empty table -> all-zero counts, no crash")


def test_null_and_untracked_counted_in_total_only():
    db = _fresh_session()
    # Two cleanly-bucketed rows...
    db.add_all([
        _rec("Open", "Critical"),
        _rec("Closed", "Low"),
    ])
    # ...one row whose status/severity are real but outside the tracked lists...
    db.add(_rec("Dismissed", "Info"))
    db.commit()
    # ...and one row with a genuine NULL status AND NULL severity, the "written by
    # raw SQL" case the ORM column defaults can't produce (they'd fill Open/Medium).
    db.execute(text(
        "INSERT INTO ai_recommendations "
        "(tenant_code, recommendation_type, title, message, status, severity) "
        "VALUES ('DEFAULT', 'x', 't', 'm', NULL, NULL)"
    ))
    db.commit()

    out = get_ai_insights(db=db, current_user=USER)

    # total counts all four rows, including the untracked and the NULL one.
    assert out["total"] == 4, out
    # buckets see only the two cleanly-labelled rows.
    assert out["open"] == 1, out
    assert out["closed"] == 1, out
    assert out["acknowledged"] == 0, out
    assert out["critical"] == 1, out
    assert out["low"] == 1, out
    assert out["high"] == 0, out
    assert out["medium"] == 0, out
    # the Dismissed/Info and NULL/NULL rows fall in no bucket, so each partition
    # sums to 2 while total is 4 (total >= partition sum — the reconciliation the
    # honest rollup must keep; a NULL/untracked row is neither dropped nor mislabelled).
    assert out["open"] + out["acknowledged"] + out["closed"] == 2, out
    assert out["critical"] + out["high"] + out["medium"] + out["low"] == 2, out
    print("PASS ai-insights: NULL + untracked rows counted in total only (buckets reconcile to 2 of 4)")


def test_no_full_table_hydrate():
    # Guard against a regression back to the whole-table scan: the endpoint must
    # aggregate in SQL, never `db.query(models.AIRecommendation).all()`.
    src = inspect.getsource(get_ai_insights)
    assert "db.query(models.AIRecommendation).all()" not in src, \
        "ai-insights must aggregate in SQL, not hydrate the whole growing table"
    assert "func.count" in src and "group_by" in src, \
        "expected SQL COUNT + GROUP BY aggregation"
    print("PASS ai-insights: aggregates in SQL (no full-table .all() hydrate)")


if __name__ == "__main__":
    test_counts_by_status_and_severity()
    test_empty_table_all_zeros()
    test_null_and_untracked_counted_in_total_only()
    test_no_full_table_hydrate()
    print("\nAll ai-insights analytics tests passed.")
