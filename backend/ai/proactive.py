"""What is worth interrupting someone for — and what is not (ADR-0031).

AMP can already tell an owner what is wrong, why, what it is likely to cost and
whether the last fix helped. All of it waits to be opened. Proactive means AMP
speaks first — and the moment a product does that, its real problem stops being
"can it detect things" and becomes "will anyone still be reading it in a month".

So this module is written the other way round from a normal alert rule. It
decides what to SUPPRESS, and it reports that decision:

    considered   everything the engines raised this run
    qualified    the few that clear the bar for interrupting a person
    suppressed   the rest, each with the reason it was held back

THE BAR, stated rather than tuned in private:

  * a machine that is STOPPED RIGHT NOW (the Command Centre's first rank);
  * a risk the Radar calls LIKELY — never POSSIBLE, never WATCH;
  * an approved action whose metric came back WORSE (ADR-0029), because a
    recommendation that made things worse is the one thing AMP should say
    without being asked.

Everything else is on the dashboard, where it belongs.

THE THREE SUPPRESSIONS, also stated:

  * SAID RECENTLY   the same signature within the cooldown. A signature is
                    stable ("risk:stock.RM-STEEL"), so the same problem keeps
                    the same identity across runs and cannot re-announce itself
                    every few minutes.
  * BELOW THE BAR   a real finding that does not meet the rules above.
  * OVER THE CAP    more qualified findings than one message may carry. The
                    count is reported; nothing is silently dropped.

NO SCHEMA. The signature rides in `Notification.notification_type` as
"proactive:<signature>", and the cooldown reads `created_at`. Both columns
already exist, so this adds no table and no migration.

READING AND SENDING ARE SEPARATE. `build_proactive` computes and writes nothing;
`send_proactive` writes, and is the only thing that does. A GET that notified
people would be a GET that notifies people every time a browser polls it.
"""
from datetime import datetime, timedelta

import models
from ai import evidence as ev

name = "proactive"

COOLDOWN_HOURS = 24
MAX_PER_RUN = 5
TYPE_PREFIX = "proactive"

SAID_RECENTLY = "SAID RECENTLY"
BELOW_THE_BAR = "BELOW THE BAR"
OVER_THE_CAP = "OVER THE CAP"
SUPPRESSIONS = (SAID_RECENTLY, BELOW_THE_BAR, OVER_THE_CAP)

BAR = ("AMP interrupts for three things only: a machine stopped right now, a risk the radar calls "
       "LIKELY, and an approved action whose metric came back worse. Everything else is on the "
       "dashboard.")


def _candidate(signature, kind, severity, title, message, qualifies, why, view):
    return {"signature": signature, "kind": kind, "severity": severity, "title": title,
            "message": message, "qualifies": qualifies, "why": why, "view": view}


def _from_command_centre(cc):
    """A machine stopped RIGHT NOW is the only Command Centre problem that
    interrupts. The rest are ranked on the card by what they cost, which is a
    question for someone who has opened it."""
    out = []
    for p in (cc.get("problems") or []):
        live = p.get("rank_basis") == "stopped now"
        out.append(_candidate(
            f"problem:{p['key']}", "Machine" if live else "Problem",
            "Critical" if live else "Warning", p["title"], p["detail"], live,
            "a machine is stopped right now" if live
            else "ranked on the card, but not happening this second",
            p.get("view") or "overview"))
    return out


def _from_radar(radar):
    out = []
    for r in (radar.get("risks") or []):
        likely = r.get("likelihood") == ev.LIKELY
        out.append(_candidate(
            f"risk:{r['key']}", "Risk", "Warning" if likely else "Info",
            r["title"], f"{r['detail']} (rule: {r['rule']})", likely,
            "the radar calls this LIKELY" if likely
            else f"the radar calls this {r.get('likelihood')}, which does not interrupt",
            r.get("view") or "overview"))
    return out


def _from_outcomes(outcomes):
    """A recommendation that made the number WORSE. AMP asked for this action;
    it should be the one to raise it."""
    out = []
    for o in (outcomes.get("outcomes") or []):
        if o.get("waiting") or not o.get("verdict"):
            continue
        worse = o["verdict"] == "WORSE"
        where = f" on {o['scope_label']}" if o.get("scope_label") else ""
        out.append(_candidate(
            f"outcome:{o['id']}", "Outcome", "Warning" if worse else "Info",
            f"{o['metric_label']}{where} got worse after an approved action",
            (f"{o.get('action') or 'An approved action'}: {o['metric_label']} moved from "
             f"{o['baseline_value']} to {o['measured_value']} {o['unit']}. "
             f"{outcomes.get('note', '')}"),
            worse,
            "an action AMP recommended was followed by a worse number" if worse
            else f"the metric came back {o['verdict']}, which does not interrupt",
            "agentactivity"))
    return out


def _said_recently(db, tenant, now):
    """{signature: when} for everything AMP has already said inside the cooldown."""
    since = now - timedelta(hours=COOLDOWN_HOURS)
    rows = (db.query(models.Notification.notification_type, models.Notification.created_at)
            .filter(models.Notification.tenant_code == tenant,
                    models.Notification.notification_type.like(f"{TYPE_PREFIX}:%"),
                    models.Notification.created_at >= since)
            .all())
    seen = {}
    for kind, created in rows:
        signature = kind.split(":", 1)[1]
        if signature not in seen or (created and created > seen[signature]):
            seen[signature] = created
    return seen


def build_proactive(db, tenant: str, now=None) -> dict:
    """What AMP would say if it spoke now, and everything it would hold back.

    Writes nothing. `send_proactive` is the only thing that writes.
    """
    at = now or datetime.utcnow()
    from ai.command_centre import build_command_centre    # lazy: these compose the pillars
    from ai.outcomes import build_outcome_summary
    from ai.risk_radar import build_risk_radar

    candidates = (_from_command_centre(build_command_centre(db, tenant, now=at))
                  + _from_radar(build_risk_radar(db, tenant, now=at))
                  + _from_outcomes(build_outcome_summary(db, tenant, now=at)))
    recent = _said_recently(db, tenant, at)

    qualified, suppressed = [], []
    for c in candidates:
        if not c["qualifies"]:
            suppressed.append({**c, "suppressed_by": BELOW_THE_BAR})
        elif c["signature"] in recent:
            when = recent[c["signature"]]
            suppressed.append({**c, "suppressed_by": SAID_RECENTLY,
                               "why": (f"AMP said this within the last {COOLDOWN_HOURS} hours"
                                       + (f", at {when.isoformat()}" if when else ""))})
        else:
            qualified.append(c)

    # Critical first, then the order the engines ranked them in.
    qualified.sort(key=lambda c: 0 if c["severity"] == "Critical" else 1)
    over_cap = qualified[MAX_PER_RUN:]
    qualified = qualified[:MAX_PER_RUN]
    for c in over_cap:
        suppressed.append({**c, "suppressed_by": OVER_THE_CAP,
                           "why": f"more than {MAX_PER_RUN} things cleared the bar in one run"})

    if not candidates:
        # The STATE here is NO DATA, and the sentence has to agree with it.
        # "No problems and no risks" is a claim about the plant; NO DATA says
        # AMP had nothing to look at. A brand-new workspace reaches this branch
        # for exactly that reason, and it must not be told its plant is clear.
        # The quiet-but-instrumented plant is the `elif not qualified` branch
        # below, which can say so because something was actually considered.
        state, headline = ev.NO_DATA, ("Nothing reached AMP to consider raising. That is not the "
                                       "same as a clear plant: with nothing to look at, there is "
                                       "nothing to hold back either.")
    elif not qualified:
        state = ev.OK
        headline = (f"Nothing worth interrupting you for. {len(suppressed)} thing"
                    f"{'s' if len(suppressed) != 1 else ''} considered and held back.")
    else:
        state = ev.OK
        headline = (f"{len(qualified)} thing{'s' if len(qualified) != 1 else ''} worth telling you "
                    f"now; {len(suppressed)} held back.")
    return {
        "generated_at": at.isoformat(),
        "state": state,
        "headline": headline,
        "considered": len(candidates),
        "qualified": qualified,
        "suppressed": suppressed,
        "cooldown_hours": COOLDOWN_HOURS,
        "max_per_run": MAX_PER_RUN,
        # On the card, next to the counts, so the bar is never a private tuning.
        "bar": BAR,
    }


def send_proactive(db, tenant: str, now=None) -> dict:
    """Write the qualified notifications. The ONLY function here that writes.

    Idempotent within the cooldown by construction: a signature that was just
    written is `SAID RECENTLY` on the next call, so running this twice in a row
    sends nothing the second time.
    """
    at = now or datetime.utcnow()
    plan = build_proactive(db, tenant, now=at)
    for c in plan["qualified"]:
        db.add(models.Notification(
            tenant_code=tenant,
            notification_type=f"{TYPE_PREFIX}:{c['signature']}",
            severity=c["severity"], title=c["title"], message=c["message"], status="Unread",
            created_at=at))
    if plan["qualified"]:
        db.commit()
    return {"sent": len(plan["qualified"]), "held_back": len(plan["suppressed"]),
            "considered": plan["considered"],
            "titles": [c["title"] for c in plan["qualified"]],
            "bar": BAR}


def say_proactive(plan) -> tuple:
    """One sentence for the Copilot, with the restraint in it."""
    return f"{plan['headline']} {BAR}", "overview"
