# Factory demo runbook — the SMT → IC instrument-cluster plant

**Who this is for:** Ash, presenting to an SME manufacturer. Ten minutes.

**The story you are telling:** a two-line electronics plant is having a bad
week. The owner does not know *which* bad thing is costing the most. AMP reads
what the plant already records, ranks the problems by the output they actually
cost, prices them in the owner's own money, and — when asked — proposes the
one action to take, which a human approves.

**The rule for the whole demo:** everything on screen is real software reading a
real database. The only thing that is fictional is the factory. Say that once,
early, and then never claim anything the screen is not showing.

**Why you can rehearse this one.** The seed is deterministic
(`reset_factory.DEMO_SEED`). Every figure below is the same on every reset, so
the numbers you practise on are the numbers the prospect sees. Until 2026-09-22
they were not: the seeder drew fresh random production, downtime, reject rates
and order dates each run, so "the biggest measured loss" was a different
sentence every time. `test_reset_factory.py::test_the_demo_is_reproducible`
fails the build if that ever comes back.

---

## BEFORE THE MEETING

Reset the plant. Locally:

```bash
cd C:\Users\ashwi\AMP\backend && python reset_factory.py
```

On production, set `RESEED_FACTORY` to a **value you have not used before** and
redeploy. The flag is single-shot — each value is consumed exactly once and
recorded in the append-only event log — because a forgotten flag once wiped
production on every deploy (~41 times, 2026-07-18). Never reuse a value, and
never leave it set expecting nothing to happen.

Then check the headline reads exactly this:

> Plant OEE 80%, measured from 6 of 8 machines; 92% of the plan due was made;
> 1 machine down right now (SMT-Reflow-01). Biggest measured loss: Reflow
> profile fault stopped production 6 times (£9,725).

If it does not, the reset did not run.

---

## THE PLANT

Two lines, eight machines. A part enters RAW, is surface-mounted on the SMT
line (→ SEMI), then assembled, programmed, tested and packed on the IC line
(→ FIN). Ten work orders, five for Bugatti and five for Mercedes.

| Line | Machines |
|---|---|
| SMT | Printer-01, PickPlace-01, **Reflow-01 (in breakdown)**, AOI-01 |
| IC | Assembly-01, Programming-01, Test-01, **FinalQC-01 (in maintenance)** |

A good unit is worth **£12.50** here. That figure is the tenant's own
configuration, not a rate hidden inside an engine — which is why money appears
at all. On a workspace that has not set one, every figure below would read in
units and AMP would say so rather than invent a price.

---

## THE SEVEN PLANTED PROBLEMS

Each one is an ordinary row in an ordinary table. Nothing tells AMP where to
look; every surface below finds them by its own rules.

| # | Planted | Where AMP finds it |
|---|---|---|
| 1 | SMT-Reflow-01 is in breakdown | Command Centre, top row — and it outranks everything sized, because a machine stopped *now* is still stopping |
| 2 | Four long stops on that oven under one reason | "Reflow profile fault stopped production 6 times" — **778 units, £9,725** |
| 3 | Two shift plans came up short | "PLAN-3102 on SMT-PickPlace-01 is behind" — **330 units, £4,125** |
| 4 | The EOL tester rejecting at ~18% on one defect | "188 units failed inspection (Solder defect)" — **188 units, £2,350** |
| 5 | Three stock items at or below reorder level | Inventory: **3 at risk**; the Copilot names the nozzle first |
| 6 | CO-5002 (Bugatti) two days past its date | Risk Radar: **LIKELY — CO-5002 is already past its date** |
| 7 | MT-9001 planned three days ago, still open | Risk Radar: **LIKELY — 1 maintenance task already overdue** |

The Command Centre shows the top five, ranked by measured impact. Items 5–7 sit
below that cut *because AMP cannot size them in units* — it says so rather than
inventing a number — and they surface on the Risk Radar and their own screens.

---

## THE TEN MINUTES

### 1. Command Centre (2 min) — "understand the plant in 30 seconds"

Open the dashboard. Read the headline out loud, then the five ranked problems:

```
1. 1 machine down right now                                    (ranked: stopped now)
2. Reflow profile fault stopped production 6 times   778 units      £9,725
3. PLAN-3102 on SMT-PickPlace-01 is behind           330 units      £4,125
4. Feeder jam stopped production 3 times             204 units      £2,550
5. 188 units failed inspection (Solder defect)       188 units      £2,350
```

**The line to say:** "These are ranked by the good units they actually cost, not
by how loud they are. And notice AMP will not add them up — two problems can
describe the same lost output, and it says so instead of giving you a total it
cannot stand behind."

### 2. Root cause (2 min) — "why are we behind?"

Ask the Copilot. It answers with what it measured *and what it could not*:

> 475 units short of the plans that came due. AMP measured 8,317 units of lost
> capacity in the window: 5,584 with a reason recorded (biggest: slow running),
> and 2,733 with none — planned time that was not running and carries no
> stoppage reason.

**The line to say:** "That last number is the important one. AMP is telling you
what it cannot explain. Most systems quietly round that into a category."

### 3. The owner's questions (3 min)

Ask these, in this order. Every answer is real:

| Ask | Answer |
|---|---|
| What is costing me the most money? | Losses ≈ **£57,763** this week — downtime £45,850, scrap £11,913 |
| Are we running out of material? | 3 items at or below reorder level — reorder the Pick & Place nozzle first |
| Which orders are at risk? | 10 orders, 1 late, 1 at risk — chase **CO-5002 (Bugatti)** first |
| Which machine needs maintenance? | 2 open tasks, **1 overdue** — Corrective on SMT-Reflow-01 |

### 4. The close (3 min) — from answer to action

This is the part nothing else does. Say:

> "How is SMT-Reflow-01 doing?"

then, without naming the machine again:

> "Create a maintenance task."

The first answer is *"SMT-Reflow-01 is Breakdown, health 20/100, 1 open
maintenance task. Latest downtime: Reflow profile fault (70 min)."*

The second names no machine at all, and AMP still knows which one you mean. It
answers with a **draft** — not a record:

> Ready to propose a **Critical** maintenance task for SMT-Reflow-01.
> SMT-Reflow-01 is at risk 80 out of 100 (machine currently in breakdown).
> **There is already 1 open maintenance task on this machine.** Nothing has been
> created: raising it puts it in the approval queue, and it only takes effect
> once somebody approves it.

Read that middle sentence out loud. AMP volunteered the one fact that argues
*against* the thing it is proposing.

Press **Propose this action**. It appears in Approvals as *Proposed*, with the
task still *Proposed*, not open. Approve it there and the task opens — and AMP
freezes the metric it was meant to move, so in a week it can tell you whether
it helped.

**The line to say:** "The AI never wrote anything. It read the plant, wrote down
what it would propose, and a person approved it. That is the difference between
a copilot you can put on a shop floor and one you cannot."

---

## WHAT NOT TO CLAIM

- **AMP does not talk to your PLCs.** It stores what an edge agent sends. The
  connectivity screen says so on its own face; do not walk past it.
- **No language model is running in production.** Every answer above is AMP's
  own engine over the live data. A self-hosted model has been measured and is
  not yet adopted; production has no GPU. If asked, say exactly that.
- **The failure-risk model is validated on synthetic machines only.** Its own
  screen says so. Do not present it as a prediction about a real plant.
- **Do not total the problem figures.** AMP refuses to, deliberately, and the
  card explains why. Adding them up on a whiteboard undoes the one piece of
  honesty a manufacturing buyer will actually test you on.
