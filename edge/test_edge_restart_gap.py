"""The restart gap: a hole in the history, flagged instead of drawn across.

The queue is the one thing that survives a restart, so it is the only place that
can notice the gateway was gone. The poll adapters count edges in RAM and take
their first post-restart reading as a baseline, so the cycles a machine ran while
the gateway was down are lost with nothing to mark them -- the board would draw a
straight line across the hole. These tests pin the line between an ordinary
bounce (a deploy, ignored) and a real absence (a power cut, flagged with the
window it spans), and that the marker fires on exactly one record.

Run: python edge/test_edge_restart_gap.py
"""
import os
import sys
import tempfile

from ampedge import buffer as buffer_mod

FAILURES = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"   [{detail}]"))
    if not ok:
        FAILURES.append(name)


def _path():
    return os.path.join(tempfile.mkdtemp(), "q.db")


def test_a_real_absence_arms_a_gap_with_its_window():
    path = _path()
    b = buffer_mod.Buffer(path)
    b.mark_alive(now=1000.0)            # last seen alive at t=1000
    b.close()
    # Reopen -- a restart -- 300s later, well past the 120s threshold.
    b2 = buffer_mod.Buffer(path)
    armed = b2.note_restart(now=1300.0, min_gap_s=120.0)
    check("a 300s absence past the threshold arms a gap over [1000, 1300]",
          armed == (1000.0, 1300.0), str(armed))
    b2.put({"machine": "M1", "total_count": 5})
    rec = b2.peek(1)[0][3]
    check("...and the first record after it carries gap_before + the window",
          rec.get("gap_before") is True and rec.get("gap_from") == 1000.0
          and rec.get("gap_to") == 1300.0,
          str({k: rec.get(k) for k in ("gap_before", "gap_from", "gap_to")}))


def test_an_ordinary_bounce_arms_nothing():
    path = _path()
    b = buffer_mod.Buffer(path)
    b.mark_alive(now=1000.0)
    b.close()
    b2 = buffer_mod.Buffer(path)
    armed = b2.note_restart(now=1030.0, min_gap_s=120.0)   # 30s -- a deploy
    b2.put({"machine": "M1", "total_count": 1})
    rec = b2.peek(1)[0][3]
    check("a 30s bounce arms nothing and the record is clean",
          armed is None and "gap_before" not in rec,
          f"{armed} {rec.get('gap_before')}")


def test_a_brand_new_queue_arms_nothing():
    path = _path()
    b = buffer_mod.Buffer(path)
    armed = b.note_restart(now=1000.0, min_gap_s=120.0)    # never ran before
    b.put({"machine": "M1", "total_count": 1})
    rec = b.peek(1)[0][3]
    check("a brand-new queue (no prior alive_at) never flags a gap",
          armed is None and "gap_before" not in rec, str(armed))


def test_the_window_is_cleared_after_one_record():
    path = _path()
    b = buffer_mod.Buffer(path)
    b.mark_alive(now=1000.0)
    b.close()
    b2 = buffer_mod.Buffer(path)
    b2.note_restart(now=1300.0, min_gap_s=120.0)
    b2.put({"machine": "M1", "total_count": 1})     # carries the gap
    b2.put({"machine": "M1", "total_count": 2})     # must be clean
    recs = [r[3] for r in b2.peek(10)]
    first_marked = recs[0].get("gap_before") is True
    second_clean = "gap_before" not in recs[1] and "gap_from" not in recs[1]
    check("the marker fires on exactly one record, not every record after",
          first_marked and second_clean,
          str([r.get("gap_before") for r in recs]))


if __name__ == "__main__":
    test_a_real_absence_arms_a_gap_with_its_window()
    test_an_ordinary_bounce_arms_nothing()
    test_a_brand_new_queue_arms_nothing()
    test_the_window_is_cleared_after_one_record()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print("restart-gap OK")
