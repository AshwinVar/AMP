"""Mutation harness for the open-escalation rule (#565).

Each mutation restores one of the five spellings the fix collapsed, or weakens
the shared predicate. Every one MUST be caught by
test_open_escalation_one_rule.py — a mutation that survives is a rule the test
does not actually pin, and the spellings would be free to drift apart again.

Mutation 12 (the tenant adoption panel) SURVIVED the first run, and the reason
is worth keeping: that panel's own list already spelled "Cancelled" correctly,
so on a fixture whose statuses were all exact-cased it was equivalent to the
shared clause. What separates them is vocabulary drift — "canceled" with one l,
and surrounding whitespace — which the fixture did not contain. Two rows were
added to the fixture rather than accepting the mutation as equivalent.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/mutate_open_escalation.py
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(HERE, "test_open_escalation_one_rule.py")

# (label, file, find, replace)
MUTATIONS = [
    # ── The shared predicate itself ────────────────────────────────
    ("open_clause: drop 'cancelled' from the terminal set — a withdrawn "
     "escalation is open again",
     "ai/escalations.py",
     'CLOSED_STATUSES = ("resolved", "cancelled", "canceled", "closed")',
     'CLOSED_STATUSES = ("resolved", "canceled", "closed")'),
    ("open_clause: drop COALESCE — a NULL status stops being open",
     "ai/escalations.py",
     'return func.lower(func.trim(func.coalesce(models.Escalation.status, ""))).notin_(\n        CLOSED_STATUSES)',
     'return models.Escalation.status.notin_(CLOSED_STATUSES)'),
    ("open_clause: invert to a whitelist — the old ('Proposed','Open') rule",
     "ai/escalations.py",
     'return func.lower(func.trim(func.coalesce(models.Escalation.status, ""))).notin_(\n        CLOSED_STATUSES)',
     'return models.Escalation.status.in_(("Proposed", "Open"))'),

    # ── The call sites that adopted it ─────────────────────────────
    ("command centre: back to != 'Resolved'",
     "analytics_routes.py",
     '    open_escalations = db.query(func.count(models.Escalation.id)).filter(\n'
     '        ai.escalations.open_clause()\n'
     '    ).scalar() or 0\n\n    # low_stock',
     '    open_escalations = db.query(func.count(models.Escalation.id)).filter(\n'
     '        or_(models.Escalation.status.is_(None),\n'
     '            models.Escalation.status != "Resolved")\n'
     '    ).scalar() or 0\n\n    # low_stock'),
    ("system health: back to != 'Resolved'",
     "analytics_routes.py",
     '    open_escalations = db.query(func.count(models.Escalation.id)).filter(\n'
     '        ai.escalations.open_clause()\n'
     '    ).scalar() or 0\n    # "Unread"',
     '    open_escalations = db.query(func.count(models.Escalation.id)).filter(\n'
     '        or_(models.Escalation.status.is_(None),\n'
     '            models.Escalation.status != "Resolved")\n'
     '    ).scalar() or 0\n    # "Unread"'),
    ("smart-alert dedup: back to != 'Resolved' (the alert-gagging bug)",
     "core_routes.py",
     '                ai.escalations.open_clause(),',
     '                models.Escalation.status != "Resolved",'),
    ("escalation agent dedup: back to the ('Proposed','Open') whitelist",
     "ai/agents.py",
     '                escalations.open_clause())',
     '                models.Escalation.status.in_(("Proposed", "Open")))'),
    ("briefing dedup: back to the ('Proposed','Open') whitelist",
     "ai/agents.py",
     '                    escalations.open_clause(),',
     '                    models.Escalation.status.in_(("Proposed", "Open")),'),
    ("task dedup: back to the ('Proposed','Open') whitelist",
     "ai/agents.py",
     '            models.MaintenanceTask.status.in_(maintenance.OPEN_STATUSES),',
     '            models.MaintenanceTask.status.in_(("Proposed", "Open")),'),
    ("task dedup: drop 'In Progress' from the shared maintenance open set",
     "ai/maintenance.py",
     'OPEN_STATUSES = ("Proposed", "Open", "In Progress")',
     'OPEN_STATUSES = ("Proposed", "Open")'),

    ("system notifications: back to != 'Resolved' — alerting on withdrawn work",
     "factory_ops_routes.py",
     '        .filter(ai.escalations.open_clause())',
     '        .filter(or_(models.Escalation.status.is_(None),\n'
     '                    models.Escalation.status != "Resolved"))'),
    ("tenant adoption panel: back to its own case-sensitive terminal list",
     "saas_routes.py",
     '                                .filter(ai.escalations.open_clause()).count())',
     '                                .filter(models.Escalation.status.notin_(\n'
     '                                    ("Resolved", "Cancelled", "Closed"))).count())'),

    # ── The handover, which was already right and must stay pinned ──
    ("handover: back to a hand-rolled whitelist",
     "ai/handover.py",
     '                        escalations.open_clause()).count())',
     '                        models.Escalation.status.in_(\n'
     '                            ("Proposed", "Open", "In Progress"))).count())'),
]


def run_test():
    env = dict(os.environ, DATABASE_URL="sqlite:///./ci_mut_esc.db")
    r = subprocess.run([sys.executable, TEST], cwd=HERE, capture_output=True,
                       text=True, timeout=180, env=env)
    return r.returncode, (r.stdout + r.stderr)


def main():
    print("Baseline (unmutated) must PASS:")
    rc, out = run_test()
    if rc != 0:
        print(out[-2500:])
        print("BASELINE FAILS — fix the code before mutation testing.")
        return 1
    print("  PASS\n")

    caught, survived = 0, []
    for i, (label, rel, find, repl) in enumerate(MUTATIONS, 1):
        path = os.path.join(HERE, rel)
        # newline="" on BOTH read and write. Reading in text mode without it
        # converts CRLF to LF, and writing it back then rewrites every line
        # ending in the file — the restore is no longer byte-identical, and the
        # next `git status` shows a file this harness claims not to have changed.
        original = io.open(path, encoding="utf-8", newline="").read()
        if find not in original:
            survived.append(f"{label}  [PATTERN DID NOT APPLY — the mutation is "
                            f"disabled, which means this guard is unmeasured]")
            print(f"{i:2}. SURVIVED (pattern missing)  {label}")
            continue
        try:
            io.open(path, "w", encoding="utf-8", newline="").write(
                original.replace(find, repl, 1))
            rc, out = run_test()
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(original)
        if rc != 0:
            caught += 1
            fails = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")]
            print(f"{i:2}. caught     {label}")
            print(f"      -> {fails[0][:96] if fails else '(non-zero exit)'}")
        else:
            survived.append(label)
            print(f"{i:2}. SURVIVED   {label}")

    print()
    print("=" * 74)
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    for s in survived:
        print(f"  SURVIVED: {s}")
    print("=" * 74)
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
