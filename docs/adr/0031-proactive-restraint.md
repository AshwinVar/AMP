# ADR-0031: AMP speaks first, and mostly decides not to

**Status:** accepted · **Date:** 2026-09-20 · **Extends** [ADR-0024](0024-factory-command-centre.md) (ranked problems), [ADR-0026](0026-production-risk-radar.md) (likelihood words), [ADR-0029](0029-closed-loop-outcomes.md) (what changed after an action)

---

## Context

AMP can tell an owner what is wrong, why, what it is likely to cost and whether
the last fix helped. **All of it waits to be opened.** Proactive intelligence
means AMP speaks first.

The moment a product does that, its real problem stops being *"can it detect
things"* and becomes *"will anyone still be reading it in a month"*. Every MES
and monitoring tool in this market has the same graveyard: a notification list
nobody opens, because everything was urgent once. An alert that trains a plant
to ignore the next one is worse than no alert.

AMP already had a notification generator (`/notifications/generate-system-notifications`).
It fires on raw conditions — a machine in Breakdown, stock below a level — and
de-duplicates by title against anything not yet marked Read. So once someone
reads a notification, the identical one can be created again immediately, and
nothing bounds how many appear in a run.

## Decision

`ai/proactive.py` is written the other way round from an alert rule. It decides
what to **suppress**, and it reports that decision.

### 1. The bar, printed on the card

Three things interrupt a person:

- a machine **stopped right now** (the Command Centre's first rank);
- a risk the Radar calls **LIKELY** — never POSSIBLE, never WATCH;
- an approved action whose metric came back **WORSE** (ADR-0029), because a
  recommendation that made things worse is the one thing AMP should raise
  without being asked.

Everything else is on the dashboard, where it belongs. The bar is a string in
the payload and is rendered on the card: a threshold nobody can see is a
threshold nobody can argue with.

### 2. Nothing is silently dropped

Every finding the engines raise appears in either `qualified` or `suppressed`,
and each suppression carries one of three published reasons:

| Reason | Means |
|---|---|
| `SAID RECENTLY` | the same **signature** within the cooldown (24h) |
| `BELOW THE BAR` | a real finding that does not meet the rules above |
| `OVER THE CAP` | more than 5 cleared the bar in one run |

The counts reconcile — `qualified + suppressed == considered` — and the test
fails if they do not.

### 3. A signature, so a problem keeps its identity

`risk:stock.RM-STEEL` is stable across runs, so the same problem cannot
re-announce itself every few minutes. It rides in
`Notification.notification_type` as `proactive:<signature>`, and the cooldown
reads `created_at`. **No new table and no migration**: both columns already
exist.

### 4. Reading and sending are separate

`GET /proactive` computes and writes nothing. `POST /proactive/send`
(Admin/Supervisor) is the only thing that writes, and is idempotent within the
cooldown by construction — running it twice in a row sends nothing the second
time.

A `GET` that notified people would notify them every time a browser polled it.

## Consequences

**Positive.**

- The restraint is inspectable. "Why didn't you tell me?" has an answer on the
  screen, with the rule beside it.
- The proactive path reuses the ranking, likelihood and outcome work rather than
  inventing a fourth severity scheme.
- No schema change, so nothing to migrate and nothing to roll back.

**Negative.**

- The bar is deliberately narrow, so genuinely interesting things sit on the
  dashboard unannounced. That is the trade being made, not an oversight.
- The cooldown is per signature, so a problem that resolves and returns inside
  24 hours is raised once. A plant that wants the second one has to open the
  card.

## Honest limits

- **AMP has no scheduler for real tenants.** `POST /proactive/send` has to be
  called — by a person on the card today, by a job when one exists. Nothing
  here fires by itself, and the card does not pretend otherwise.
- **Notifications are in-app only.** No email, no SMS, no push. "Proactive"
  here means AMP decides and records what it would say; delivery beyond the app
  is not built, and this ADR does not claim it.
- **The thresholds are judgement**, like every other threshold in this sprint:
  24 hours, 5 per run, and the three qualifying rules. They are constants in one
  place, printed on the card, and changeable in one edit.
- **It cannot raise what the engines cannot see.** A problem nobody records is
  not suppressed — it was never considered.
