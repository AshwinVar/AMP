# AMP — Data Flows

*End-to-end traces of every important path. Verified at commit `0eb94ca`. Companion to the Handbook (chapter refs in parentheses).*

## 1. LOGIN (Ch.5)
```
browser (login/page.tsx)
  → POST /login {username,password}          (lib/api.ts)
  → core_routes.login()
  → SELECT users WHERE username=?             (401 if none)
  → security.verify_password (bcrypt; legacy SHA-256 auto-upgraded)
  → resolve tenant (user.tenant_code)
  → subscription/trial gate                   (403 if Cancelled/expired)
  → auth.create_access_token({sub,role,tenant}, exp=+240m)  (HS256)
  → 200 {access_token, role, tenant}
  → localStorage.token ; route /dashboard or /oem
  → every later call: Authorization: Bearer <jwt>
```

## 2. AUTHENTICATED REQUEST (Ch.7, 17)
```
request + Bearer JWT
  → TenantScopeMiddleware: decode_token_optional ONCE → set_current_tenant(tenant)
  → SchemaGuardMiddleware: 503 if schema behind
  → RateLimit → CORS → SecurityHeaders → PlanGate (403 if unlicensed)
  → route(*_routes.py): Depends(get_db), Depends(get_current_user|require_roles)
  → db.query(Model): do_orm_execute adds WHERE tenant_code=? (scoped models)
  → Pydantic response schema → JSON
  → reset_current_tenant() in finally
```

## 3. MACHINE TELEMETRY via MQTT (Ch.15)
```
machine/simulator → publish flowmes/{tenant}/{site}/machines {machine,status,util,counts}
  → broker → mqtt_service.on_message (subscribed flowmes/+/+/machines)
  → mqtt_identity.parse_topic → Route(tenant, site)   (drop if unroutable)
  → check_payload_agrees (reject if body 'tenant' contradicts topic)
  → tenant_is_provisioned? (else drop)
  → set_current_tenant(tenant)
  → get_or_create_machine(tenant, site, name)          (identity = triple)
  → update status/util (guards) ; write MachineEvent (on change),
    ProductionRecord (if good+rejected==total), DowntimeLog (on →Breakdown)
  → publish DowntimeStarted on the SAME transition        [added 2026-09-01]
      (guarded: a failing subscriber is logged, never costs the committed row —
       unlike the HTTP path, where it correctly fails the request)
  → commit
  → build machine_update {tenant_code, machine, production, timeline}
  → safe_broadcast → live_ws.broadcast_live_event
      → run_coroutine_threadsafe onto the SERVER's loop    [fixed 2026-09-01]
      → live_ws.manager.broadcast → owning-tenant browsers only
```
> **Two corrections, 2026-09-01.** The ingest previously published **no events at
> all**, so the Escalation agent never saw machine-reported breakdowns. And
> `safe_broadcast` wrapped a synchronous callee in `asyncio.run(...)`, raising
> `ValueError` on every message and delivering on a throwaway event loop instead
> of the server's. Both fixed; see `test_mqtt_publishes_downtime.py` and
> `test_live_broadcast_bridge.py`.

## 4. LIVE WEBSOCKET (Ch.16)
```
browser onload → ws://API/ws/live?token=<jwt>   (lib/live.ts)
  → main.py /ws/live → ws_auth.resolve(db, token) BEFORE accept
      (valid+unexpired+user exists+is_active+claimed tenant == user.tenant_code;
       else close 4401/4403, no accept)
  → ConnectionManager.connect(ws, tenant)          (refuse if tenant falsy)
  → on each machine_update: broadcast sends only where conn.tenant == payload.tenant_code
  → any inbound client frame → close (one-way feed) ; 30s heartbeat
```

## 5. WORK ORDER → PRODUCTION → BOM (Ch.10)
```
PATCH /work-orders/{id} {status:"Completed", actual_quantity:N}
  → work_orders_routes.update_work_order
  → first completion? (completed_at is None)  → stamp completed_at
  → event_bus.publish(ProductionCompleted(part, qty=N, tenant), db)
       → EventLog append
       → subscribers.move_bom_on_production_completed:
            bom.resolve(db, tenant, part)  → components
            InventoryItem: consume qty*per_unit (Issue) ; receive N finished (Receive)
            if a component crosses reorder → event_bus.publish(InventoryLow)
       → ai.subscribers.recommend_on_production_completed (risk≥55 → AIRecommendation)
       → ai.agents Maintenance (risk≥75) / Yield (good-rate<85%) propose
  → all commit atomically with the WO update
```

## 6. INVENTORY LOW → REORDER AGENT (Ch.9, 13)
```
POST /inventory/transactions {type:"Issue", qty}   (or BOM consumption)
  → stock -= qty ; ledger row ; if crossed reorder_level → publish InventoryLow
  → ai.agents.draft_reorder_on_inventory_low:
       PurchaseOrder(AUTO-PO, status=Draft) + AgentAction(status=Proposed)
       AgentPolicy: reorder auto-approved by default → still just a Draft
  → visible in Inventory + Mission Control insights
```

## 7. AGENT PROPOSE → APPROVE → EXECUTE (Ch.13)
```
event → agent._propose():
   create item PENDING (Task=Proposed | PO=Draft | Escalation=Proposed)
   + AgentAction(status="Proposed")           (audit row AND queue row)
   auto-approve if AgentPolicy allows, else Notification
human: POST /agent-actions/{id}/approve|reject   (require_roles Admin/Supervisor)
   → approvals.authorise(db, action, actor, decision):
        1 tenant match?  → 404
        2 still Proposed? → 400
        3 not expired?    → 409
        4 actor exists+active+in-tenant+approver-role (re-read DB) → 401/403
   → apply_decision: Task→Open | PO→Approved | Escalation→Open ; stamp decided_by/at
```

## 8. OEE COMPUTATION (Ch.10 note; ADR-0014)
```
records in window [now-d, now)
  A = Σ runtime / Σ planned
  P = Σ(ideal_cycle_s × total) / (Σ runtime × 60)
  Q = Σ good / Σ total
  OEE = clamp(A) × clamp(P) × clamp(Q)     (clamp = max(0,min(1,x)))
  has_data = planned>0 OR total>0 ; undefined component → None (not 0)
  coverage: machines_reporting / machines_expected travels with the number
  × TenantConfig.unit_value_gbp → £ (ADR-0010; unset → units only)
engines: analytics_engine.pooled_oee_from_sums / calculate_oee_from_record ; oee_contract.plant_oee
```

## 9. OEM ONBOARDING (Ch.19)
```
founder/OEM admin → provision OemOrganization + OemUser (oem_admin_routes)
OEM user → POST /oem/login (OemUser table; is_active + role∈OEM_ROLES + org active)
  → JWT {sub, role, principal:"oem", oem}  (NO tenant claim)
every OEM request → oem_auth.require_oem(caps) → resolve() re-reads principal from DB
  → binds sentinel tenant OEM:<code> → factory tables return 0 rows
```

## 10. OEM MACHINE CLAIM (Ch.20; ADR-0019)
```
OEM: POST /oem/machines               → MachineInstallation(status=Manufactured, factory_tenant_code=NULL)
OEM: POST /oem/machines/{id}/claim    → MachineClaim(token_hash=sha256(code), status=Pending, expires_at)
                                         raw code returned ONCE (QR: APP_URL/claim/<code>)
Factory Admin: GET /connected-equipment/claim/{code}   → preview (Admin-only; find_by_hash; usable?)
Factory Admin: POST /connected-equipment/claim/{code}  → accept (atomic):
     UPDATE machine_claims SET status='Claimed' WHERE id=? AND status='Pending'      (0→refuse)
     UPDATE machine_installations SET factory_tenant_code=?, status='Assigned'
            WHERE id=? AND factory_tenant_code IS NULL                                (0→rollback)
     + OemDataSharingPolicy upsert (grants chosen at accept)
Factory Admin: POST /connected-equipment/{id}/link     → installation.machine_id = floor Machine
OEM: POST /oem/machines/{id}/commission                → Commissioning → Active (+MachineCommissioned)
telemetry (MQTT) → installation.operating_hours/last_seen_at → GET /oem/fleet
uniform refusal: spent/expired/wrong/not-yours → identical 404 on preview AND accept
```

## 11. OEM CONSENT ENFORCEMENT (Ch.21; ADR-0017)
```
Factory Admin: PUT /connected-equipment/sharing {grants CSV}   → OemDataSharingPolicy
OEM: GET /oem/fleet → oem_sharing.fleet_row(installation, grants):
     row = {} ; row["operating_hours"] = None (default hidden)
     if SHARE_OPERATING_HOURS in grants: row["operating_hours"] = installation.operating_hours
     ... (one copy-in per grant; grants read FRESH each request)
toggle a grant OFF → next request: 'if' false → field stays None (fails closed)
guard: a caller lacking SHARE_OPERATING_HOURS may not supply service_hours (no bisection oracle)
```

## 12. OEM SERVICE VERDICT (Ch.22)
```
since     = operating_hours - (last_service_hours or 0)
remaining = service_interval_hours - since
remaining<=0 → overdue ; <=5% → due ; <=15% → due_soon ; else ok
guards: no interval → not_configured ; no hours → unknown ; meter<last → unknown
POST /oem/machines/{id}/service writes last_service_hours/at   (never hours % interval)
```

## 13. DEPLOYMENT (Ch.25-26; ADR-0018)
```
local → git push → GitHub Actions ci.yml:
   backend(tests+boot+AERON journey) · migrations(PG: upgrade+empty-diff+verify_pg_deploy) ·
   coverage(≥78) · frontend(Vitest≥89+build+drift) · e2e(Playwright)
merge master →
   Railway: preDeploy `python migrate.py` (abort deploy on failure; old version serves) →
            startCommand uvicorn → startup schema_guard → /readiness 200 → traffic
   Vercel:  next build → app.marx8.com
```

## 14. BACKUP / RESTORE (Ch.26)
```
backup.yml (daily 02:17 UTC): pg_dump → gzip → assert real (size, ≥40 CREATE TABLE) → 30d artifact
   → restore-drill job: restore into throwaway PG18 (scripts/restore_check.sh)
restore_drill.py: dump → new DB → restore → alembic upgrade → boot → login → verify data+isolation = RTO
retention.yml (weekly): dry-run scheduled ; apply only on manual dispatch apply=true
```
