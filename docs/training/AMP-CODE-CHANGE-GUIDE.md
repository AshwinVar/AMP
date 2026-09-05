# AMP — Code-Change Guide

*"I want to change X — where, and in what order?" Verified at commit `0eb94ca`. Every recipe is additive-first: prefer adding files + registering them over editing existing domains.*

## The universal order
**data → migration → domain logic → events → API → permissions → tenancy → read-model → UI → tests → CI → deploy.** Skip the steps a given change doesn't need.

## Golden rules
- **Never** hand-write a `WHERE tenant_code=?` for a scoped model — the ORM hook does it. Do it explicitly only for the non-scoped tables (`AgentAction`, `EventLog`, `User`, GMATS, OEM).
- **Never** edit another domain to react to it — add a **subscriber** to its event.
- **Never** alter a production table with `create_all` — write an **Alembic migration**.
- **Always** smoke-test middleware/auth changes on a *running* server (`boot + POST /login`), per ADR-0002.
- **Always** ship a test; for a security rule, ship a `mutate_*.py` too.

---

## Recipe: add a field to an existing entity (e.g. `Machine.location`)
1. `backend/models.py` — add the column to the model.
2. `cd backend && alembic revision -m "machine location"` → edit the new `alembic/versions/00XX_*.py` (`op.add_column(...)`); the boot patches (`_ensure_column`) are dev-only.
3. `backend/schemas.py` — add the field to the relevant `*Response`/`*Create`.
4. `backend/<domain>_routes.py` — accept/return it if needed.
5. `frontend/components/<Domain>Section.tsx` — render/edit it.
6. `backend/test_<domain>_routes.py` — extend.

## Recipe: add an API endpoint
1. `backend/<domain>_routes.py` — add `@router.get|post|patch(...)`.
2. Add `Depends(require_roles([...]))` if it writes.
3. `backend/schemas.py` — request/response shapes if new.
4. `frontend/lib/api.ts` is generic — just call `apiGet/apiPost(...)` from the component.
5. `backend/test_<domain>_routes.py` — cover success + 401/403/404/409.

## Recipe: add a whole module (e.g. Energy)
1. `backend/modules.json` — register the `energy` pack (id, label, routes, plans) → drives nav + licensing.
2. `backend/models.py` — `EnergyReading(... tenant_code ...)` + Alembic migration.
3. `backend/tenancy.py` — add `EnergyReading` to `SCOPED_MODELS` **and** `CORE_TENANT_TABLES` (keep counts in lockstep).
4. `backend/energy_routes.py` — `router = APIRouter(prefix="/energy")` + endpoints + `require_roles`.
5. `backend/main.py` — `import energy_routes` + `app.include_router(energy_routes.router)`.
6. *(optional react)* `backend/ai/subscribers.py` — subscribe to `ProductionCompleted` etc.
7. `backend/ai/energy.py` — a `build_energy_summary(db, tenant)` read-model + a GET in `read_model_routes.py`.
8. `frontend/components/EnergySection.tsx` + `renderSection("energy", <EnergySection/>)` in `app/dashboard/page.tsx` + gate in `lib/modules.ts`.
9. `backend/test_energy_routes.py` + `mutate_energy.py` + an isolation assertion.

## Recipe: add a telemetry signal (e.g. vibration)
1. Publisher/edge sends it in the MQTT JSON.
2. `backend/mqtt_service.py` (`on_message`) — parse + persist (to `IoTTelemetry` or a `Machine` column + migration).
3. `backend/machine_status.py` — if it should influence status/utilization.
4. Surface via `ai/twin.build_machine_detail` → `MachineDetailDrawer.tsx`.

## Recipe: add / change a domain event
1. `backend/events.py` — a new frozen dataclass (`tenant_code`, `event_type`, `event_version`).
2. Publish it in the producing `*_routes.py` (`event_bus.publish(evt, db)`).
3. Add subscribers in `subscribers.py` / `ai/subscribers.py` + register in the `register()`.
4. `backend/test_event_bus.py` / domain test.

## Recipe: add / tune an AI agent
1. `backend/ai/agents.py` — a handler; tune thresholds at the top constants.
2. Register on the event in `ai/agents.register()`; route the proposal through `_propose()` + `AgentAction`.
3. Autonomy: `AgentPolicy` / `AUTO_APPROVE_AGENTS` (default `reorder`).
4. `backend/test_agents.py` + `mutate_*` for any safety rule.

## Recipe: change OEE
- Canonical contract (windowing, has_data, coverage, None-vs-0): `backend/oee_contract.py`.
- Pooled + per-record engines: `backend/analytics_engine.py` (`pooled_oee_from_sums`, `calculate_oee_from_record`).
- £ money story: `TenantConfig.unit_value_gbp` + `tenancy.tenant_unit_value`.
- Tests: `test_oee_contract.py` (golden datasets) + `mutate_oee_contract.py`.

## Recipe: change inventory / BOM
- Reorder trigger: `inventory_routes.py create_inventory_transaction` (`InventoryLow`).
- Reorder draft qty: `ai/agents.py draft_reorder_on_inventory_low`.
- BOM data: `PATCH /bom/{id}` (Admin). BOM resolution rules: `bom.py resolve`.

## Recipe: change roles / permissions
- Gate an endpoint: `require_roles(["Admin","Supervisor"])`.
- Add a role: `VALID_ROLES` in `users_routes.py` + view-gating sets in `frontend/lib/modules.ts`.
- Agent-approval roles: `approvals.APPROVER_ROLES`.

## Recipe: change tenancy (rare, load-bearing)
- New scoped table: `tenant_code` column + add to `SCOPED_MODELS` **and** `CORE_TENANT_TABLES` + migration.
- Preview/authorization rule: `tenancy.effective_tenant` (keep the DEFAULT+Admin fail-closed check).
- Verify with `audit_isolation.py`, `test_tenant_isolation_http.py`.

## Recipe: change the OEM platform
- Consent grants/visibility: `oem_sharing.py` (allowlist copy-in; never redact-out).
- Claim code/expiry/atomicity: `oem_claims.py` (preserve: only factory-accept sets `factory_tenant_code`).
- Lifecycle/service: `oem_service.py` (`LIFECYCLE`, `service_state`, `COMMISSIONING_CHECKS`).
- Capabilities/roles: `oem_auth.ROLE_CAPABILITIES`.
- Tests: `test_machine_claim.py`, `test_oem_service_consent.py`, `mutate_oem_sharing.py`.

## Recipe: add a real industrial protocol (the OEM edge-agent work)
1. `backend/industrial_adapters.py` — a new `ProtocolAdapter` subclass implementing `read()` with a real client library (add it to `requirements.txt`).
2. Register it in `get_adapter()` (currently returns `SimulatorAdapter` unconditionally).
3. Map signals via `PlcSignalMapping` → `machine_status`.
4. Tests + a real-device integration check.

## Recipe: change the frontend
- New dashboard card: `components/<X>Snapshot.tsx` (fetch on 30s interval, `return null` when empty) → mount in `app/dashboard/page.tsx`.
- New page: `frontend/app/<route>/page.tsx` (folder = URL; `[param]` for dynamic).
- New section + nav: component + `renderSection(...)` + entry in `modules.json` (preferred) or `lib/modules.ts` + role gate.

## Recipe: database migration (production)
```bash
cd backend
alembic revision -m "describe change"     # creates alembic/versions/00XX_*.py
# edit upgrade()/downgrade()
alembic upgrade head                       # apply locally
# CI's migrations job asserts the autogenerate diff is empty before merge
```
Deploy runs `migrate.py` before serving; `/readiness` gates traffic.

## Recipe: change CI / deploy
- CI jobs: `.github/workflows/ci.yml` (backend, migrations, coverage, frontend, e2e).
- Backup/retention: `backup.yml`, `retention.yml`, `retention.py`.
- Railway: `backend/railway.toml` (`preDeployCommand`, `healthcheckPath`). Keep the start command byte-identical across `Procfile`/`Dockerfile`/`railway.toml`. Do **not** add `--workers`.
- Env vars: set in Railway/Vercel dashboards; `SECRET_KEY` + `DATABASE_URL` are mandatory.
