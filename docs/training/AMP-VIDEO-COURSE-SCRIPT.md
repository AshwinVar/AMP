# AMP — Video Course Script

> **On video generation:** I could **not** generate a narrated video or screen recording — that capability isn't available to me in this environment, and I won't claim otherwise. What follows is a **shoot-ready script**: for each episode, the **narration** (read aloud), the **screen actions** (what to record), the **code to open** (exact files/lines at commit `0eb94ca`), and the **diagram** to show (pull the Mermaid from the Handbook). Record it with any screen recorder (OBS, Loom) reading the narration over the actions.

**Format per episode:** 🎙️ NARRATION · 🖥️ SCREEN · `</>` CODE · 📊 DIAGRAM. Total ≈ 3.5–4 hours across 12 episodes.

---

## Episode 1 — What AMP Is (12 min)
🎙️ "Factories drown in information a human can't hold. AMP is the operating system that captures it, remembers it, shows it live, and acts on it. The MES everyone thinks we are is just the first app." Cover the reality→awareness gap, the OS analogy, the two user worlds (factory vs OEM), and the honest edges (no ML, simulated PLC).
🖥️ Open `app.marx8.com` landing page; log in; pan the dashboard; open the OEM portal.
`</>` `frontend/app/page.tsx` (the pitch), `backend/main.py:357-445` (the app being assembled).
📊 Handbook Ch.1 "AMP from the top" flowchart.

## Episode 2 — The Evolution (15 min)
🎙️ Walk the eras: FlowMES birth → deploy war → first customer (multi-tenancy by necessity) → the 2026-07-13 ADR inflection → read-model explosion → proactive plant → SaaS → modularization → security/DevOps → OEM. Emphasise the one revert (the PR#3 deadlock) and the rule it created.
🖥️ Scroll `docs/ENGINEERING-HISTORY.md`; open `docs/adr/README.md` and show the 19-row table.
`</>` `git log --oneline --reverse | head -40`; open `docs/adr/0002` postmortem section.
📊 Handbook Ch.2 timeline.

## Episode 3 — Repository Tour + Boot (18 min)
🎙️ "175 backend files are five layers, not a pile." Foundation → assembler → domain apps → backbone → tests. Then the boot: decide schema owner → mount 27 routers → build middleware → schema-guard halt → MQTT + sim + seed.
🖥️ File-tree walk of `backend/` and `frontend/`; then scroll `main.py` top to bottom.
`</>` `database.py`, `main.py:82-89` (subscriber wiring), `:121-136` (`_MANAGED`), `:361-445` (routers), `:534-556` (schema halt).
📊 Handbook Ch.4 safe-start flowchart.

## Episode 4 — Login, Click to Database (14 min)
🎙️ Define HTTP/JSON/hashing/JWT/authn-vs-authz as you go. Trace the click through bcrypt to a signed token and back; explain *stateless*.
🖥️ Open devtools Network tab; perform a login; show the `POST /login` request/response and the stored token.
`</>` `frontend/app/login/page.tsx`, `core_routes.py:85-125`, `security.py:23-31`, `auth.py:77-90`.
📊 Handbook Ch.5 login sequence diagram.

## Episode 5 — Database + Multi-tenancy (20 min)
🎙️ From zero: table/row/PK/FK/index/transaction; ORM; then the 57 tables by domain; then the star: `tenant_code` + the two ORM hooks that make Factory A blind to Factory B. Tell the `is_active` outage as the lesson (liveness ≠ correctness).
🖥️ Open a DB client; show `machines` rows with different `tenant_code`; run the same query as two tenants (or show the founder company-switcher).
`</>` `models.py:12-40` (Machine/Downtime), `tenancy.py` (`effective_tenant`, `install_scoping` hooks), `schema_guard.py` docstring.
📊 Handbook Ch.6 ER slice + Ch.17 tenancy flowchart.

## Episode 6 — The Event Bus + Read Models (18 min)
🎙️ The noticeboard analogy; the four events; walk `ProductionCompleted → BOM subscriber`; then read-models as pure projections that can't go stale; `pulse` over `twin`+`impact`.
🖥️ Complete a work order in the UI; show inventory move + a proposed reorder appearing.
`</>` `events.py` (bus + dataclasses), `subscribers.py:12-83`, `read_model_routes.py` (thin delegations), `ai/pulse.py`.
📊 Handbook Ch.11 event flowchart + Ch.10 before/after.

## Episode 7 — AI + Agents (the honesty episode) (16 min)
🎙️ State the three buckets out loud: rules (95%), one optional LLM, **zero trained ML**. Then the 5 agents; observe→propose→approve→execute; the `approvals.py` gate re-checking the DB.
🖥️ Show the Mission Control insights feed; approve/reject an agent action in the Approvals inbox.
`</>` `predictive_engine.py` (threshold weights), `ai/agents.py` (constants + `_propose`), `approvals.py:82-160`, `ai_copilot.py:_ask_claude` (the one LLM call).
📊 Handbook Ch.13 oversight flowchart.

## Episode 8 — Real-time: MQTT + WebSocket (16 min)
🎙️ MQTT postal analogy; topic-based tenant identity `(tenant,site,name)`; then the WebSocket that authenticates before accepting and filters by tenant. Be explicit: pipeline real, PLC drivers simulated.
🖥️ Run `mqtt_machine_publisher.py`; watch the dashboard update live; open devtools WS frames.
`</>` `mqtt_service.py:on_message`, `mqtt_identity.py`, `live_ws.py` (ConnectionManager), `ws_auth.py:resolve`, `industrial_adapters.py:get_adapter` (the simulator seam).
📊 Handbook Ch.15 MQTT flow + Ch.16 WS sequence.

## Episode 9 — The OEM Platform (20 min)
🎙️ The AERON compressor example; why OEM ≠ tenant (sentinel `OEM:<code>`); the factory-controlled claim (OEM proposes, factory disposes); the atomic accept.
🖥️ Walk the OEM registry; generate a claim; accept it as a factory Admin from `/claim/<code>`; show it appear in the fleet.
`</>` `oem_auth.py:sentinel_tenant`, `oem_claims.py:accept` (the two UPDATEs), `connected_equipment_routes.py:accept_claim`.
📊 Handbook Ch.19 ER diagram + Ch.20 claim sequence.

## Episode 10 — OEM Consent + Service (14 min)
🎙️ The 7 grants, default-deny, allowlist copy-in; trace a toggle-off; the hour-meter bisection leak and its two lessons; the `hours % interval` service bug.
🖥️ Toggle Operating Hours off as a factory; refresh the OEM fleet; show the field vanish (not zero).
`</>` `oem_sharing.py:fleet_row` (copy-in), `oem_service.py:service_state`.
📊 Handbook Ch.21 consent sequence + Ch.22 lifecycle state diagram.

## Episode 11 — Testing + CI/CD + Deploy (16 min)
🎙️ The test taxonomy; mutation testing explained (break the source, expect red); the 5 CI jobs; migrations-before-serve; backup with restore drill.
🖥️ Run one `python test_oee.py`; run `mutate_oee_contract.py` and show `caught`; open a GitHub Actions run.
`</>` `mutate_oee_contract.py`, `.github/workflows/ci.yml`, `backend/railway.toml`, `migrate.py`.
📊 Handbook Ch.25 pipeline + Ch.27 "what runs where".

## Episode 12 — How to Change AMP + Wrap (18 min)
🎙️ The additive change model; walk "add vibration" then "add an Energy module" against the checklist. Close with "AMP in 5 minutes" and the 20 things.
🖥️ Live-code a trivial new read-model + GET + Snapshot card end to end (or narrate the steps against the files).
`</>` `AMP-CODE-CHANGE-GUIDE.md` recipes; `read_model_routes.py`; a `*Snapshot.tsx`.
📊 Handbook Ch.28 Energy-module flowchart.

---

## Production notes
- **Record at the SHA in the docs** (`0eb94ca`) so line numbers match; if the code has moved, re-derive from the Module Map.
- **Redact secrets** on screen (`.env`, tokens, the founder password).
- **Keep the honesty beats** (Episodes 7, 8, 10, 11): no ML, simulated PLC, `is_active` gap — they build credibility with technical viewers.
- Suggested order for a **new hire**: 1→12 in sequence. For an **investor/CTO**: 1, 2, 5, 7, 9, 11. For an **OEM engineer**: 1, 8, 9, 10.
