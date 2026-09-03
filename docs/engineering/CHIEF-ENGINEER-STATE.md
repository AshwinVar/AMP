# AMP — Chief Engineer State

> Handover file. A new session should be able to read only this and continue.
> Keep it short. Update it at the end of every completed task.

**Updated:** 2026-09-01
**Master SHA examined:** `ffc21f0` (588 commits)
**Production SHA:** `0eb94ca` — `/health` `{"status":"ok","database":"ok","schema":"ok"}`

---

## LAST COMPLETED TASKS

| Task | Priority | Status |
|---|---|---|
| MQTT→WebSocket bridge: `asyncio.run` on a sync callee raised `ValueError` every message; delivery ran on a throwaway event loop | P1 | fixed, tested |
| MQTT ingest published no domain events — machine-reported breakdowns never reached the bus, so the Escalation agent was blind to them | P1 | fixed, tested |
| Founder handbook verified against master by 10 subsystem specialists: 385 claims confirmed, 20 misleading | P8 | verification done, affected sections synced |
| `/machine-health` N+1: 3 queries per machine on a 3s poll (607 statements at 200 machines) — **measured first**, then batched to a flat 10 | P4 | fixed, tested, measured |

---

## KNOWN P0 / P1

**None open.** Both P1 defects above are fixed on this branch.

---

## VERIFIED DEFECT BACKLOG

| # | Finding | Verified? | Class | Priority |
|---|---|---|---|---|
| 1 | MQTT→WS `asyncio.run(None)` | YES | BUG | **FIXED** |
| 2 | MQTT publishes no `DowntimeStarted` | YES | BUG | **FIXED** |
| 3 | 4 of 7 `SHARE_*` grants have no enforcement point | YES | INTENTIONAL — no read path exists, so nothing leaks. Consent recorded ahead of the feature | not a defect |
| 4 | `oem_claims.accept` sets `status="Assigned"` by bulk UPDATE, bypassing `oem_service.transition` | YES | INTENTIONAL — the conditional UPDATE's row count *is* the security decision; routing it through the state machine would break claim atomicity | document, don't change |
| 5 | Dashboard polls `fetchAll` every 3s; 46 requests/round | **MEASURED** via `dashboard_perf.py` | PERFORMANCE — the N+1 in `/machine-health` was the real cost (607→10 queries). The remaining 45 endpoints are flat at 1-5 queries each | **largest win taken**; further work needs HTTP-level measurement (`loadtest.py`, never run) |
| 6 | Copilot provider coupling | YES | Already has `AI_PROVIDER` anthropic/gemini branching — if/else, not a clean interface | P6 |

---

## PRODUCT COMPLETION BACKLOG

- **`release_installation`** sets `status="Sold"` from any state including `Active` — verify whether the state machine should govern it. UNVERIFIED, needs a look.
- **`manage_models` / `manage_branding`** capabilities are declared but no route requires them; there is no API to create a machine model (catalogue is seeded). Gap, not a bug.
- **Load testing has never been run.** `docs/PERFORMANCE.md` and `load/thresholds.js` both say so in their own opening lines. Nothing in CI runs it.
- **Only 3 of 193 backend suites use a real HTTP client**; the rest call route functions directly. Fast and legitimate, but it is not end-to-end API testing and should not be described as such.

---

## AI ROADMAP PHASE

**Phase 1 — provider abstraction: DONE (2026-09-01).**
`ai_copilot.PROVIDERS` is now a registry of `AIProvider` objects
(`AnthropicProvider`, `GeminiProvider`); `_provider`, `_current_model`,
`_ai_enabled` and `_ask_llm` all consult it instead of each branching on the same
two strings. A third provider is one class + one tuple entry, pinned by
`test_ai_provider_registry.py` with a stub provider. **One deliberate behaviour
change:** an `AI_PROVIDER` naming no registered provider now selects nothing and
logs why — it used to fall through to auto-detect, so a typo while moving OFF the
paid tier kept silently billing Anthropic.

Previously recorded state (now superseded):
`ai_copilot.py` already branches on `AI_PROVIDER` (`anthropic` | `gemini`) with per-provider model defaults and a `urllib` call — no SDK. It is if/else branching rather than an `AIProvider` interface, but it is **not** hard-coupled to one vendor. A clean interface is worthwhile; it is not urgent.

Phases 2–6 not started. Note before starting Phase 2: the LLM is already read-only and is handed a pre-built text context (`_build_factory_context`), which is the correct shape — do not rebuild it.

---

## REAL OEM INPUT REQUIRED

- Which of `SHARE_ALARMS` / `SHARE_TELEMETRY` / `SHARE_MAINTENANCE_HISTORY` / `SHARE_DOWNTIME` matters commercially, and what the data must look like. Alarm codes are meaningless without the manufacturer's fault dictionary.
- Whether warranty runs from despatch, delivery, installation or commissioning (`warranty_months` is declared and unused for exactly this reason).
- Any real PLC/controller before touching industrial protocol drivers.

---

## MANUAL ACTION REQUIRED

- **Production has no MQTT broker.** `FastAPI MQTT connection error: ConnectionRefusedError(111)` on every boot; `MQTT_BROKER` is unset. The live-telemetry path therefore does not run in production today. The bridge fix above is correct but latent until a broker exists.
- `RESEED_FACTORY` is consumed at `274946` and can be deleted from Railway.
- `docs/training/` is **untracked** — 159 KB of handbook that is not in git and would be lost with the working tree.

---

## NEXT 5 ENGINEERING TASKS

1. **`release_installation` lifecycle** — verify, then either route through `transition()` or document why not.
2. **AI Phase 1** — extract an `AIProvider` interface behind the existing `AI_PROVIDER` branching, no behaviour change, with tests.
3. **AI Phase 5 before 3** — build the evaluation harness (deterministic questions with known answers) *before* adding tools, so tool work can be measured.
4. **Run `loadtest.py` once** — it has never been executed; it needs a scratch PostgreSQL and gives the first HTTP-level numbers.
5. **Sync remaining training-doc drift** (18 of 20 misleading items still unsynced; MQTT/events/twin sections are done).

---

## CONVENTIONS THAT BIND FUTURE SESSIONS

- Failing test first; confirm it fails for the expected reason.
- Mutation-test every security guard; investigate every surviving mutation.
- eslint baseline is **exactly 134**.
- Schema change ⇒ model + Alembic migration + fresh-schema test + upgrade test + PostgreSQL verification.
- Never weaken a test to make a change pass.
