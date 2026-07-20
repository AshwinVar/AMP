# The Engineering History of AMP

*Reconstructed from the repository itself — 197 commits, 137 pull requests, 8 ADRs, 45 days.*

**Scope:** `3578204` (2026-06-04, "Initial FlowMES-Enterprise") → `3875b20` (2026-07-19, "calibrate sim production volume").
**Method:** git history, branches, PR titles, ADRs, source, tests, config. Where documentation and code disagree, the code wins and the disagreement is recorded (§7.6).
**Audience:** engineers joining AMP who need to know not just what the system does, but why it is shaped this way — and which parts are load-bearing.

---

## 1. Timeline — the eras

Commit volume by day tells the story before any of the details do:

```
Jun 04  ████ 4          (birth)
Jun 18  ████████ 8      (deploy war)
Jun 19-21 ███████████ 11
Jun 22  █████████████ 13 (first customer)
Jun 27  █████ 5         (hardening)
Jun 29  ████████ 8      (platform layer)
Jun 30  ███ 3
Jul 05  █ 1             (the quiet month-end)
Jul 11  ███ 3           (rebrand)
Jul 13  ████ 4          (ADRs — architecture begins)
Jul 14  ████████████████████ 20  (event bus, AI platform, agents)
Jul 15  █████████ 9     (twin, read-models)
Jul 16  ███████████████████████████ 27 (read-model explosion)
Jul 17  ██████████████ 14 (line-aware factory, briefing)
Jul 18  ████████████████████████████████████████████ 44 (pillars, copilot, perf)
Jul 19  ███████████████████████ 23 (SaaS lifecycle, LLM)
```

The shape is meaningful: a slow, painful six weeks of product-building, then an inflection on **2026-07-13** when the first two ADRs were accepted. Everything after that point moves an order of magnitude faster, because the architecture stopped being improvised.

---

### Era 1 — Birth as FlowMES (2026-06-04, 4 commits)

**Commits:** `3578204` Initial FlowMES-Enterprise · `1dcaabb` added readme · `4ef2e05` remove unnecessary · `8a3e83a` PLC simulator documentation

**Problem:** an SME manufacturer's shop floor has no real-time visibility. Machine state, downtime and production live in paper and spreadsheets.

**What shipped:** a FastAPI + SQLAlchemy backend and a Next.js dashboard, with a PLC simulator so the thing could be demonstrated without a factory attached. The simulator turned out to be one of the most consequential early decisions (§6.10) — every subsequent demo, and most subsequent testing, depended on the product being *alive* without hardware.

**Architectural impact:** established the monolith-plus-SPA shape that still holds today. `backend/main.py` was born here and has been touched by **92 of 197 commits** — it is, and remains, the system's centre of gravity (§8.4).

**Artifact of note:** the very first commit also created `FlowMES-Enterprise/` — a nested repository (its own `.git` directory), tracked as a single gitlink entry. It has never contained working code and has never been removed. It is the oldest piece of dead weight in the tree (§7.6).

---

### Era 2 — The Deploy War (2026-06-18 → 06-21, 19 commits)

**Representative commits:** `2377799` Deploy-ready: env config, module system, factory simulator · `c3d9355` Fix start command: use `python -m uvicorn` · `4e2150e` Add railway.toml · `e9ca5fd` Add passlib; bust Railway build cache · `b7d6d14`/`09fe145` CORS fixes · `63708ba` live simulation loop in FastAPI startup · `3c28afe`/`265041e`/`dbc5043` **three consecutive commits fixing one search bar**

**Problem:** working locally is not shipping. This era is almost entirely the friction of getting a Python monolith and a Next.js app onto Railway and Vercel and talking to each other.

**What was learned, expensively:**
- **CORS preflight** (`b7d6d14`, `09fe145`): `allow_credentials=True` with a wildcard origin produces a 400 on OPTIONS. The fix — credentials off, explicit origins — is still the shape of the CORS config today, and CORS ordering came back to bite once more a month later (§7.2).
- **The search bar took three commits** (`3c28afe` → `265041e` → `dbc5043`): a flex container, then a class, then a `backdrop-filter` stacking context, each independently blocking keyboard input. A reminder that CSS bugs are diagnosed by bisection, not by reasoning.
- **The simulation loop** (`63708ba`) moved into FastAPI startup so the deployed dashboard stayed animated — the origin of the background loop that would later need tenant-allowlisting (#117) and volume calibration (#137).

**Also shipped:** the module/pack system (`2377799`) — the seed of plan-tier licensing that would not be enforced for another month (#121, #122); BOM viewer; RBAC (`ccf9d19`); enterprise inventory — remnants, issue slips, GRN, cycle count, variance report, CSV import (`d8d5123`).

---

### Era 3 — The First Customer, and Multi-Tenancy by Necessity (2026-06-22, 13 commits)

**Commits:** `e23ca1d` GMATS tenant inventory (4-bucket stock, aliases, proforma/tax-invoice) · `22ee8fc` dedicated GMATS client login · `94600e7` lock employee creation to Admin; tenant-scoped user management · `b9dbcb0` **enforce tenant isolation on GMATS data** · `1b690b3` GMATS admin from env var, no hardcoded secret · `df08bb6` reconcile client login tenants on startup

**Problem:** GMATS Machineries (a Bengaluru compressor manufacturer) became the first real pilot. A second company's data now shared a database with the demo factory.

**Why it matters:** this is where multi-tenancy *actually* started — not as an architecture, but as a series of pragmatic patches. `tenant_code` appeared on GMATS tables; logins were mapped to tenants through a `CLIENT_TENANTS` dictionary; a startup block reconciled users whose `tenant_code` predated the column. `b9dbcb0`'s message — "enforce tenant isolation" — describes enforcement in *specific endpoints*, not a system property.

**Technical debt created (deliberately):** isolation lived in whichever query the author remembered to filter. That debt was paid off three weeks later by ADR-0002, and the ADR says so explicitly: *"every week of new code against un-scoped tables raises future cost and risk."*

**Also notable:** `1b690b3` — the GMATS admin password moved to an env var rather than being committed. Security hygiene appeared here and held; there are no committed secrets in the history.

---

### Era 4 — Hardening and the Platform Layer (2026-06-27 → 06-30, 16 commits)

**Commits:** `bac9ca6` **harden auth: bcrypt passwords, locked CORS, optional Sentry** · `02ce565` public landing page + pricing, login moved to `/login` · `5fb4022` **platform layer: per-tenant licensing, white-label branding, audit log, health** · `a70e0a8` wire branding/licensing into UI; add Docker + CI · `ca3fb09` AI Factory Copilot (off until `ANTHROPIC_API_KEY`) · `85d84b5` copilot calls Anthropic REST via stdlib (drop SDK) · `2482135` **remove Dockerfile from backend/ — it hijacked Railway's NIXPACKS builder** · `1dbda87` industrial connectivity adapter framework

**This era turned a demo into a product.**

- **`bac9ca6`** replaced SHA-256 with bcrypt, including transparent rehash-on-login for legacy hashes — a migration strategy with zero user impact, still in `main.py` today.
- **`5fb4022`** introduced `platform_routes.py` and `TenantConfig`: licensing, branding, audit log, health. This is the SaaS substrate that ADR-0008 would formalise three weeks later.
- **`ca3fb09` + `85d84b5`** established the pattern that survived everything since: **the LLM is optional, gated on an environment variable, and called over plain REST with no SDK dependency** so that adding AI can never break the deploy. Nineteen days later this design let a second provider (Gemini) slot in behind the same interface in a single PR (#131).
- **`2482135`** is the era's sharpest lesson: a `Dockerfile` in `backend/` silently overrode `railway.toml`'s `builder = "NIXPACKS"`, so deploys failed while the last good deploy kept serving — new endpoints simply 404'd. **Diagnosis technique invented here and used ever since:** diff the deployed `/openapi.json` against the code.

---

### Era 5 — Rebrand: FlowMES → AMP (2026-07-11, 3 commits)

**Commits:** `ae9b55d` Rebrand FlowMES to AMP · `c4e4c78` complete rebrand across app, docs, PDFs · `95d04d9` stop tracking committed virtualenv and Python bytecode

A rename is usually cosmetic. This one marked a change of ambition: from *FlowMES* (a manufacturing execution system) to **AMP** (an autonomous manufacturing platform). The vision text in the README changed from machine monitoring to "an AI operating system for manufacturing" — and two days later the architecture changed to match.

`95d04d9` also removed a committed virtualenv from tracking — the second piece of accumulated repo debt, and unlike `FlowMES-Enterprise/`, this one was actually cleaned up.

---

### Era 6 — Architecture Begins: the ADR Inflection (2026-07-13 → 07-14, 24 commits)

**The single most important day in the repository is 2026-07-13**, commit `30fa5fc`: *"Accept ADR-0001 (domain event bus) and ADR-0002 (tenant-scope core domain)."* Before it, the project was a well-built application. After it, it was an architecture with a written rationale, and the delivery rate roughly quintupled.

**PR#1 — the event bus** (`f198728`, ADR-0001). `backend/events.py`: an in-process publish/subscribe bus with immutable, tenant-scoped, dataclass events (`ProductionCompleted`, `DowntimeStarted`, `InventoryLow`, `QualityInspectionFailed`), every one appended to an `event_log`. The first migration was behaviour-preserving: work-order completion's inline BOM movement became a *subscriber*. Nothing changed for users; everything changed for what was possible next.

**PR#2 — tenant columns** (`e3e5a76`, ADR-0002). `tenant_code` added to core tables, backfilled, indexed.

**PR#3 — enforcement, and the only revert in the repository.** This is the most instructive incident in AMP's history and it is worth reading in full.

> `65ed29d` PR#3: automatic tenant scoping enforcement
> `b033b8a` **Revert "PR#3: automatic tenant scoping enforcement"**
> `07b3218` Re-apply tenant enforcement (fixed pure-ASGI middleware)
> `9a4f8e6` docs: PR#3 middleware postmortem + post-deploy smoke-test step

The first implementation bound the request's tenant using `@app.middleware("http")` — Starlette's `BaseHTTPMiddleware`. That class buffers the request body and runs the endpoint in a *separate task*. Two consequences: **every POST deadlocked** (`POST /login` hung in production), and the tenant `contextvar` would not have propagated into the threadpool handler even if it hadn't. **It passed all unit tests, because the unit tests never exercised the HTTP layer.**

The fix was a pure-ASGI middleware (`TenantScopeMiddleware`) that shares the endpoint's task. The postmortem is preserved in ADR-0002 and produced two standing rules that are still enforced:
1. Never use `BaseHTTPMiddleware` for request-context binding — use pure ASGI.
2. **Any middleware or auth change must be smoke-tested against a running server (boot + `POST /login`), not only unit tests** — codified in `docs/Production-Setup.md` §7.

**PR#4–#5** completed the sweep: automatic scoping across all core tables, then the live WebSocket feed.

**PR#6–#11 (ADR-0003) — the AI platform.** `backend/ai/` was created as a package of capabilities that *consume the event stream*, with the existing `predictive_engine` and `ai_copilot` wrapped rather than rewritten (strangler, per the standing charter). PR#9 introduced the **Insights read-model** — the pattern that would later be named in ADR-0007 — and PR#10/#11 made **Mission Control** the default dashboard view.

**PR#13–#20 (ADR-0004, ADR-0005) — agents.** The Maintenance agent (`81a045d`) was the first AI that *acted*: on critical predictive risk it opened a real maintenance task. Then, immediately, the guardrails: **ADR-0005's oversight layer** (`2046449`) made agents *propose* rather than act, unified in one `AgentAction` record that is simultaneously audit log and approval queue. Reorder, Quality and (later) Escalation and Yield agents followed the same contract.

The sequencing here is the interesting part: autonomy shipped **first**, oversight **second**, two days apart. ADR-0004 explicitly considered and rejected a human-approval queue as "premature" — then ADR-0005 built exactly that once a second agent (Reorder, which drafts purchase orders) made the risk concrete. That is not a mistake; it is a correctly-timed reversal, and the ADRs record both sides of it.

**Numbering artifact:** on this day the internal "PR#n" labels desynced from GitHub's numbering — `81a045d` is "PR#13 … (#12)" while `51fbde2` is "PR#12 … (#13)". Harmless, but future archaeologists should trust the parenthesised GitHub number.

---

### Era 7 — The Twin and the Read-Model Explosion (2026-07-15 → 07-16, 36 commits)

**ADR-0006** (`4b9fd38`, PR#21) added the **Machine Health twin**: a per-machine live snapshot composing state, a 0–100 health score from predictive risk, recent downtime, open tasks and pending agent actions. Critically, it was defined as a *read-model over existing tables* — no new storage — and the ADR explicitly rejects both extending the spatial floor-map and materialising a twin table.

**ADR-0007** (`4944776`) then named the pattern that had been emerging for four days: an **AI read-model** is a pure `build_*` projection that composes signals into one snapshot answering one question, adds no storage, is tenant-scoped, exposed 1:1 at a GET endpoint, and unit-tested in isolation. `pulse` is called out as the proof of composability — *a read-model over read-models*.

What followed was the fastest sustained delivery in the project's history: **27 commits on 2026-07-16 alone**, nearly all of them new read-models and their dashboard surfaces — downtime Pareto, quality first-pass yield, production throughput, OEE per machine and per line, agent detail, supply risk, agent autonomy controls. The pattern was cheap enough that a new decision surface cost a function, an endpoint, a component and a test.

**`e7a14fd` (PR#49)** rebuilt the DEFAULT demo factory as a two-line SMT → IC instrument-cluster plant, and immediately produced two FK-ordering bugs (`e02deb0`, `a66e745`) — foreshadowing the identical class of bug in tenant purging three days later (#126).

---

### Era 8 — The Proactive Plant (2026-07-17 → 07-18, 58 commits)

The product stopped being a set of dashboards and started telling the user what to do.

- **`e6a73ab` (#67) — the morning briefing**: "what needs attention right now," a ranked feed every pillar contributes to. Then made **actionable** (#68 — click an alert, drill in), then **proactive** (#70 — the Escalation agent raises the top alert into the approval queue on its own), then **traceable** (#71 — the ⚡ pill deep-links to the escalation).
- **New pillars in rapid succession**: delivery outlook (#72), cost of losses (#74), executive scorecard with week-on-week deltas (#76–#78), twin heat-mapping by OEE/cost (#80), maintenance load (#82–#84), the weekly plant report (#85), compliance (#95–#97).
- **`a278917` (#86) — the copilot became real without an API key.** `ai/assistant.py` is a rule-first keyword router over the read-models: pillar Q&A, machine-by-name, week-on-week trends, "find X", "help", and a one-shot rundown. This is the single most commercially important decision of the era — the AI feature demos with zero cost and zero external dependency (§6.9).
- **Operational maturity arrived**: a public `/health` and an AI-platform self-report (#91); an end-to-end API surface test across every read-model endpoint (#90, extended #107); sliding-session token refresh (#101) and expired-session redirect (#102); global entity search (#104); mobile navigation (#105).
- **A three-PR performance sweep** (#108, #109, #111): read-model time windows pushed from Python into SQL, then the `created_at` columns they filter on indexed. `#110` fixed a pillar that had been unwindowed by oversight.

**Two incidents worth studying:**

**`bc86335` (#112) — the duplicate API client.** Entity search silently returned nothing in production. Root cause: `page.tsx` carried its own local `apiGet` with a hardcoded `?t=` cache-buster, so `/search?q=reflow` became `/search?q=reflow?t=…`. The minimal fix was one line; the *structural* fix (`0d6cd4d`, #113) deleted 88 lines by making the page adopt the canonical `lib/api` client. Lesson: a duplicated abstraction is a latent outage.

**`76b6035` (#114) — the RESEED incident.** A `RESEED_FACTORY=1` environment flag intended as a one-shot rebuild was left set on Railway, so **every boot wiped and rebuilt the production factory** — roughly 41 times. Symptoms were confusing precisely because the app worked: cost figures swung between \$317k and \$50k, week-on-week deltas were always null, machine IDs climbed 126 → 461. The fix made the flag **self-consuming**: its value is recorded in an `EventLog` row on use, and a boot that finds its own flag already consumed skips the rebuild — wipe-proof, because the record survives in the one table the reset does not touch.

---

### Era 9 — The SaaS Machine (2026-07-19, PRs #115–#128)

One day; the entire commercial lifecycle. Formalised afterwards in **ADR-0008**.

| PR | What | Why it mattered |
|----|------|-----------------|
| #115 | Second-tenant onboarding: founder preview + starter factory | `effective_tenant(claim, header)` — the `X-Tenant` header is honoured **only** for DEFAULT-claim tokens, so the founder previews any tenant with full fidelity and a client can never escape its own |
| #116 | Scope the tenant registry | Any authenticated user could read every company's name, plan, seats and fee — a metadata leak found while explaining isolation |
| #117 | Restrict the sim loop to demo tenants | The simulator ran unscoped and animated **every** tenant's machines — harmless for demos, catastrophic for a real customer's data |
| #118 | One-click admin provisioning + password change | Removed the last manual onboarding step; one-time password, bcrypt-stored, audit-logged |
| #119 | Sim diagnostics in `/platform/status` | Made "is the sim running, over whom?" answerable from the app rather than Railway logs — immediately paid for itself twice (§7.5) |
| #120 | Cancelled subscriptions block login | Made the SaaS Admin status dropdown a real control |
| #121 | Plan tiers drive the licence | Pricing stopped being cosmetic: Starter → core, Growth → +operations+factory, Enterprise → all |
| #122/#123 | Server-side plan gating, then **inside CORS** | The UI padlocked packs but the API still served them. #123 is a pure-ordering bug: Starlette runs the *last-added* middleware first, so the gate's 403s were being emitted outside CORS and arriving as opaque network errors |
| #124/#126 | Tenant offboarding + FK-safe purge | Deleting a company orphaned its data forever. The purge sweeps every model carrying `tenant_code` — future tables covered automatically — in multi-pass savepoint deletes |
| #125 | Tenant-prefix starter inventory codes | **The most dangerous bug of the era**: `item_code` is globally unique, so only the *first* tenant to seed ever got a factory. Every subsequent real customer would have landed on an empty dashboard, failing silently |
| #127 | Trial lifecycle | 30-day clock, days-left visible to both sides, expiry blocks login |
| #128 | Window predictive risk to 30 days | The risk engine scored absolute thresholds against *lifetime* accumulation, so every long-lived machine eventually became permanently "risky" — inverting what predictive maintenance means |

**#125 and #126 were both discovered by end-to-end verification on production**, not by tests — and both immediately gained regression tests. The offboarding suite now runs with `PRAGMA foreign_keys=ON` so SQLite enforces what Postgres enforces (§7.4).

---

### Era 10 — The LLM, For Real (2026-07-19, PRs #129–#137)

The copilot had been "LLM-optional" since Era 4. This era connected one, and the sequence is a case study in diagnosing an integration you don't control.

1. **#129** — the LLM began degrading gracefully: any failure falls back to the rule-based assistant, labelled `source: "rules"` with an honest note. Built because a credit-less Anthropic key surfaced a raw `Anthropic API 400: {...}` into what would be a customer's chat window.
2. **#131** — a **Gemini free-tier provider** behind the same endpoints. Provider chosen by environment only; with both keys present, Anthropic (paid, commercial data terms) wins. Documented as demo-only: free-tier data may be used for training.
3. **#132** — `/ai/status` began reporting the **last LLM error, founder-only**. The graceful fallback had hidden failures from customers *and* from us.
4. **#133** — that diagnostic immediately paid off: the hardcoded default model 404'd with *"no longer available to new users."* Rather than chase names, the code now asks Gemini's ListModels API what the key actually has and retries.
5. **#134** — and paid off again: the newest discovered model 429'd instantly, because preview/experimental variants carry zero free-tier quota. The fix walks candidates on 404/429 and caches the first that answers.
6. **#135/#136** — the working LLM finally reached the UI: conversational answers with per-answer provenance badges in the copilot panel, and a one-tap AI narrative on the weekly report.
7. **#137** — the simulator's volume was calibrated after the Overview was caught claiming **79,970 downtime minutes in a 7-day window** — more minutes than a week contains. The sim had been writing a full 480-minute shift record every 45-second tick; OEE ratios were always correct, but every absolute magnitude was inflated ~100×.

---

## 2. Evolution of the architecture

```
Monolith + SPA                     (Jun 04)
  │  FastAPI + SQLAlchemy + Next.js; simulator instead of hardware
  ▼
+ Deployment & module system       (Jun 18–21)
  │  Railway/Vercel; packs defined but unenforced
  ▼
+ Tenancy by patch                 (Jun 22)
  │  GMATS: tenant_code on some tables, filtering per-endpoint
  ▼
+ Platform layer                   (Jun 29)
  │  TenantConfig, audit log, health; LLM gated behind an env var
  ▼
+ Event backbone & tenant chokepoint  (Jul 13–14)   ← ADR-0001, ADR-0002
  │  events.py + event_log; ORM-level scoping via pure-ASGI middleware
  ▼
+ AI platform on the stream        (Jul 14)         ← ADR-0003
  │  ai/ package; capabilities subscribe to events; rule-first, LLM-optional
  ▼
+ Agents with oversight            (Jul 14)         ← ADR-0004, ADR-0005
  │  agents propose → AgentAction (audit + queue) → human approves
  ▼
+ Read-model layer                 (Jul 15–18)      ← ADR-0006, ADR-0007
  │  31 build_* projections; every decision surface is a pure function
  ▼
+ Commercial chokepoints           (Jul 19)         ← ADR-0008
     effective tenant · plan gate · trial clock · offboarding purge
```

**The through-line is chokepoints.** Every serious correctness property in AMP is enforced at exactly one place that requests must pass through, never at N call sites:

| Property | Chokepoint | Alternative rejected |
|---|---|---|
| Tenant isolation | `do_orm_execute` hook + `before_flush` stamp | Filtering in ~80 queries |
| Request tenant binding | `TenantScopeMiddleware` (pure ASGI) | `BaseHTTPMiddleware` — deadlocked POSTs |
| Plan licensing | `PlanGateMiddleware` + path→pack table | Per-endpoint decorators |
| Agent safety | `AgentAction` propose/approve | Per-item approval flags |
| Data purge | Sweep of every model with `tenant_code` | A hand-maintained table list |

**Debt eliminated:** per-endpoint tenant filtering (ADR-0002); scattered AI scripts (ADR-0003); duplicated frontend API client (#113); Python-side time windows (#108–#111); the committed virtualenv (`95d04d9`); orphaned tenant data (#124).

**Debt remaining:** §8.5.

---

## 3. Feature catalogue

| Feature | Introduced | Evolution | Maturity |
|---|---|---|---|
| **Auth / RBAC** | Era 1–2 (`ccf9d19`) | SHA-256 → bcrypt with rehash-on-login (`bac9ca6`) → 4h JWT + sliding refresh (#101) + expiry redirect (#102) + self-service password change (#118) | **Production** |
| **Inventory** | Era 2 (`d8d5123`) | Basic stock → enterprise (remnants, issue slips, GRN, cycle count, variance, CSV) → GMATS 4-bucket variant | **Production**, most feature-dense domain |
| **Production / work orders** | Era 1 | WO completion → BOM deduction inline → *event subscriber* (PR#1) | **Production** |
| **Downtime** | Era 1 | Logs → Pareto → reason drill-down → per-line split (#66) | **Production** |
| **Quality** | Era 2 | Inspections → first-pass yield + defect Pareto → 7-day window (#110) | **Production** |
| **Maintenance / CMMS** | Era 3 | Manual tasks → agent-opened tasks (ADR-0004) → maintenance load pillar (#82–#84) | **Production** |
| **Multi-tenancy** | Era 3 (patches) | Per-endpoint filters → ORM chokepoint (ADR-0002) → founder preview + full lifecycle (ADR-0008) | **Production**, hardened |
| **Licensing** | Era 2 (packs) / Era 4 (`TenantConfig`) | Cosmetic → UI padlocks (#121) → **API-enforced** (#122) | **Production** |
| **Audit logging** | Era 4 (`5fb4022`) | Platform log → `AgentAction` as agent audit → provisioning/purge/login-block events | **Production** |
| **Event bus** | Era 6 (PR#1) | 4 event types, in-process, synchronous, `event_log`-backed | **Production**, deliberately minimal |
| **AI platform** | Era 6 (ADR-0003) | 3 wrapped engines → **31 read-models** across ~28 modules | **Production** |
| **AI agents** | Era 6 (ADR-0004/5) | 1 → **5** (maintenance, quality, reorder, escalation, yield), all propose-then-approve | **Production** |
| **Machine Health twin** | Era 7 (ADR-0006) | Grid → single-machine cockpit → sparklines → overlay heat-mapping (#80) | **Production** |
| **Mission Control** | Era 6 (PR#10/#11) | Insights feed → default landing view → Factory Pulse header | **Production** |
| **Morning briefing** | Era 8 (#67) | Digest → actionable → agent-proactive → deep-linked | **Production**, the product's signature |
| **Copilot** | Era 4 (`ca3fb09`) | LLM-gated stub → **rule-first router** (#86) → LLM w/ fallback (#129) → dual-provider (#131) → self-healing models (#133/#134) → **in the UI** (#135/#136) | **Production** (rules) / **Beta** (LLM, free tier) |
| **Industrial adapters** | Era 4 (`1dbda87`) | Framework + simulator for OPC UA, Modbus, S7, Allen-Bradley, Beckhoff, Omron | **Experimental** — real drivers need an on-site edge agent |
| **MQTT / WebSockets** | Era 1–2 | Telemetry ingestion + live dashboard; WS feed tenant-scoped in PR#5 | **Production** |
| **Health checks** | Era 4 / #91 | Platform health → public `/health` → AI self-report + sim heartbeat | **Production** |
| **CI/CD** | Era 4 (`a70e0a8`) | GitHub Actions: backend compile-check + frontend build; auto-deploy Railway/Vercel from master | **Production**, but thin (§8.5) |
| **Docker** | Era 4, **removed** (`2482135`) | Deleted because it hijacked NIXPACKS | **Abandoned deliberately** |

---

## 4. Product evolution

**FlowMES** *(Jun 04)* — real-time machine downtime visibility for one SME factory. Vision: replace the clipboard.

**Enterprise MES** *(Jun 18–22)* — inventory depth (remnants, GRN, variance), BOM, RBAC. Vision: replace the clipboard *and* the spreadsheets.

**Multi-tenant SaaS** *(Jun 22–29)* — GMATS as first pilot, then licensing/branding/audit as a platform layer, then a public landing page with INR pricing tiers. Vision: one deployment, many factories.

**AMP — Manufacturing Intelligence** *(Jul 11–16)* — the rebrand, then event bus → AI platform → agents → read-models. Vision changed from *recording* the factory to *reasoning about* it. The README's framing shifted to "an AI operating system for manufacturing," with the MES explicitly demoted to "the foundation."

**AMP today** *(Jul 17–19)* — proactive (the plant tells you what needs attention and an agent escalates it), conversational (ask in plain language, get a grounded answer), and commercially complete (onboard → license → trial → bill-state → offboard). Vision: the factory's operating system, sold as a subscription.

---

## 5. ADR timeline

| ADR | Accepted | Implemented by | Rejected alternatives | Long-term capability unlocked |
|---|---|---|---|---|
| **0001 — Domain event bus** | 07-12 | PR#1 `f198728` | Direct calls (coupling); Kafka/NATS now (premature ops); DB triggers/CDC (couples events to schema) | Everything downstream: AI subscribes to the stream; `event_log` is the substrate for analytics and the twin |
| **0002 — Tenant-scope the core domain** | 07-12 | PR#2 `e3e5a76`, PR#3 `65ed29d`→revert→`07b3218`, PR#4, PR#5 | DB/schema-per-tenant (heavy, blocks cross-tenant AI); Postgres RLS (layer later); do nothing (compounding risk) | Safe multi-tenancy; the founder-preview and plan-gate chokepoints of ADR-0008 ride the same mechanism |
| **0003 — AI as an event-consuming platform** | 07-14 | PR#6–#11 | Scattered scripts; one AI god-module; LLM-first (cost/latency/offline-fragile); separate AI microservice (premature) | The `ai/` package; rule-first economics that let AI demo for free |
| **0004 — Agents act on the stream** | 07-14 | PR#13 `81a045d`, PR#15 | Advisory-only (never realises autonomy); act on every recommendation (too noisy); approval queue (judged premature — reversed 2 days later); separate agent runtime | Autonomy ladder: elevated → recommend, critical → act |
| **0005 — Agent oversight** | 07-14 | PR#16 `2046449`, PR#17–#20 | Log without control; per-item approval flags; a workflow engine (premature) | `AgentAction` as unified audit + queue; agent metrics, trends, roster, ROI all read from it |
| **0006 — Machine Health twin** | 07-15 | PR#21 `4b9fd38`, PR#23 | Extending the spatial floor-map (muddies both); a materialized twin table (premature) | Per-machine cockpit; the overlay heat-map; the pattern generalised into ADR-0007 |
| **0007 — Read-models as projections** | 07-15 | `4944776` + ~30 PRs | Materialized projections; composing in the frontend (leaks scoping, duplicates logic); fat bespoke endpoints | The delivery engine — a new decision surface costs a function + endpoint + component + test, never a migration |
| **0008 — Tenant lifecycle & commercial enforcement** | 07-19 | PRs #115–#129 | Per-endpoint checks; delete-with-implicit-purge; trial state on `TenantConfig` | The SaaS machine; documents three hard-won testing lessons |

**ADR discipline note:** ADRs 0001–0002 were accepted *before* implementation; 0008 was written *after* the PRs shipped, as a consolidation. Both modes are legitimate; the repository is honest about which is which.

---

## 6. Major engineering decisions, and their reasoning

**6.1 Why an event bus, and why in-process?** Coupling was growing with every consumer, and the AI vision needed a stream to reason over. But the scale — one pilot — could not justify Kafka's operational burden. The resolution: put the *interface* in place now, keep the transport in-process and synchronous (sharing the caller's DB session so subscriber work commits atomically), and swap in an outbox + broker later without touching a single producer or subscriber. Optionality bought for ~200 lines.

**6.2 Why row-level `tenant_code` and not a database per tenant?** Schema-per-tenant isolates harder but complicates the cross-tenant analytics and AI the vision depends on, and it multiplies operational work at a scale of two tenants. Row-level scoping also matched the pattern GMATS data already used. RLS is explicitly deferred as defense-in-depth, not rejected.

**6.3 Why enforce scoping in the ORM rather than in queries?** Because the failure mode of the alternative is silent and catastrophic: one forgotten `WHERE` leaks another company's data. A `do_orm_execute` hook fails closed for every current *and future* query, including fetch-by-id (a foreign row simply isn't found).

**6.4 Why pure ASGI middleware?** Forced by production. `BaseHTTPMiddleware` buffers bodies and runs endpoints in a separate task — deadlocking POSTs and breaking `contextvar` propagation. This is the repository's clearest example of a decision made by evidence rather than preference.

**6.5 Why read-models instead of tables?** A projection cannot go stale — there is nothing to invalidate, no sync job, no migration. At the volume of decision surfaces AMP was adding (dozens in a week), correctness-by-construction beat performance-by-materialisation. The escape hatch is preserved: any read-model can be materialised later behind the identical API.

**6.6 Why do agents propose instead of act?** They *did* act first (ADR-0004), deliberately, because the first action was internal, bounded, idempotent and reversible. The moment an agent could draft a purchase order, the calculus changed and ADR-0005 introduced oversight. The unified `AgentAction` was chosen over per-item approval flags because it makes "everything the agents did" one queryable thing — which is precisely what the metrics, trend, roster and ROI views were later built on.

**6.7 Why a modular monolith and not microservices?** Deployment friction was already the dominant cost (Era 2 was almost entirely deploy debugging) at *one* service. Every ADR that could have introduced a service — AI platform, agent runtime — explicitly deferred it behind a package interface, on the grounds that extraction stays cheap while premature distribution does not.

**6.8 Why no Docker?** Tried and removed (`2482135`). A `Dockerfile` in the Railway build directory silently overrode the pinned NIXPACKS builder, breaking deploys in a way that *looked* like nothing deploying. The platform's native builder already containerises; the extra layer bought nothing and cost a day.

**6.9 Why rule-first, LLM-optional?** Three reasons, all commercial: the copilot demos with zero API cost; it works offline and can't be rate-limited; and answers over read-models are deterministic and can't hallucinate a machine that doesn't exist. The LLM became an *enhancement* rather than a dependency — which is exactly why a credit-less key (#129) and a retired model name (#133) were annoyances rather than outages.

**6.10 Why keep a factory simulator in production?** Because the product's value is only visible when the factory is alive. The cost of that decision came due twice — it animated other tenants' data (#117) and it inflated every magnitude by ~100× (#137) — and both times the fix was calibration, not removal.

---

## 7. What was learned

**7.1 The biggest successes**
- **The ADR inflection.** Delivery rate went from ~1.5 to ~20 commits/day after 2026-07-13. Writing the *why* down made the next hundred decisions cheap.
- **Read-models as a delivery engine.** 31 projections, ~40 test suites, no migrations.
- **Rule-first AI.** The most commercially load-bearing decision in the codebase.
- **Chokepoint enforcement.** Isolation, licensing, and purging are all properties of the system, not habits of the author.

**7.2 The biggest mistakes (all instructive)**
| Mistake | Cost | Fix | Standing rule |
|---|---|---|---|
| `BaseHTTPMiddleware` for tenant binding | Production POST deadlock; only revert in history | Pure ASGI | Smoke-test middleware against a running server |
| `Dockerfile` in the Railway build dir | Silent deploy failures | Deleted | Diff deployed `/openapi.json` vs code |
| `RESEED_FACTORY` left set | Prod factory wiped ~41× | Self-consuming flag via `EventLog` | Destructive env flags must be single-shot by construction |
| Duplicate `apiGet` in `page.tsx` | Entity search silently broken | Adopt canonical client (−88 lines) | One abstraction, one home |
| Unprefixed starter `item_code` | Every tenant after the first got an empty factory | Tenant-prefix + two-tenant test | Multi-tenant seeds need multi-tenant tests |
| Plan gate added after CORS | 403s arrived as opaque network errors | Reorder | Response-producing middleware goes *inside* CORS |
| Unwindowed lifetime risk inputs | Machines became permanently "risky" | 30-day window | Thresholds need windows |
| Sim writing a shift per tick | 79,970 downtime minutes in a 7-day week | 15-min slices, gated | Demo data must be physically possible |

**7.3 Dead ends and abandonments** — Docker (removed); the `FlowMES-Enterprise/` nested repo (dead since commit 1, never removed); a committed virtualenv (untracked in `95d04d9`); the "act on every recommendation" autonomy model (superseded by ADR-0005 within 48 hours).

**7.4 The testing lesson, learned three times.** Unit tests passed while production broke — for the middleware deadlock (no HTTP layer in tests), the purge FK ordering (SQLite doesn't enforce foreign keys by default), and the seed collision (single-tenant tests can't see a global-uniqueness clash). Each produced a permanent countermeasure: a mandatory post-deploy smoke test, `PRAGMA foreign_keys=ON` in the offboarding suite, and a two-tenant seeding test.

**7.5 The unexpected discovery: build the diagnostic first.** `/platform/status`'s sim block (#119) and LLM `last_error` (#132) were each built to answer a question that had cost real time — and each immediately exposed a *different* bug within minutes (the mis-applied `SIM_TENANTS` variable; the retired Gemini model). Observability paid for itself on the first use, twice.

**7.6 Documentation-vs-code inconsistencies found during this reconstruction**

*(Addendum, 2026-07-20: four of the five were resolved the same day the document was compiled — see the trailing notes. Kept in place because the reconstruction found them.)*

1. **`railway.toml` set `healthcheckPath = "/docs"`** — pointing Railway's health probe at the Swagger UI, even though a purpose-built public `/health` (with a real DB check) shipped in #91. The probe passed whenever FastAPI was up, including when the database was down. — *Resolved: #140 made `/health` return 503 on a dead DB; #144 pointed Railway's probe at `/health`; #145 added a build-sha `version` so each deploy's cutover is verifiable.*
2. **The docs describe an Anthropic-only LLM.** `docs/AMP-Complete-Documentation.md` and the README described `ANTHROPIC_API_KEY` as *the* switch and `_ask_claude` as *the* call path. Since #131–#134 the code supports a Gemini provider with model self-discovery; production runs Gemini. — *Resolved (#141): the complete documentation's LLM sections now describe the dual-provider design and the graceful fallback.*
3. **ADR-0002 promises a CI guard** that "fails loudly when a core query is missing a tenant filter." `.github/workflows/ci.yml` ran only `compileall` and `npm run build`. The guard was never built; the ORM chokepoint made it less necessary, but the ADR overstates what shipped. — *Still open: CI now runs the full test suite (#139), which is the broader safety net; the specific static tenant-filter guard remains unbuilt.*
4. **`FlowMES-Enterprise/`** was tracked at the repo root as a stale gitlink containing a nested `.git`. — *Resolved (#148): untracked and gitignored.*
5. **Internal `PR#n` labels desynced from GitHub numbering** on 2026-07-14 (`PR#13 … (#12)`, `PR#12 … (#13)`). — Cosmetic; trust the parenthesised GitHub number.

---

## 8. Current state

**8.1 Complete and production-ready** — MES core (machines, downtime, shifts, work orders, production, quality, inventory incl. the enterprise and GMATS variants); auth/RBAC with bcrypt and sliding sessions; multi-tenancy end-to-end (isolation, founder preview, onboarding, licensing, trials, cancellation, offboarding); the event bus and `event_log`; the AI platform's 31 read-models; 5 agents under propose/approve oversight; Mission Control, the morning briefing, Machine Health twin, executive scorecard, weekly report; the rule-based copilot; audit logging; `/health` and `/platform/status`; CI + auto-deploy.

**8.2 Beta** — the LLM copilot layer. Wired, live, self-healing, and gracefully degrading, but running on a **free tier whose terms permit training on submitted data**; it must be switched to Anthropic (delete `AI_PROVIDER`, add credits) before any real customer's data flows through it.

**8.3 Experimental** — industrial protocol adapters (framework and simulator only; real OPC UA/Modbus/S7 drivers require an on-site edge agent); the factory simulator itself, which is demo scaffolding that happens to run in production behind a tenant allowlist.

**8.4 What should never be rewritten**
- **`backend/events.py`** — small, correct, and the reason the AI platform could exist at all.
- **`backend/tenancy.py`** — the ORM chokepoint plus `effective_tenant`; every isolation guarantee rests on ~200 lines here.
- **The `ai/` read-model package** — 31 pure functions with individual test suites; the cheapest-to-extend part of the system.
- **The ADR trail** — the *why* is otherwise unrecoverable.
- **The `AgentAction` model** — one record serving as audit log and approval queue; the thing that makes agent autonomy sellable.

**8.5 What needs redesign or attention** *(updated 2026-07-20; most items closed the same day)*
- **`backend/main.py` monolith.** *(Largely resolved, ADR-0009.)* Was 4,274 lines / 192 endpoints — the standing structural debt. **Fifteen** domains have since been peeled into `register(app)` route modules, each preserving the route-count invariant and shipping a registration-guard test: `read_model_routes` (#143), `agent_routes` (#146), `saas_routes` (#147), `costing_routes`, `machines_routes`, `orders_routes`, `factory_ops_routes` (#153), and the core-CRUD + reporting wave — `work_orders_routes` (#154), `inventory_routes` (#155), `quality_routes` (#156), `production_planning_routes` (#157), `industrial_iot_routes` (#158), `operator_routes` (#159), `users_routes` (#160), `reports_routes` (#161). Event-publishing handlers kept their `event_bus.publish(...)` on the request session (guard-tested), so `ProductionCompleted` / `InventoryLow` / `QualityInspectionFailed` still commit atomically after the move. Now **1,675 lines / 44 endpoints**. What remains is the `/analytics` cluster, which is pinned to main-local compute (`generate_alerts`, `analytics_summary`) and needs those factored into `analytics_engine` first — the `calculate_oee_from_record` dedup (#162) was the first such enabling move. The survey for the first peel-off also found and removed a dead shadowed duplicate `/health` (#142).
- **CI is a build check, not a test run.** *(Resolved, #139.)* CI now runs all backend suites and a boot check on every push. Wiring them in immediately surfaced a latent defect: `database.py` did `os.environ["DATABASE_URL"]` with no fallback, so any fresh clone or CI runner would have `KeyError`'d on every test — fixed by giving CI a throwaway SQLite DB while keeping the app fail-loud.
- **Documentation drift** (§7.6). *(Resolved, #141.)* The LLM chapter now describes the real dual-provider design.
- **`railway.toml` healthcheck.** *(Resolved, #144.)* Now points at `/health` (truthful status code since #140) instead of `/docs`; a public build-sha `version` on `/health` (#145) confirms each deploy cuts over.
- **`FlowMES-Enterprise/` gitlink.** *(Resolved, #148.)* Untracked; was an orphan mode-160000 gitlink since commit 1.
- **No database backups and no uptime monitor** — the two remaining production risks, both requiring account-level action. (The uptime monitor now has a truthful `/health` — 200/503 by DB liveness — to watch.)
- **Login-time-only commercial enforcement** — a cancelled tenant's existing token remains valid for up to 4 hours (accepted in ADR-0008).
- **`FlowMES-Enterprise/`** should be removed.

---

## 9. Engineering metrics

| Metric | Value |
|---|---|
| Commits on `master` | **197** |
| Pull requests merged | **137** (#1–#137) |
| Contributors | 1 (Ashwin Vardharajan) |
| Elapsed time | **45 days** (2026-06-04 → 2026-07-19) |
| Branches created | 118 (feature-per-PR, deleted on merge) |
| Reverts | **1** (PR#3 tenant enforcement) |
| ADRs | **8** |
| Database tables | 48 |
| API endpoints | 192 |
| AI read-models (`build_*`) | 31 across ~28 modules |
| AI agents | 5 |
| Domain event types | 4 (+ the bus) |
| Backend test suites | 40 |
| Frontend components | 73 |
| Navigation views | 42 |
| Largest file | `backend/main.py` — 1,675 lines (peaked at 4,274 before the ADR-0009 route-module extraction), touched by 92 commits |
| Commit-type mix (conventional era) | 51 `feat` · 17 `fix` · 6 `docs` · 3 `perf` · 2 `test` · 1 `refactor` |
| Deployment targets | Railway (backend), Vercel (`app.marx8.com`) |
| Live tenants | DEFAULT (demo), GMATS (pilot), APEX (second tenant) |

**Delivery inflection:** 56 commits in the 37 days before the first ADR (~1.5/day); **141 commits in the 7 days after (~20/day)** — a ~13× change in rate.

---

## 10. The story of AMP

AMP began on 4 June 2026 as **FlowMES** — a FastAPI backend, a Next.js dashboard, and a PLC simulator so a factory could be demonstrated without a factory. The problem was mundane and real: an SME manufacturer's machine downtime lives on a clipboard, and nobody knows the plant's OEE until the month is over.

The first six weeks were not glamorous. They were CORS preflight failures, a start command Railway wouldn't accept, a search bar that took three commits to accept keystrokes, and a `Dockerfile` that silently hijacked the build system so that new endpoints 404'd while the old deploy kept happily serving. What those weeks produced, besides a working product, was a set of diagnostic instincts — *check the deployed OpenAPI spec against the code* — that the project still uses.

The turn came on 22 June, when **GMATS Machineries** became the first pilot. A second company's data now sat in the same database as the demo, and multi-tenancy arrived the way it usually does: as a patch. A `tenant_code` column here, a filtered query there, a dictionary mapping usernames to tenants. It worked, and it was quietly accumulating the kind of risk where one forgotten `WHERE` clause becomes a data breach.

By the end of June the product had a platform layer — licensing, branding, audit, health — and an AI copilot that did nothing at all unless an API key was present. That last decision, made almost casually, turned out to be one of the most durable in the codebase: **AI was optional from the very first line of it.**

On 11 July, FlowMES became **AMP**. The rename was a statement of intent — from *manufacturing execution* to *autonomous manufacturing platform* — and it would have been marketing, except that two days later the architecture changed to match.

**13 July is the hinge of this repository.** Commit `30fa5fc` accepted ADR-0001 (a domain event bus) and ADR-0002 (tenant-scope the core domain). In the 37 days before that commit, the project produced 56 commits. In the 7 days after, it produced 141. Writing down *why* made the next hundred decisions almost free.

The event bus went in first, behaviour-preserving: work-order completion's inline BOM movement became a subscriber, and every domain event started landing in an append-only `event_log` — the substrate the AI would later reason over. Then tenant scoping, which promptly produced the repository's only revert and its most valuable lesson. The enforcement middleware used Starlette's `BaseHTTPMiddleware`, which buffers request bodies and runs endpoints in a separate task; it deadlocked every POST in production, and it had passed every unit test because the tests never touched the HTTP layer. The fix was a pure-ASGI middleware; the *real* fix was a standing rule, written into ADR-0002 and the deploy checklist: **middleware and auth changes get smoke-tested against a running server.**

With a stream to listen to and isolation that couldn't be forgotten, the AI platform followed within a day — not as a chatbot, but as a package of capabilities subscribing to factory events. Then agents: first one that *acted* (critical predictive risk opens a maintenance task), then, forty-eight hours later, the realisation that an agent which can draft a purchase order needs a leash. ADR-0005 introduced propose-then-approve and the `AgentAction` record — one row that is simultaneously an audit log and an approval queue. That single modelling choice is why the agent fleet later grew metrics, trends, a roster, and an ROI view without any new storage.

Mid-July produced the pattern that defines the codebase. The Machine Health twin was built as a *read-model* — a pure function composing existing tables into one snapshot, with no new storage — and it worked so well that ADR-0007 named the pattern and standardised it. What followed was the fastest stretch in the project's life: 27 commits in a single day, nearly all new decision surfaces, because a new one cost a function, an endpoint, a component and a test — never a migration.

Then the product started talking. The morning briefing ranked what needed attention; the Escalation agent began raising the top alert on its own; the copilot learned to answer plant questions from the read-models with no API key at all. That last one mattered commercially more than any LLM integration would: the AI demos for free, works offline, and cannot invent a machine that doesn't exist.

The final two days compressed a company's worth of SaaS plumbing into a lifecycle: onboard a tenant with a seeded factory, provision its admin in one click, gate its features by the plan you sold it, count down its trial, block it when it cancels, and purge it cleanly when it leaves. Every rule enforced at a chokepoint; every chokepoint proven on production with a throwaway tenant. That verification found two bugs no test had: a globally-unique `item_code` that would have given every customer after the first an empty factory, and a purge that died on Postgres foreign keys because SQLite hadn't been enforcing them.

The very last stretch connected a real LLM — and turned into a small clinic on integrating a service you don't control. A credit-less key surfaced a raw API error into what would have been a customer's chat window, so the copilot learned to degrade to rules. A free-tier provider went in behind the same interface. A founder-only diagnostic was added to explain *why* the copilot had fallen back — and immediately caught a retired model name, then a preview model with zero quota. Each fix made the system less dependent on any particular vendor's naming. And when the dashboard was caught claiming 79,970 minutes of downtime in a seven-day week, the simulator that had made every demo possible since day one was finally calibrated to obey physics.

AMP today is a modular monolith with an event backbone, an AI platform of 31 read-models and 5 supervised agents, complete multi-tenant commercial machinery, and 56 test suites — built by one person in 45 days, deployed continuously, and documented by nine architecture decision records that explain not just what was chosen but what was rejected and why. The `main.py` monolith that was the standing structural debt at the time of first writing has since been peeled into fifteen domain route-modules (ADR-0009), taking it from 4,274 lines to 1,675 with the route-count invariant held at every step.

The pattern worth carrying forward is the one visible in every era: **when a property must always hold, put it in exactly one place requests cannot avoid** — and when production teaches you something a test could not, write the lesson down where the next engineer will trip over it.

---

*Compiled 2026-07-19 from repository evidence at `3875b20`. Every claim traceable to a commit, ADR, file or config in this tree.*
