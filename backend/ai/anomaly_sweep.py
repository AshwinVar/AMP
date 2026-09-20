"""The anomaly check, across the whole fleet, with a reason for every machine (ADR-0032).

AMP has had a telemetry anomaly check since ADR-0020, and until now the only way
to use it was to open a model card, pick one machine from a dropdown and press a
button. A plant with forty machines has forty dropdown choices and no reason to
believe it has looked at all of them.

This runs the SAME check over every machine the tenant owns and returns one row
per machine — a score, or the reason there is no score:

    scored          a number, and the evaluation's own caveats attached to it
    NOT MEASURED    nothing to score: no telemetry, or not enough history yet
    NOT CONFIGURED  the model artifact is unavailable

WHAT IT IS NOT. The anomaly model is EXPERIMENTAL and was not adopted (ADR-0020:
it did not beat the existing rules on held-out synthetic data). So:

  * the result state is always MODEL NOT VALIDATED;
  * a score is `MODEL ESTIMATE` provenance, never a measurement;
  * nothing here is styled or worded as an alarm, and the payload may not
    contain "alarm", "alert" or "fault" — the test fails on those words;
  * it does NOT feed the proactive bar (ADR-0031). A model that has not earned
    adoption has not earned the right to interrupt anyone.

CONSENT IS CHECKED ONCE, NOT PER MACHINE. The check learns a baseline from the
company's own telemetry, so it runs only with that company's consent
(ADR-0020). Without it the sweep returns no scores at all and says so — it does
not silently return an empty list, which would read as "nothing unusual".

A FOUNDER PREVIEW NEVER RUNS IT, for the same reason a preview cannot give the
consent: the consent covers the company's own Admins and Supervisors.
"""
from datetime import datetime

import models
from ai import evidence as ev
from amp_ai.core.contracts import ConsentRequired

name = "anomaly_sweep"

MAX_MACHINES = 60
NOT_ADOPTED = ("This check is experimental: on held-out synthetic data it did not beat the rules AMP "
               "already uses, so it is shown as an estimate and never as an alarm.")
NO_CONSENT = ("Learning from telemetry is off for this company, so AMP fitted no baselines and scored "
              "nothing. An Admin can turn it on under AI learning consent.")
PREVIEW = ("The anomaly check learns from this company's own telemetry, and its consent covers only its "
           "own Admins and Supervisors, so it does not run from a platform preview.")


def _fact(key, label, value, prov, unit="", source="", window="last hour", detail=""):
    return ev.Fact(key=key, label=label, value=value, provenance=prov, unit=unit,
                   source=source, window=window, detail=detail).to_dict(key)


def _row(machine, result):
    """One machine's row: a score, or the reason there is not one."""
    status = (result or {}).get("status")
    score = (result or {}).get("score")
    base = {"machine_id": machine.id, "name": machine.name, "line": machine.line or "",
            "score": None, "state": ev.NOT_MEASURED, "reason": None, "facts": []}
    if status == "insufficient_history":
        needed, have = result.get("needed") or {}, result.get("have") or {}
        short = next((k for k in needed if k in have and have[k] < needed[k]), None)
        base["state"] = ev.INSUFFICIENT_HISTORY
        base["reason"] = (f"not enough history yet (have {have[short]} of {needed[short]} {short})"
                          if short else "not enough history yet")
        return base
    if status != "ok" or score is None:
        base["state"] = ev.NOT_CONFIGURED if status == "model_unavailable" else ev.NOT_MEASURED
        base["reason"] = (result or {}).get("reason") or "nothing to score in this window"
        return base
    base.update({
        "score": round(float(score), 2),
        # MODEL NOT VALIDATED is the state of every scored row, because the model
        # is experimental. A row that said OK would be claiming an adoption the
        # evaluation refused.
        "state": ev.MODEL_NOT_VALIDATED,
        "reason": None,
        "facts": [
            _fact(f"anomaly.{machine.id}.score", f"{machine.name}: anomaly score",
                  round(float(score), 2), ev.MODEL, "", "telemetry baseline",
                  detail=NOT_ADOPTED),
        ],
    })
    return base


def build_anomaly_sweep(db, tenant: str, *, scorer, gate, previewing=False, now=None) -> dict:
    """Score every machine this tenant owns, or say why not.

    `scorer(db, tenant, machine_id, gate=...)` is the SAME function the
    single-machine route calls, passed in so this module runs no model of its
    own and cannot drift from it.
    """
    at = now or datetime.utcnow()
    if previewing:
        return {"generated_at": at.isoformat(), "state": ev.NOT_MEASURED, "headline": PREVIEW,
                "machines": [], "scored": 0, "not_scored": 0, "consent": False,
                "note": NOT_ADOPTED}

    machines = (db.query(models.Machine)
                .filter(models.Machine.tenant_code == tenant)
                .order_by(models.Machine.id).limit(MAX_MACHINES).all())
    rows, consent_ok = [], True
    for m in machines:
        try:
            rows.append(_row(m, scorer(db, tenant, m.id, gate=gate)))
        except ConsentRequired:
            # The consent refusal is the whole FLEET's answer, not one machine's:
            # the consent is per company, so a second machine would refuse for
            # the same reason. Stop and say it once.
            consent_ok = False
            break
        except Exception:                            # noqa: BLE001 - a refusal is a result
            rows.append({"machine_id": m.id, "name": m.name, "line": m.line or "", "score": None,
                         "state": ev.NOT_MEASURED,
                         "reason": "this machine could not be scored", "facts": []})
    if not consent_ok:
        return {"generated_at": at.isoformat(), "state": ev.NOT_CONFIGURED, "headline": NO_CONSENT,
                "machines": [], "scored": 0, "not_scored": len(machines), "consent": False,
                "note": NOT_ADOPTED}

    scored = [r for r in rows if r["score"] is not None]
    # Highest score first, then by machine, so the order is stable.
    scored.sort(key=lambda r: (-r["score"], r["machine_id"]))
    unscored = [r for r in rows if r["score"] is None]
    if not machines:
        state, headline = ev.NO_DATA, "There are no machines to check."
    elif not scored:
        state = ev.INSUFFICIENT_HISTORY
        headline = (f"None of {len(machines)} machines could be scored yet. Every one says why "
                    "below; none of them is being reported as normal.")
    else:
        state = ev.MODEL_NOT_VALIDATED
        top = scored[0]
        headline = (f"{len(scored)} of {len(machines)} machines scored. The highest is {top['name']} "
                    f"at {top['score']}. This is an estimate from an experimental check, not an alarm.")
    return {
        "generated_at": at.isoformat(),
        "state": state,
        "headline": headline,
        # Scored first, then everything AMP could not score — never dropped,
        # because an absent machine reads as a machine that was fine.
        "machines": scored + unscored,
        "scored": len(scored),
        "not_scored": len(unscored),
        "consent": True,
        "note": NOT_ADOPTED,
    }


def say_anomaly_sweep(sweep) -> tuple:
    """One sentence for the Copilot, with the model's status in it."""
    if sweep["state"] in (ev.NOT_CONFIGURED, ev.NOT_MEASURED) and not sweep["machines"]:
        return sweep["headline"], "machines"
    return f"{sweep['headline']} {NOT_ADOPTED}", "machines"
