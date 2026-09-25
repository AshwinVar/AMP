# AMP — QA Framework & Safety Audit

**Phase 1: Repository discovery — existing test framework and safety audit**
Repository: `C:\Users\ashwi\AMP` (branch `master`)
Date: 2026-07-23. Supersedes the earlier draft (repo has advanced ~80 PRs since; `main.py` modularized, test count grew 60→78).
Scope: **Read-only inspection.** No source, dependency, migration, data, or deployment changes were made. **No tests were executed** — import-time database safety is not provable for the app-importing suites (see §6, §21).
Method: static analysis only (`git ls-files`, file reads, AST-free grep, JSON parsing). Database target classified without reading any secret value.

---

## 1. Executive summary

AMP has a **large and, in parts, genuinely strong backend test suite** — **78 test files, 239 test functions** covering events, tenant scoping, read-models, AI agents, auth, and a broad band of pure-arithmetic OEE/KPI logic — but it is wrapped in a **fragile, unautomated, and operationally unsafe harness**, and its apparent breadth is inflated by a large block of tests that assert almost nothing behavioural.

Three findings dominate:

1. **Import-time database side effect (critical, unchanged since the last audit).** `database.py` runs `load_dotenv()` → `os.environ["DATABASE_URL"]` → `create_engine(...)` at import with **no fallback**, and `main.py` executes `Base.metadata.create_all(...)` plus `ALTER TABLE`/`CREATE INDEX` DDL **at module scope** (lines 79, 124–137) — followed by `tenancy.install_scoping()`. **27 of 78 test files `import main`**, so a naïve `pytest` collection issues schema DDL against whatever `DATABASE_URL` resolves to. Locally, that resolves to the developer's **localhost PostgreSQL** (`.env`, classified without reading credentials). CI is safe only because it injects a disposable `DATABASE_URL=sqlite:///./ci.db`. A second, narrower DDL-at-import hazard lives in `factory_simulator.py:22` (real-engine `create_all` on import), reached by `test_sim_calibration.py`.

2. **CI runs scripts, not a test runner.** The workflow loops `for f in test_*.py; do python "$f"; done`, executing only each file's `__main__` block. Any `def test_*` **not manually wired into `__main__` never runs** and never fails CI. There is **no coverage measurement, no backend linting, no type-checking**, and the **frontend has zero automated tests** — only `next build` runs; even `npm run lint` is defined but not invoked.

3. **Breadth is partly illusory.** **21 of the 78 files (37 of 239 functions) are route "registration guards"** — they `import main`, walk `main.app.routes`, and assert path ownership and that a dependency is *attached*; event coupling is checked by `inspect.getsource(...)` substring match. **No route suite issues a real request** (`TestClient` appears in zero test files), executes a handler, or proves a 401/403/cross-tenant denial through the ASGI stack. They guard the refactor's module boundaries — useful — but contribute close to zero behavioural coverage.

The healthiest recent additions are the **pure-logic suites** (`test_analytics_engine.py` — 11 tests, `test_ai_twin_oee.py` — 5 tests): no DB, `SimpleNamespace` stubs, locking the pooled-OEE/KPI arithmetic. These are exactly the right shape and should be the model for expansion.

**Headline ratings: HIGH operational-safety risk, MEDIUM coverage-quality risk.** No production code changes are recommended in this phase; a small, low-risk remediation (§23) makes the whole suite pytest-safe and unlocks coverage/lint/type gates.

---

## 2. Existing backend testing framework — **Existing but not automated**

| Attribute | Finding |
|---|---|
| Language / runtime | Python 3.11 (CI `setup-python@v5`) |
| Web framework | FastAPI `0.136.1` |
| ORM | SQLAlchemy `2.0.49` |
| Prod DB driver | `psycopg2-binary 2.9.12` (PostgreSQL) |
| Auth | `python-jose[cryptography] 3.5.0` (JWT HS256), `bcrypt 4.2.1` |
| Messaging | `paho-mqtt 2.1.0`, `websockets 16.0` |
| Config | `python-dotenv 1.2.2` (`load_dotenv()` at import) |
| **Test framework** | **None declared.** `pytest` is **not** in `requirements.txt`. Tests are plain scripts (`python test_x.py`, exit 0 = pass). Docstrings claim pytest-collectable, but pytest is neither pinned nor installed by CI. |
| pytest config | **Absent** — no `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`. |
| Shared fixtures / factories | **Absent** — no `conftest.py` anywhere. Each suite hand-rolls its own `create_engine("sqlite://")` + `Base.metadata.create_all` and inline seed helpers. No factory library (`factory_boy`/`faker`). |
| Coverage / lint / type tooling | **Absent** — no `pytest-cov`, `coverage`, `ruff`, `flake8`, `mypy`, `black`. |

**Test-isolation pattern — three classes (counts are exact):**

| Class | Count | Behaviour | Safe to collect? |
|---|---|---|---|
| Self-isolating | 44 | Build own `sqlite://` engine, never import `main` | **Yes** |
| App-importing | 27 | `import main` → import-time DDL against the resolved DB (3 of these also build a local sqlite engine for the test body itself) | **No** (needs env override) |
| Pure-logic | 7 | Neither import `main` nor touch a DB (`test_analytics_engine`, `test_ai_twin_oee`, `test_duration`, `test_live_ws`, `test_plan_gate`, `test_predictive_engine`, `test_recovery`) | **Yes** |

One self-isolating suite is a hidden exception: `test_sim_calibration.py` builds its own in-memory engine **but** `from factory_simulator import tick_production`, and importing `factory_simulator` runs `Base.metadata.create_all(bind=engine)` against the **real** engine (`factory_simulator.py:22`). So it is DDL-exposed despite not importing `main`.

## 3. Existing frontend testing framework — **Missing**

| Attribute | Finding |
|---|---|
| Framework | Next.js `16.2.6`, React `19.2.4` / `react-dom 19.2.4` |
| Language | TypeScript `^5` (`strict: true`) |
| Charts / styling | `recharts ^3.8.1`, Tailwind CSS `^4` |
| Lint | ESLint `^9` + `eslint-config-next` (`npm run lint` → `eslint`) |
| **Test framework** | **None.** No Vitest, Jest, Playwright, Cypress, or `@testing-library`; no `test` script in `package.json` (`dev`, `build`, `start`, `lint` only). |
| Test files | **0** (`*.test.*` / `*.spec.*` — none outside `node_modules`). |
| CI involvement | Build only (`npm ci` + `npm run build`). **Lint not run in CI.** |

Frontend testing is entirely **Missing**. ~90 components and the whole dashboard have no component/integration/E2E coverage.

## 4. Exact available test commands

**Backend, per-suite (as designed):**
```
cd backend && python test_<name>.py       # exit 0 = pass; runs the file's __main__ block
```

**Backend, CI loop** (`.github/workflows/ci.yml`, job `backend`, `working-directory: backend`, `env: DATABASE_URL=sqlite:///./ci.db`):
```bash
for f in test_*.py; do
  if python "$f"; then echo "PASS $f"; else echo "FAIL $f"; failed="$failed $f"; fi
done
[ -n "$failed" ] && exit 1
```
plus `python -m compileall -q backend` and the boot check `python -c "import main; print(len(main.app.routes))"`.

**Frontend, CI** (job `frontend`, Node 20): `npm ci` → `npm run build`.

**Available but NOT wired anywhere:** `pytest` (implied by docstrings; not installed/pinned) · `cd frontend && npm run lint` (defined, absent from CI) · `backend/e2e_sim.py` (manual simulator; **defaults to the production host** — see §21).

## 5. Number of test files and collected tests

- **Backend: 78 `test_*.py` files, 239 `def test_` functions.** Frontend: **0**.
- All 78 files contain an `if __name__ == "__main__":` runner (confirmed) — this is the *only* execution path CI uses (see §8).
- "Collected tests" via pytest is **not measured** — pytest is not installed, and collection is unsafe (§21), so no `--collect-only` count exists. The 239 figure is the static `def test_` count; the number actually executed by CI is **≤ 239** and unverifiable, because CI runs `__main__` blocks, not collection (§8).

**Breakdown by kind:**

| Kind | Files | Functions | Notes |
|---|---|---|---|
| Route registration guards | 21 | 37 | Ownership/dependency-attachment only; no requests (§16) |
| Read-model / AI-domain arithmetic | ~30 | ~90 | Many strong numeric asserts; SQLite-backed or stubbed |
| Pure-logic (no DB) | 7 | ~50 | Healthiest; `test_analytics_engine` 11, `test_recovery` 12 |
| Auth / RBAC / tenancy / lifecycle | ~8 | ~40 | `test_onboarding` 13, `test_tenancy` 5, `test_plan_gate` 6 |
| Events / agents | 3 | 22 | `test_agents` 11, `test_ai` 10, `test_event_bus` 1 |
| WebSocket / smoke / health | 3 | 5 | `test_live_ws` 1, `test_api_smoke` 1, `test_health` 3 |

## 6. Test database and fixture strategy — **Unsafe (default) / Missing (shared fixtures)**

Reported as **classification only — no usernames, passwords, tokens, or full URLs read.**

| Source | Engine | Host class | Environment | Disposable? |
|---|---|---|---|---|
| `backend/.env` (developer) | PostgreSQL | `localhost`, port 5432 | Development | **No** — real local Postgres |
| CI (`ci.yml` env) | SQLite `./ci.db` | Local file | Test/CI | **Yes** |
| `backend/flowmes.db` (**tracked in git**, 57 KB) | SQLite | Local file | Legacy artifact | Obsolete |
| `backend/ci.db` (untracked, 708 KB) | SQLite | Local file | CI scratch | Yes |

**Facts:**
- No `test`/`dev`/`prod` branch anywhere: no `APP_ENV` / `ENVIRONMENT` / `TESTING` variable exists in the codebase. `database.py` has one code path.
- `load_dotenv()` does **not** override an already-exported var, so an explicit `DATABASE_URL` in the shell wins; **absent one, the developer `.env` (localhost Postgres) is used.**
- `backend/.env` is git-ignored and untracked (verified). Only key *names* were read: `DATABASE_URL, SECRET_KEY, ALLOWED_ORIGINS, MQTT_BROKER, MQTT_PORT, MQTT_TOPIC`. No values printed. `MQTT_BROKER`/`ALLOWED_ORIGINS` classify as localhost.
- **Fixture strategy: none shared.** Each self-isolating suite repeats the same `create_engine("sqlite://", connect_args={"check_same_thread": False})` + `create_all` boilerplate and inline `_seed()` helpers — 44 near-duplicate bootstraps. A single `conftest.py` fixture would replace all of them.

**Verdict:** the suite is safe to run **only** when `DATABASE_URL` is pinned to an ephemeral SQLite target **before any app import** (as CI does). The default local invocation is **unsafe** against the developer's Postgres.

## 7. Tests currently executed by CI

`.github/workflows/ci.yml` — triggers `push` to `master`/`dev` **and all `pull_request`s`. (Working branch is now `master`, so pushes trigger directly.)

- **Job `backend`** (Python 3.11, `DATABASE_URL=sqlite:///./ci.db`): install deps → `compileall` → loop all 78 `test_*.py` as scripts (every suite runs even if one fails; non-zero exit if any failed) → boot check (`import main`, count routes).
- **Job `frontend`** (Node 20): `npm ci` → `npm run build`.

Because CI supplies a throwaway SQLite URL, the import-time DDL lands on an ephemeral file — **CI is safe**; local invocation without the override is not.

## 8. Tests present but excluded from CI

- **Silent-skip risk (structural).** CI executes each file's `__main__` block, **not** pytest collection. Any `def test_*` not explicitly called inside `__main__` passes CI without ever running. With 239 functions hand-wired across 78 `__main__` blocks, drift is inevitable and invisible.
- **Frontend tests** — none exist; only `next build` runs. **ESLint (`npm run lint`) defined but not invoked.**
- **Coverage** — not measured (no `pytest-cov`/`coverage`).
- **Backend lint / type-check** — no `ruff`/`flake8`/`mypy` step.
- **`e2e_sim.py`** — manual; not in CI (and unsafe by default, §21).
- **MQTT / industrial-adapter paths** — no suite exists (§15), so nothing runs.

## 9. Authentication and RBAC coverage — **Existing but incomplete**

**Authentication.** Covered: JWT create + refresh claim survival (`test_auth_refresh.py` — decodes the reissued token, checks `sub`/`role`/`tenant` and `exp ≈ now+240min`); plan/tenant 401/403 flows (`test_plan_gate.py`); bcrypt round-trip via `verify_password` (`test_onboarding.py`). **Gaps:** `auth.verify_token()` (the 401 path) is never called by any test; **no expired-token, wrong-signature, or `alg:none` rejection test**; `decode_token_optional` (input to tenant scoping) has no direct test; `login()` failure branches (unknown user / wrong password → 401) unasserted; the legacy SHA-256 verify + rehash-on-login path is untested.

**RBAC.** `require_roles` guards ~100 endpoints across the router modules (incl. destructive admin routes: tenant deletion, invoice voiding, item deletion). **The entire suite contains exactly ONE genuine role-denial assertion:**

```
test_users_routes.py:53-63
  checker = auth.require_roles(["Admin"])
  checker(current_user={"sub":"o","role":"Operator"})  → expects HTTPException 403
  checker(current_user={"sub":"a","role":"Admin"})["role"] == "Admin"
```

That is a **unit test of the checker callable**, not a role denial through a real route. Two suites additionally introspect that routes *carry* a role/`get_current_user` dependency (`test_users_routes.py:41-50`, `test_read_model_routes.py:57-70`) — proving a guard is *attached*, not that it *rejects a request*. **No per-domain route-level RBAC matrix exists**: no Operator/Supervisor is driven through machines/quality/work-orders/inventory/tenant-admin routes to assert 403. Because every other test calls handlers directly with a hand-built `current_user` dict (or only inspects `app.routes`), `Depends(require_roles(...))` never executes. **Security note:** `auth.py:11` still ships a hard-coded fallback `SECRET_KEY` (`"...change_in_production"`) when the env var is unset — a fail-open default, untested.

## 10. Tenant-isolation coverage — **Existing and reliable (unit-level); route-level Missing**

Mechanism (`tenancy.py`): three layers — boot-time `ensure_tenant_columns` migration; a pure-ASGI `TenantScopeMiddleware` that binds a `contextvars` tenant from the bearer/`X-Tenant` headers (only a `DEFAULT` claim may honour the header); and `install_scoping`, a `do_orm_execute` hook appending `with_loader_criteria(tenant_code == current)` to every SELECT of the 28 scoped models plus a `before_flush` stamp. **Critical property: when the contextvar is `None`, both hooks no-op — queries run fully unscoped.**

Strongest proofs (all at the repository/contextvar layer, in-memory SQLite):
- `test_tenancy.py:43` — `assert gmats.get(other_id) is None` (foreign-id read defeated — beats id-guessing).
- `test_tenancy.py:85-86` — exact-list equality per tenant (a leak fails the assert).
- `test_onboarding.py:130,184` — read-model isolation (`build_twins(...,"DEFAULT")==[]`) and sim-loop write isolation ("sim leaked into APEX").

**Gaps:** **no route-level cross-tenant test** anywhere (verified across all 21 route suites — `workspace_id`/tenant-B-via-HTTP never appears); `TenantScopeMiddleware`'s header-parsing path is never exercised end-to-end; and — the single highest-value missing test in the codebase — **the unscoped-when-`None` failure mode is untested**: nothing asserts what a missing/garbage `Authorization` header does to scoping, even though a `None` tenant silently returns all tenants' rows.

## 11. Event-bus and subscriber coverage — **Existing but incomplete**

`events.py`: a **synchronous, in-process** `EventBus`; dispatch by exact `type(event)`; every event optionally persisted to `EventLog` (only if `db is not None`); subscribers share the caller's transaction; **no try/except** in `publish` (a raising subscriber aborts the whole publish). Four event types: `ProductionCompleted`, `DowntimeStarted`, `InventoryLow`, `QualityInspectionFailed`.

- `test_event_bus.py` proves runtime parity for **`ProductionCompleted`** (event path == legacy inline path for BOM/inventory movement) **and** the `EventLog` write. Strong.
- **Only 2 of 4 events are ever put through `bus.publish`** (`ProductionCompleted`, `DowntimeStarted`); `InventoryLow` and `QualityInspectionFailed` are tested by calling handlers directly — dispatch, `EventLog`, and fan-out for those two are unexercised.
- Route suites "assert" event emission by **`inspect.getsource` substring** only (`test_quality_routes.py:36-37`, `test_work_orders_routes.py:35-36`, `test_inventory_routes.py:40-41`). **Machine status/downtime emission is not asserted at all** despite the docstring claim.
- **Never tested:** the production wiring where one `ProductionCompleted` fans out to all three registered handlers (every test wires an isolated single-module bus); the nested `InventoryLow` cascade republished from inside the `ProductionCompleted` handler; subscriber-exception behaviour; ordering; idempotency under duplicate events; dead-letter; `publish(event, db=None)` silent no-audit path; duplicate registration (double-fire).

## 12. Read-model coverage — **Existing and reliable (breadth); correctness/dialect incomplete**

`test_api_smoke.py` seeds a fresh in-memory DB and drives **16 read-model endpoint handlers** off `main.app.routes`, asserting each returns a dict — a good breadth guard for composites (scorecard/handover/weekly-report/briefing). **But 12 of the 16 are type-only asserts** (`isinstance(result, dict)` — an endpoint returning `{}` passes); only 4 carry a value assertion, and even those are weak (`["kpis"]` truthy; `has_data is True`). The copilot check implies **≥25 read-models exist**, so ≥9 are never touched by the smoke test.

Depth lives in the **dedicated suites**, which are genuinely strong: `test_scorecard`, `test_twin`/`test_twin_overlay`, `test_pulse` (recomputes from `build_twins` to lock the composition contract), and the new pure-arithmetic `test_analytics_engine` (11) and `test_ai_twin_oee` (5) — pooled-not-averaged OEE, clamps, zero-guards, loss-value math. **Gaps:** numeric correctness under realistic multi-record seeds is uneven; **all DB tests use SQLite while production is Postgres**, so dialect drift (JSON, aggregate/`NULL` semantics, constraints) and the `migration_*.sql` files are never validated against Postgres.

## 13. AI-agent lifecycle coverage — **Existing but incomplete**

`test_agents.py` (11) proves the **Maintenance / reorder / quality / escalation / yield** agents' trigger→proposal→policy→decision path against in-memory SQLite, including: threshold negatives; **auto-approval** both ways (`AgentPolicy` row and `AUTO_APPROVE_AGENTS` env); the observable auto-approved effect (PO reaches `Approved`, `decided_by=="auto-policy"`); and idempotency for several agents. `test_agent_routes.py` adds route-ownership; `test_ai_copilot_fallback.py` (8) covers copilot provider fallback.

**Gaps:** the **rejection path is tested for `purchase_order` only** — the maintenance-task and escalation reject branches are never exercised; the **quality-agent idempotency guard is never actually hit** (the test feeds a below-threshold event that returns before the dedupe check); `apply_decision` with a non-`approve`/`reject` value is untested; **`AUTO_APPROVE_AGENTS` env manipulation is not wrapped in try/finally** (`test_agents.py:165-171,288`) — a failing assert mid-block leaks the var into later tests in the same process (cross-test contamination); and the broader `ai/*.py` roster lacks equivalent trigger→action→idempotency→audit-trail tests.

## 14. WebSocket coverage — **Existing but incomplete**

`test_live_ws.py` (1 test) covers the `ConnectionManager` fan-out in isolation and includes a **strong tenant-isolation assertion** — `test_broadcast_reaches_only_matching_tenant` (`:39-41`): the `DEFAULT` client receives the message, `gmats_sent == []`, `anon_sent == []`. That is the single strongest real-time assertion in the repo.

**But the endpoint itself is entirely untested.** `@app.websocket("/ws/live")` (`main.py:408-429`) authenticates via `tenancy.tenant_from_token(websocket.query_params.get("token"))` (`main.py:412`) and **accepts an invalid/absent token as an anonymous connection** (never refused) — no test pins whether that is intent or bug. Untested: handshake, the `?token=` auth path, the heartbeat loop, `manager.disconnect` cleanup in the `finally`, and dead-socket auto-eviction on send failure. No test uses `TestClient.websocket_connect`.

## 15. MQTT coverage — **Missing**

**Zero.** No `test_*.py` references `mqtt` or `paho`. `mqtt_service.py`, `mqtt_listener.py`, `mqtt_machine_publisher.py` have no coverage of any kind — including the substantial DB-writing logic in `mqtt_service.on_message` (machine upsert, status→`MachineEvent`, production gate, `Breakdown`→`DowntimeLog`, tenant-routed live broadcast). The industrial ingestion path — a core input to the platform's real-time claim — is verified nowhere. Import-safety and startup-coupling hazards are detailed in §21.

## 16. API and integration coverage — **Existing but incomplete (registration only)**

The **21 route suites (37 functions)** are **registration/ownership guards, not integration tests**. Uniform pattern: `import main`, iterate `main.app.routes`, assert each expected path is registered exactly once and `r.endpoint.__module__` equals the owning `*_routes` module; dependency presence via `r.dependant.dependencies`; event coupling via `inspect.getsource` substring. **`TestClient` is used by zero test files.** Consequently **nothing** verifies, through the ASGI stack: 401 on unauthenticated calls, 403 on wrong-role calls, cross-tenant denial, request validation (422), status codes, response bodies, `Depends` resolution, error paths, or middleware ordering. These suites correctly guard the route-extraction refactor's module boundaries — real value for that purpose — but they are not behavioural API tests. **True HTTP/integration coverage is effectively Missing.**

## 17. Frontend coverage — **Missing**

Zero test files; no test tooling (Vitest/Jest/Playwright/Cypress/Testing-Library all absent). Only `next build` runs in CI; ESLint exists but is not enforced. No component, integration, or E2E tests for the ~90-component Next.js/React dashboard, `lib/api.ts`, or `lib/live.ts` (the WebSocket client).

## 18. Linting and type-checking coverage — **Missing (backend) / Existing but not automated (frontend)**

- **Backend:** no `ruff`/`flake8`/`black`/`isort` config or dependency; **no `mypy`** despite type hints throughout. `compileall` (syntax only) is the sole static check in CI. No pre-commit hooks (`.pre-commit-config.yaml` absent; `.git/hooks` has only samples).
- **Frontend:** ESLint + `eslint-config-next` configured and runnable (`npm run lint`), TypeScript `strict: true` — but **neither lint nor `tsc` runs in CI**; only `next build` does (which surfaces type errors only if the build fails on them). Type-checking is effectively unenforced as a gate.

## 19. Flaky, duplicated, obsolete or unsafe tests — **Unsafe / Obsolete**

- **27 app-importing suites** trigger import-time `create_all` — hazardous harness coupling, not incorrect tests. *Unsafe.*
- **`factory_simulator.py:22`** runs real-engine `create_all` on import, reached by `test_sim_calibration.py` — a self-isolating suite silently DDL-exposed. *Unsafe.*
- **CI `__main__`-runner** silently skips any unwired `def test_*`. *Unsafe (process).*
- **SQLite-only vs Postgres production** — dialect drift never caught. *Incomplete/Unsafe.*
- **Duplicated bootstraps** — 44 near-identical `create_engine("sqlite://")` + seed blocks; no shared fixture. *Duplication.*
- **`test_api_smoke.py`** seeds fixed PKs (id 1/2) against a module-level engine and never closes sessions in its loop — running it twice in one process would collide. *Fragile.*
- **`backend/flowmes.db` still tracked in git** (57 KB) — `.gitignore` now lists `*.db`, but the file was committed earlier and never `git rm --cached`'d. *Obsolete.*
- **Tracked scratch files:** `live_simulator.py`, `main_predictive_endpoint_to_add.py`, `phase11_model_to_add.py`, `phase11_schema_to_add.py`; plus untracked `tree.txt` (1.6 MB). *Obsolete.*
- **`auth.py:11` hard-coded `SECRET_KEY` fallback** — untested fail-open default. *Unsafe (security).*
- No true flakiness observed (tests are deterministic and offline) — the risk is silent non-execution, not intermittent failure.

## 20. Critical untested behaviours (ranked)

1. **Unscoped-by-default tenant failure mode** — a `None` tenant (bad/missing auth header) silently returns all tenants' rows; nothing asserts the guard holds. *Highest value.*
2. **MQTT / industrial-adapter ingestion** — zero tests on a core observability input.
3. **HTTP-level auth/RBAC enforcement** — no `TestClient` proves 401/403 through the stack; one isolated `require_roles` unit check aside, RBAC is unverified across ~100 guarded endpoints.
4. **Route-level cross-tenant isolation** — proven only at the repository layer.
5. **Entire frontend** — the primary human-facing surface.
6. **Postgres dialect correctness** — all DB tests are SQLite; `migration_*.sql` and `create_all` never validated on Postgres.
7. **Invalid/expired/tampered JWT rejection**, and the `SECRET_KEY` fail-open default.
8. **`/ws/live` endpoint** (anonymous-accept auth, disconnect cleanup, dead-socket eviction).
9. **Event failure/ordering/idempotency/cascade** paths, and the 3-handler production fan-out.
10. **Full AI-agent roster** lifecycle beyond the five agents in `test_agents.py`; reject paths for task/escalation.

## 21. Safety risks in existing test commands

The existing commands are safe **only** under the exact conditions CI enforces; run any other way they are hazardous. Mandatory pre-execution conditions and their status:

| # | Condition | Status | Basis |
|---|---|---|---|
| 1 | Environment explicitly classified as **test** | ❌ Fail | No `APP_ENV`/`ENVIRONMENT`/`TESTING` exists |
| 2 | Database is **disposable** | ❌ Fail (default) | Default resolves to local Postgres; disposable only if overridden to SQLite |
| 3 | Dev `.env` is **not** used | ❌ Fail | `load_dotenv()` loads it unless a var is pre-exported |
| 4 | Prod/staging access **impossible** | ⚠️ Partial | No prod/staging creds in checkout, but not code-enforced; `e2e_sim.py` defaults to the prod host |
| 5 | Collection triggers **no unsafe import-time behaviour** | ❌ Fail | `import main` → DDL ×27; `import factory_simulator` → DDL; two MQTT modules connect at import |

Concrete hazards:
- **`import main`** (27 files) → `Base.metadata.create_all` + `ALTER TABLE`/`CREATE INDEX` DDL against the resolved DB. Local default = **localhost Postgres**.
- **`import factory_simulator`** → real-engine `create_all` (`factory_simulator.py:22`).
- **`mqtt_listener.py:147` and `mqtt_machine_publisher.py:28`** call `client.connect(...)` at **module import** (hardcoded `127.0.0.1:1883`, then block forever) — collection hazards if pytest ever imports them by glob.
- **`TestClient(app)` context-manager coupling** — `main.py:299-301` startup event calls `start_mqtt_service()`, spawning a thread that attempts a broker connect. Current tests avoid it (they only `import main` and inspect routes), but the first `with TestClient(app):` will trigger a real socket attempt (swallowed on a daemon thread, but real).
- **`e2e_sim.py:34`** defaults `AMP_URL` to `https://flowmes-production.up.railway.app` — running it with no override drives traffic (including state-mutating calls) against **production**.

**Minimal safe invocation (for future use; not executed in this audit):** export `DATABASE_URL=sqlite:///./throwaway.db` (or `sqlite://`) **before** invoking, from `backend/`, exactly as CI does — and never run `e2e_sim.py` without `AMP_URL` pointed at a local/disposable target.

## 22. Recommended autonomous QA-agent permissions

Least privilege with a **mandatory pre-flight safety proof**; every write via a PR branch the human reviews.

**READ (always):** full repo read; `git status/log/diff/branch/show`; list/inspect files; read env *key names* only (never values).

**EXECUTE (sandboxed, only after the pre-flight proof passes):** `pytest`, `compileall`, `ruff`, `mypy`, `eslint`, `tsc --noEmit`, `next build`, coverage — **exclusively** against an ephemeral SQLite DB, offline.

**WRITE (PR branch only):** files under a future `tests/`, `docs/engineering/`, and CI/lint/test *configuration*. **Never** production source, `.env`, migrations, or `settings`.

**Mandatory pre-flight DB-safety proof (all must hold, else halt and report):**
1. `DATABASE_URL` scheme is `sqlite` and target is `:memory:`/`sqlite://`/ephemeral file — refuse if scheme is `postgresql` or host ≠ localhost/sqlite.
2. `APP_ENV=test` (once such a classifier exists — see §23).
3. The dev `.env` is not the active source (an explicit env var overrides it).
4. No reachable prod/staging endpoint (network egress restricted; refuse if any target resolves to a `railway.app`/remote host).
5. Target imports are side-effect-free (no import-time DDL / no broker connect) — else refuse.

**FORBIDDEN:** connecting to any non-ephemeral DB; network access to staging/prod; printing secret values; installing/removing/upgrading dependencies without human approval; running migrations; seeding/truncating/deleting data; pushing to `master`/`dev`; merging; deploying; changing Claude/agent permissions.

**Escalation:** on any failed pre-flight condition, halt and report (as this audit did) rather than proceed.

## 23. Smallest implementation plan that preserves the current framework

Ordered by safety-per-effort. Every step is additive — **the existing `python test_x.py` scripts keep working unchanged**; no production business logic is touched.

1. **Make imports side-effect-free (highest priority).** Move `Base.metadata.create_all(...)` and the `_ensure_*` DDL out of `main.py` module scope into an `init_db()` called from the FastAPI startup handler (or env-guarded). Do the same for `factory_simulator.py:22`. Resolves pre-flight condition #5 for 27+1 suites at a stroke.
2. **Add a test bootstrap.** Introduce `backend/conftest.py` + a minimal `pytest.ini` that does `os.environ.setdefault("DATABASE_URL", "sqlite://")` and `APP_ENV=test` **before any app import**, and expose one shared in-memory-session fixture to replace the 44 duplicated bootstraps. `__main__` blocks stay for convenience.
3. **Add an environment classifier.** `APP_ENV` + a fail-closed guard so `create_all`/migrations refuse unless `APP_ENV in {test, dev}` and the DB is SQLite/localhost. Closes conditions #1–#3.
4. **Adopt pytest as the canonical runner.** Add `pytest` + `pytest-cov` to `requirements.txt`; replace the CI `for f in test_*.py` loop with `pytest -q --cov`, closing the silent-skip gap (§8). Keep the script loop as a fallback if desired.
5. **Add CI quality gates.** Backend `ruff` (lint) + `mypy` (types) + a coverage floor; frontend `npm run lint` and `tsc --noEmit`.
6. **Fill the highest-value gaps, in order:** (a) a `TestClient` + `dependency_overrides` harness proving 401/403 and **cross-tenant denial through the ASGI stack**, plus the **unscoped-when-`None`** assertion; (b) MQTT/adapter unit tests against a fake broker; (c) the `/ws/live` endpoint via `websocket_connect`; (d) a minimal Vitest + Testing-Library frontend smoke set.
7. **Repo hygiene.** `git rm --cached backend/flowmes.db`; relocate/remove the `*_to_add.py` and `live_simulator.py` scratch files; drop `tree.txt`.
8. **Security fix.** Make `auth.py` fail closed when `SECRET_KEY` is unset.

Steps 1–4 alone convert the suite from "unsafe to collect" to "one `pytest` command, coverage-measured, CI-gated" without rewriting a single existing test.

---

## Area classification summary

| Area | Classification |
|---|---|
| Backend test framework (execution harness) | **Existing but not automated** (script loop; no pytest/coverage) |
| Backend pytest config / fixtures / factories | **Missing** |
| Frontend test framework | **Missing** |
| Import-time / DB run safety | **Unsafe or obsolete** |
| Test DB disposability & env classification | **Unsafe or obsolete** (no `APP_ENV`; dev Postgres default) |
| Authentication | **Existing but incomplete** |
| RBAC | **Existing but incomplete** (one isolated checker unit test; no route-level matrix) |
| Tenant isolation | **Existing and reliable** (unit-level); route-level **Missing** |
| Event bus & subscribers | **Existing but incomplete** |
| Read-models | **Existing and reliable** (breadth); correctness/dialect **incomplete** |
| AI-agent lifecycle | **Existing but incomplete** |
| WebSocket | **Existing but incomplete** |
| MQTT / industrial adapters | **Missing** |
| API / HTTP integration | **Existing but incomplete** (registration guards only; no `TestClient`) |
| Frontend coverage | **Missing** |
| Backend lint / type-check | **Missing** |
| Frontend lint / type-check | **Existing but not automated** (configured; not in CI) |
| Coverage gates | **Missing** |
| Committed DB artifact & scratch files | **Unsafe or obsolete** |
| `auth.py` SECRET_KEY fallback | **Unsafe or obsolete** |

---

## Proposed Phase 1 QA agent (proposal only — NOT created or implemented)

**Name:** `qa-safeguard` (Phase 1 — Safety & Regression Sentinel).

**Mission:** make the existing suite safe to run under one command and hold the line on regressions — **without** rewriting existing tests or touching production logic.

**Operating envelope:** the least-privilege model and mandatory pre-flight DB-safety proof in §22. Runs read-only discovery + sandboxed execution against ephemeral SQLite only; all writes land on a PR branch for human review; halts and reports on any failed safety condition.

**Phase 1 backlog (in order):**
1. Land remediation steps 1–4 of §23 (side-effect-free imports, `conftest.py`/`pytest.ini`, `APP_ENV` guard, pytest+coverage in CI) — each as a separate reviewable PR.
2. Add the `TestClient` auth/RBAC/cross-tenant harness and the unscoped-`None` assertion (§20 #1, #3, #4).
3. Stand up MQTT/adapter fake-broker unit tests (§15) and the `/ws/live` endpoint test (§14).
4. Add backend `ruff`/`mypy` and frontend `lint`/`tsc` CI gates; introduce a minimal Vitest frontend smoke set.

**Explicitly out of scope for Phase 1:** creating the agent (this document proposes it only), any production-source change beyond the mechanical import-safety move in §23 step 1, dependency installs without human approval, and anything touching data, migrations, deployments, or permissions.

---

*End of Phase 1 audit. No source code, dependencies, migrations, data, or deployments were modified. No tests were executed. The QA agent above is proposed only. Awaiting approval before any remediation (Phase 2).*
