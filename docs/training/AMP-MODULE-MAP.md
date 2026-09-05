# AMP — Module Map

*"Where is X?" — the full lookup. Verified at commit `0eb94ca`. Paths are repo-relative to `backend/` and `frontend/` unless noted.*

## How to read this
Every module follows the same skeleton:
**Frontend `*Section.tsx`** ↔ **`*_routes.py` (API)** ↔ **`models.py` tables** ↔ *(optional)* **event** ↔ **`ai/*` read-model** ↔ *(optional)* **agent** ↔ **`test_*.py`**.

---

## Factory MES modules

### Machines / Downtime / Shifts / Production
- **Frontend:** `components/MissionControlSection.tsx`, `MachineHealthSection.tsx`, `DigitalTwinSection.tsx`, `MachineDetailDrawer.tsx`
- **Routes:** `machines_routes.py` (no prefix) — `GET/POST /machines`, `PATCH /machines/{id}/status`, `GET/POST /downtime-logs`, `GET/POST /shifts`, `GET/POST /production-records`, `GET /machine-events`, `POST /machines/import-csv`
- **Logic:** `machine_status.py` (status/util normalisation)
- **Tables:** `Machine`, `DowntimeLog`, `ShiftData`, `ProductionRecord`, `MachineEvent`, `FactoryLayoutNode`
- **Events:** produces `DowntimeStarted`
- **Read-models:** `ai/twin.py`, `ai/downtime.py`, `ai/reliability.py`, `ai/oee.py`
- **Agents:** Maintenance, Escalation
- **Tests:** `test_machine_*.py`, `test_core_routes.py`, `test_oee.py`

### Work Orders
- **Frontend:** `components/WorkOrdersSection.tsx`, `WorkOrderTraceDrawer.tsx`
- **Routes:** `work_orders_routes.py` (`/work-orders`)
- **Logic:** `subscribers.py`, `bom.py`
- **Tables:** `WorkOrder`
- **Events:** produces `ProductionCompleted`
- **Read-models:** `ai/production.py`, `ai/trace.py`
- **Agents:** Yield
- **Tests:** `test_work_orders_routes.py`, `test_flow.py`, `test_trace.py`

### BOM (Bill of Materials)
- **Frontend:** `components/BomViewer.tsx`
- **Routes:** `bom_routes.py` (`/bom`, Admin-only)
- **Logic:** `bom.py` (`resolve`, `components_of`)
- **Tables:** `BillOfMaterials`, `BomComponent`
- **Events:** consumed indirectly by `ProductionCompleted` subscriber
- **Tests:** `test_bom*.py`, `mutate_bom.py`

### Inventory (basic + enterprise + GMATS)
- **Frontend:** `components/InventorySection.tsx`, `EnterpriseInventory.tsx`, `GmatsInventory.tsx`, `InventorySnapshot.tsx`, `PartRunwayDrawer.tsx`
- **Routes:** `inventory_routes.py` (`/inventory`), `enterprise_inventory_routes.py` (no prefix), `gmats_inventory_routes.py` (`/gmats`)
- **Tables:** `InventoryItem`, `InventoryTransaction`, `Supplier`, `PurchaseOrder`, `Remnant`, `MaterialIssueSlip`, `GoodsReceiptNote`, `GRNItem`, `CycleCount`, `CycleCountItem`, `Gmats*`
- **Events:** produces `InventoryLow`
- **Read-models:** `ai/inventory.py`, `ai/coverage.py`, `ai/stock_health.py`, `ai/stock_accuracy.py`, `ai/supply.py`, `ai/supplier_performance.py`
- **Agents:** Reorder
- **Logic:** `doc_numbers.py` (ADR-0012 tenant-scoped numbers)
- **Tests:** `test_enterprise_inventory_routes.py`, `test_supply.py`, `test_coverage.py`, `test_csv_import_atomicity.py`

### Quality
- **Frontend:** `components/QualitySection.tsx`, `QualitySnapshot.tsx`, `QualityDefectDrawer.tsx`
- **Routes:** `quality_routes.py` (`/quality`)
- **Tables:** `QualityInspection`
- **Events:** produces `QualityInspectionFailed`
- **Read-models:** `ai/quality.py`
- **Agents:** Quality
- **Tests:** `test_quality_*.py`

### Maintenance
- **Frontend:** `components/MaintenanceSection.tsx`, `MaintenanceSnapshot.tsx`, `MaintenanceExecutionSnapshot.tsx`
- **Routes:** `factory_ops_routes.py` (`/maintenance/tasks`)
- **Tables:** `MaintenanceTask`
- **Read-models:** `ai/maintenance.py`
- **Agents:** Maintenance (creates tasks)
- **Tests:** `test_maintenance*.py`

### Production Planning / Scheduling
- **Frontend:** `components/ProductionPlanSection.tsx`, `SchedulingSection.tsx`
- **Routes:** `production_planning_routes.py` (no prefix)
- **Tables:** `ProductionPlan`, `ProductionSchedule`
- **Read-models:** `ai/schedule.py`, `ai/schedule_load.py`, `ai/flow.py`
- **Tests:** `test_production_planning_routes.py`, `test_schedule.py`

### Factory Ops (Escalations / Layout / Documents / Notifications)
- **Frontend:** `components/EscalationSection.tsx`, `DocumentsSection.tsx`, `NotificationsSection.tsx`, `DigitalTwinSection.tsx` (layout)
- **Routes:** `factory_ops_routes.py` — `/escalations`, `/factory-layout/nodes`, `/documents`, `/notifications`
- **Tables:** `Escalation`, `FactoryLayoutNode`, `ComplianceDocument`, `Notification`, `Alert`
- **Read-models:** `ai/escalations.py`, `ai/compliance.py`
- **Tests:** `test_factory_ops_routes.py`, `test_defect_escalations.py`, `test_compliance.py`

### Operator Terminal
- **Frontend:** `components/OperatorTerminalSection.tsx`
- **Routes:** `operator_routes.py` (`/operator`)
- **Tables:** `OperatorJobExecution`
- **Read-models:** `ai/workforce.py`, `ai/roster.py`, `ai/handover.py`, `ai/shift.py`
- **Tests:** `test_operator_routes.py`

### Reports / Costing / Analytics
- **Frontend:** `components/CsvExportsCard.tsx`, `CostingSection.tsx`, `ExecutiveOeeSection.tsx`, `TrendsSection.tsx`, `AIInsightsSection.tsx`, `PredictiveMaintenanceSection.tsx`
- **Routes:** `reports_routes.py` (`/reports`), `costing_routes.py`, `analytics_routes.py` (26 endpoints), `recommendations_routes.py`
- **Logic:** `analytics_engine.py`, `oee_contract.py`, `report_generator.py`, `csv_safe.py`
- **Tables:** `CostRecord`, `ReportRequest` (+ reads across all)
- **Read-models:** `ai/oee.py`, `ai/losses.py`, `ai/recovery.py`, `ai/cost.py`, `ai/scorecard.py`, `ai/trends.py`, `ai/delivery.py`, `ai/insights.py`
- **Tests:** `test_analytics_engine.py`, `test_oee_contract.py`, `test_costing_routes.py`, `test_reports_routes.py`

### Industrial IoT
- **Frontend:** `components/IoTCommandSection.tsx`, `IndustrialConnectivity.tsx`, `IndustrialGatewaySection.tsx`
- **Routes:** `industrial_iot_routes.py` (no prefix)
- **Logic:** `industrial_adapters.py` (simulator framework), `mqtt_service.py`, `mqtt_identity.py`
- **Tables:** `IoTTelemetry`, `IndustrialDevice`, `IndustrialSignal`, `PlcSignalMapping`
- **Read-models:** `ai/connectivity.py`
- **Tests:** `test_industrial_iot_routes.py`, `test_connectivity.py`, `test_industrial_adapters.py`

## Platform / SaaS / Identity

### Users / RBAC
- **Frontend:** `components/UsersSection.tsx` · **Routes:** `users_routes.py` (`/users`, Admin-only) · **Tables:** `User` · **Logic:** `auth.py`, `security.py` · **Tests:** `test_users_routes.py`

### Platform (licensing / branding / audit / health)
- **Frontend:** `components/ModuleLicensingPanel.tsx`, `BrandingSettingsCard.tsx`, `UnitRateEditor.tsx`, `PlatformStatusCard.tsx` · **Routes:** `platform_routes.py` (`/health`, `/readiness`, `/tenant-config`, `/modules`, `/audit-logs`) · **Tables:** `TenantConfig`, `AuditLog` · **Logic:** `module_manifest.py` + `modules.json`, `schema_guard.py`

### SaaS lifecycle
- **Frontend:** `components/SaaSAdminSection.tsx` · **Routes:** `saas_routes.py` (`/saas/*`, founder-gated) · **Tables:** `CompanyTenant` · **Logic:** `onboard_tenant.py`, `offboard_tenant.py` · **Tests:** `test_onboarding.py`, `test_offboarding.py`

### AI / Agents / Mission Control
- **Frontend:** `components/MissionControlSection.tsx`, `ApprovalsInbox.tsx`, `AgentActivitySection.tsx`, `AgentPolicyPanel.tsx`, `AgentRoiSection.tsx`, `AgentDetailDrawer.tsx`, `AICopilot.tsx`, `NextBestActionCard.tsx`
- **Routes:** `agent_routes.py`, `read_model_routes.py`, `ai/copilot.py` register (`/ai/ask`, `/ai/status`, `/ai/report`, `/ai/copilot/ask`)
- **Logic:** `ai/agents.py`, `ai/subscribers.py`, `approvals.py`, `ai_copilot.py`, `ai/assistant.py`, `predictive_engine.py`, `ai/prediction.py`
- **Tables:** `AIRecommendation`, `AgentAction`, `AgentPolicy`, `EventLog`
- **Read-models:** `ai/pulse.py`, `ai/insights.py`, `ai/impact.py`, `ai/twin.py`, `ai/briefing.py`, `ai/scorecard.py`, `ai/report.py`, `ai/search.py`
- **Tests:** `test_agents.py`, `test_agent_routes.py`, `test_agent_stats.py`, `test_ai_copilot_*.py`, `test_recovery.py`

## OEM platform
- **Frontend:** `frontend/app/oem/page.tsx`, `frontend/lib/oem.ts`, `components/OemMachineRegistry.tsx`, `frontend/app/claim/[code]/page.tsx`
- **Routes:** `oem_routes.py` (`/oem/*`), `oem_admin_routes.py`, `connected_equipment_routes.py` (`/connected-equipment/*`)
- **Logic:** `oem_auth.py` (sentinel tenant, capabilities), `oem_service.py` (lifecycle, service), `oem_sharing.py` (consent allowlist), `oem_claims.py` (claim atomicity), `oem_events.py`, `demo_aeron.py`
- **Tables:** `OemOrganization`, `OemUser`, `MachineModel`, `MachineInstallation`, `OemDataSharingPolicy`, `MachineClaim`
- **Events:** `MachineInstalled`, `MachineClaimed`, `MachineCommissioned`, `ServiceCompleted` (via `oem_events`)
- **Read-models:** `oem_sharing.fleet_row` / `service_view` / `commissioning_view`
- **Tests:** `test_machine_claim.py`, `test_oem_provisioning.py`, `test_oem_service_consent.py`, `test_connected_equipment.py`, `mutate_oem_sharing.py`, `audit_oem_adversarial.py`, `audit_oem_demo_journey.py`

## Infra / ops
- **Migrations:** `alembic/versions/0001_baseline` → `0008_machine_claim`, `migrate.py`, `schema_guard.py`, `alembic/env.py`
- **CI/CD:** `.github/workflows/ci.yml` (5 jobs), `backup.yml`, `retention.yml`
- **Deploy:** `backend/railway.toml`, `backend/Procfile`, root `Dockerfile`, `docker-compose.yml`, `frontend/vercel.json`
- **DR:** `restore_drill.py`, `verify_pg_deploy.py`, `scripts/restore_check.sh`, `retention.py`
- **Load:** `loadtest.py`, `load/` harness
