"use client";

import "../phase29-enterprise.css";

import { useEffect, useRef, useState } from "react";
import { apiGet, apiPost, apiPatch, apiDelete, getToken, getUserRole } from "../../lib/api";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

import MachineTimeline from "../../components/MachineTimeline";
import MachineStateSummary from "../../components/MachineStateSummary";
import WorkOrdersSection from "../../components/WorkOrdersSection";
import BomViewer from "../../components/BomViewer";
import PredictiveMaintenanceSection from "../../components/PredictiveMaintenanceSection";
import ProductionPlanSection from "../../components/ProductionPlanSection";
import EscalationSection from "../../components/EscalationSection";
import InventorySection from "../../components/InventorySection";
import EnterpriseInventory from "../../components/EnterpriseInventory";
import GmatsInventory from "../../components/GmatsInventory";
import UsersSection from "../../components/UsersSection";
import AICopilot from "../../components/AICopilot";
import IndustrialConnectivity from "../../components/IndustrialConnectivity";
import type { User } from "../../lib/types";
import QualitySection from "../../components/QualitySection";
import ExecutiveOeeSection from "../../components/ExecutiveOeeSection";
import FactoryPulse from "../../components/FactoryPulse";
import QualitySnapshot from "../../components/QualitySnapshot";
import DowntimeSnapshot from "../../components/DowntimeSnapshot";
import ProductionSnapshot from "../../components/ProductionSnapshot";
import OeeSnapshot from "../../components/OeeSnapshot";
import LossesSnapshot from "../../components/LossesSnapshot";
import InventorySnapshot from "../../components/InventorySnapshot";
import FlowSnapshot from "../../components/FlowSnapshot";
import ShiftSnapshot from "../../components/ShiftSnapshot";
import BriefingSnapshot from "../../components/BriefingSnapshot";
import DeliverySnapshot from "../../components/DeliverySnapshot";
import CostSnapshot from "../../components/CostSnapshot";
import HandoverSnapshot from "../../components/HandoverSnapshot";
import ScorecardStrip from "../../components/ScorecardStrip";
import MaintenanceSnapshot from "../../components/MaintenanceSnapshot";
import ComplianceSnapshot from "../../components/ComplianceSnapshot";
import WeeklyReportSnapshot from "../../components/WeeklyReportSnapshot";
import DigitalTwinSection from "../../components/DigitalTwinSection";
import OrdersDispatchSection from "../../components/OrdersDispatchSection";
import PurchasingSection from "../../components/PurchasingSection";
import DocumentsSection from "../../components/DocumentsSection";
import MaintenanceSection from "../../components/MaintenanceSection";
import SchedulingSection from "../../components/SchedulingSection";
import IoTCommandSection from "../../components/IoTCommandSection";
import AIInsightsSection from "../../components/AIInsightsSection";
import PlatformStatusCard from "../../components/PlatformStatusCard";
import MissionControlSection from "../../components/MissionControlSection";
import AgentActivitySection from "../../components/AgentActivitySection";
import AgentRoiSection from "../../components/AgentRoiSection";
import MachineHealthSection from "../../components/MachineHealthSection";
import SaaSAdminSection from "../../components/SaaSAdminSection";
import CostingSection from "../../components/CostingSection";
import OperatorTerminalSection from "../../components/OperatorTerminalSection";
import NotificationsSection from "../../components/NotificationsSection";
import ApprovalsInbox from "../../components/ApprovalsInbox";
import TrendsSection from "../../components/TrendsSection";
import EnterprisePolishSection from "../../components/EnterprisePolishSection";

import type {
  MachineEvent,
  MachineStateSummary as MachineStateSummaryType,
} from "../../lib/phase8-types";
import type { WorkOrder, WorkOrderAnalytics } from "../../lib/phase9-types";
import type { PredictiveRisk } from "../../lib/phase10-types";
import type {
  ProductionPlan,
  ProductionPlanAnalytics,
} from "../../lib/phase11-types";
import type { Escalation, EscalationAnalytics } from "../../lib/phase12-types";
import type { InventoryAnalytics, InventoryItem, InventoryTransaction } from "../../lib/phase13-types";
import type { QualityAnalytics, QualityInspection } from "../../lib/phase14-types";
import type { ExecutiveOee } from "../../lib/phase15-types";
import type { FactoryCommandCenter, FactoryLayoutNode } from "../../lib/phase16-types";
import type { CustomerOrder, CustomerOrderAnalytics } from "../../lib/phase17-types";
import type { PurchaseOrder, PurchasingAnalytics, Supplier } from "../../lib/phase18-types";
import type { ComplianceDocument, DocumentAnalytics, MaintenanceAnalytics, MaintenanceTask, ProductionSchedule, ScheduleAnalytics } from "../../lib/mega-pack1-types";
import type { AIInsights, AIRecommendation, IoTCommandCenter, IoTTelemetry } from "../../lib/mega-pack2-types";
import type { CompanyTenant, CostingAnalytics, CostRecord, OperatorAnalytics, OperatorJobExecution, SaaSAnalytics } from "../../lib/mega-pack3-types";
import type { AuditLog, FinalExecutiveSummary, NotificationItem, ReportRequest, SystemHealth } from "../../lib/phase27-types";
import { connectLiveSocket, type LiveEvent } from "../../lib/live";
import {
  NAV_ITEMS,
  MODULE_CATALOG,
  PLAN_MODULES,
  getEnabledModules,
  isViewEnabled,
  getViewModule,
  canRoleSeeView,
  type PlanName,
} from "../../lib/modules";
import LockedModuleView from "../../components/LockedModuleView";

type Machine = {
  id: number;
  name: string;
  status: string;
  utilization: number;
  downtime: string;
};

type DowntimeLog = {
  id: number;
  machine_id: number;
  reason: string;
  duration: string;
  notes?: string;
};

type Shift = {
  id: number;
  shift_name: string;
  target_output: number;
  actual_output: number;
};

// The page uses the canonical API client (lib/api) — one client for the whole
// app, carrying the query-string-safe cache-buster, the sliding-session token
// refresh and the expired-session redirect. Only the token-claim readers that
// lib/api doesn't export stay local.
function getUserTenant(): string {
  const token = getToken();
  if (!token) return "DEFAULT";
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.tenant || "DEFAULT";
  } catch {
    return "DEFAULT";
  }
}

function getUserName(): string {
  const token = getToken();
  if (!token) return "";
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.sub || "";
  } catch {
    return "";
  }
}

function getStatusStyle(status: string) {
  switch (status) {
    case "Running":
      return "bg-green-500/20 text-green-400 border-green-500/40";
    case "Idle":
      return "bg-yellow-500/20 text-yellow-400 border-yellow-500/40";
    case "Breakdown":
      return "bg-red-500/20 text-red-400 border-red-500/40";
    case "Maintenance":
      return "bg-blue-500/20 text-blue-400 border-blue-500/40";
    default:
      return "bg-gray-500/20 text-gray-400 border-gray-500/40";
  }
}

function parseDurationToMinutes(value: string) {
  const lower = String(value || "").toLowerCase();
  let total = 0;

  const hourMatch = lower.match(/(\d+)\s*h/);
  const minuteMatch = lower.match(/(\d+)\s*m/);

  if (hourMatch) total += Number(hourMatch[1]) * 60;
  if (minuteMatch) total += Number(minuteMatch[1]);

  if (!hourMatch && !minuteMatch) {
    const plainNumber = Number(lower.replace(/\D/g, ""));
    total += Number.isNaN(plainNumber) ? 0 : plainNumber;
  }

  return total;
}

function calculateOEE(utilization: number) {
  const availability = utilization / 100;
  const performance = 0.9;
  const quality = 0.95;

  return Math.round(availability * performance * quality * 100);
}

export default function DashboardPage() {
  const [machines, setMachines] = useState<Machine[]>([]);
  const [downtimeLogs, setDowntimeLogs] = useState<DowntimeLog[]>([]);
  const [shifts, setShifts] = useState<Shift[]>([]);
  const [activeView, setActiveView] = useState("mission");
  // Narrow-viewport nav: below 1180px the sidebar is off-canvas; the topbar
  // hamburger toggles it, and navigating closes it.
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  // Which Overview card group is open. Only the active group's cards mount, so
  // the home stays glanceable and the background polling stays light.
  const [overviewTab, setOverviewTab] = useState("performance");
  // Escalation the briefing hero deep-linked to, so the Escalation Center can
  // highlight + scroll to it when opened from an "⚡ escalated" pill.
  const [focusedEscalationId, setFocusedEscalationId] = useState<number | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<typeof NAV_ITEMS>([]);
  // Global entity search: typed hits (machines, orders, documents, …) from
  // GET /search, each carrying the view that opens it. Debounced.
  type EntityHit = { type: string; id: number; label: string; sublabel: string; view: string };
  const [entityResults, setEntityResults] = useState<EntityHit[]>([]);
  const searchDebounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  function queryEntities(q: string) {
    if (searchDebounce.current) clearTimeout(searchDebounce.current);
    if (q.trim().length < 2) {
      setEntityResults([]);
      return;
    }
    searchDebounce.current = setTimeout(async () => {
      try {
        const r = await apiGet<{ results: EntityHit[] }>(`/search?q=${encodeURIComponent(q.trim())}`);
        setEntityResults((r.results || []).filter((h) => canRoleSeeView(h.view, role, isFounder)));
      } catch {
        setEntityResults([]);
      }
    }, 250);
  }

  const [name, setName] = useState("");
  const [status, setStatus] = useState("Running");
  const [utilization, setUtilization] = useState(0);
  const [downtime, setDowntime] = useState("0 min");

  const [selectedMachineId, setSelectedMachineId] = useState("");
  const [reason, setReason] = useState("Material Shortage");
  const [duration, setDuration] = useState("");
  const [notes, setNotes] = useState("");

  const [shiftName, setShiftName] = useState("");
  const [targetOutput, setTargetOutput] = useState(0);
  const [actualOutput, setActualOutput] = useState(0);

  const [machineEvents, setMachineEvents] = useState<MachineEvent[]>([]);
  const [machineStateSummary, setMachineStateSummary] = useState<
    MachineStateSummaryType[]
  >([]);

  const [workOrders, setWorkOrders] = useState<WorkOrder[]>([]);
  const [workOrderAnalytics, setWorkOrderAnalytics] =
    useState<WorkOrderAnalytics | null>(null);
  const [workOrderForm, setWorkOrderForm] = useState({
    work_order_no: "",
    part_number: "",
    batch_number: "",
    machine_id: "",
    target_quantity: 0,
    actual_quantity: 0,
    status: "Planned",
  });

  const [productionPlans, setProductionPlans] = useState<ProductionPlan[]>([]);
  const [productionPlanAnalytics, setProductionPlanAnalytics] =
    useState<ProductionPlanAnalytics | null>(null);
  const [productionPlanForm, setProductionPlanForm] = useState({
    plan_no: "",
    work_order_id: "",
    machine_id: "",
    planned_quantity: 0,
    actual_quantity: 0,
    plan_date: new Date().toISOString().slice(0, 10),
    shift_name: "Shift A",
    status: "Planned",
  });

  const [predictiveRisks, setPredictiveRisks] = useState<PredictiveRisk[]>([]);

  const [executiveOee, setExecutiveOee] = useState<ExecutiveOee | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([]);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [reports, setReports] = useState<ReportRequest[]>([]);
  const [systemHealth, setSystemHealth] = useState<SystemHealth | null>(null);
  const [finalSummary, setFinalSummary] = useState<FinalExecutiveSummary | null>(null);
  const [reportForm, setReportForm] = useState({ report_no: "", report_type: "Executive Summary", requested_by: "Admin", format: "PDF", status: "Generated", notes: "" });

  const [tenants, setTenants] = useState<CompanyTenant[]>([]);
  const [saasAnalytics, setSaasAnalytics] = useState<SaaSAnalytics | null>(null);
  const [tenantForm, setTenantForm] = useState({ company_code: "", company_name: "", industry: "", plan_name: "Starter", subscription_status: "Trial", seats: 5, monthly_fee: 0 });
  const [costRecords, setCostRecords] = useState<CostRecord[]>([]);
  const [costingAnalytics, setCostingAnalytics] = useState<CostingAnalytics | null>(null);
  const [costForm, setCostForm] = useState({ cost_no: "", cost_type: "Material", reference_type: "", reference_id: 0, description: "", amount: 0, department: "Production" });
  const [operatorExecutions, setOperatorExecutions] = useState<OperatorJobExecution[]>([]);
  const [operatorAnalytics, setOperatorAnalytics] = useState<OperatorAnalytics | null>(null);
  const [operatorForm, setOperatorForm] = useState({ execution_no: "", operator_name: "", machine_id: "", work_order_id: "", production_plan_id: "", job_status: "Started", good_count: 0, rejected_count: 0, notes: "" });

  const [iotTelemetry, setIotTelemetry] = useState<IoTTelemetry[]>([]);
  const [iotCommand, setIotCommand] = useState<IoTCommandCenter | null>(null);
  const [iotForm, setIotForm] = useState({ machine_id: "", signal_name: "status", signal_value: "Running", numeric_value: 0, unit: "", source: "Manual" });
  const [aiRecommendations, setAiRecommendations] = useState<AIRecommendation[]>([]);
  const [aiInsights, setAiInsights] = useState<AIInsights | null>(null);

  const [documents, setDocuments] = useState<ComplianceDocument[]>([]);
  const [documentAnalytics, setDocumentAnalytics] = useState<DocumentAnalytics | null>(null);
  const [documentForm, setDocumentForm] = useState({ document_no: "", title: "", document_type: "SOP", department: "Production", version: "1.0", owner: "QA Lead", approval_status: "Draft", review_due_date: new Date().toISOString().slice(0,10), storage_link: "", notes: "" });
  const [maintenanceTasks, setMaintenanceTasks] = useState<MaintenanceTask[]>([]);
  const [maintenanceAnalytics, setMaintenanceAnalytics] = useState<MaintenanceAnalytics | null>(null);
  const [maintenanceForm, setMaintenanceForm] = useState({ task_no: "", machine_id: "", task_type: "Preventive", priority: "Medium", assigned_to: "Maintenance", planned_date: new Date().toISOString().slice(0,10), downtime_minutes: 0, spare_parts_used: "", status: "Open", notes: "" });
  const [productionSchedules, setProductionSchedules] = useState<ProductionSchedule[]>([]);
  const [scheduleAnalytics, setScheduleAnalytics] = useState<ScheduleAnalytics | null>(null);
  const [scheduleForm, setScheduleForm] = useState({ schedule_no: "", work_order_id: "", production_plan_id: "", machine_id: "", shift_name: "Shift A", scheduled_date: new Date().toISOString().slice(0,10), priority: "Medium", planned_quantity: 0, estimated_minutes: 480, status: "Scheduled", notes: "" });

  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [purchaseOrders, setPurchaseOrders] = useState<PurchaseOrder[]>([]);
  const [purchasingAnalytics, setPurchasingAnalytics] = useState<PurchasingAnalytics | null>(null);
  const [supplierForm, setSupplierForm] = useState({
    supplier_code: "",
    supplier_name: "",
    contact_person: "",
    email: "",
    phone: "",
    category: "",
    status: "Active",
  });
  const [poForm, setPoForm] = useState({
    po_no: "",
    supplier_id: "",
    item_id: "",
    item_name: "",
    order_quantity: 0,
    received_quantity: 0,
    unit: "pcs",
    expected_delivery_date: new Date().toISOString().slice(0, 10),
    status: "Open",
    notes: "",
  });

  const [customerOrders, setCustomerOrders] = useState<CustomerOrder[]>([]);
  const [customerOrderAnalytics, setCustomerOrderAnalytics] = useState<CustomerOrderAnalytics | null>(null);
  const [customerOrderForm, setCustomerOrderForm] = useState({
    order_no: "",
    customer_name: "",
    product_name: "",
    linked_work_order_id: "",
    linked_production_plan_id: "",
    order_quantity: 0,
    dispatched_quantity: 0,
    priority: "Medium",
    due_date: new Date().toISOString().slice(0, 10),
    status: "Pending",
    notes: "",
  });

  const [factoryNodes, setFactoryNodes] = useState<FactoryLayoutNode[]>([]);
  const [factoryCommandCenter, setFactoryCommandCenter] = useState<FactoryCommandCenter | null>(null);
  const [factoryNodeForm, setFactoryNodeForm] = useState({
    machine_id: "",
    node_name: "",
    node_type: "Machine",
    x_position: 40,
    y_position: 50,
    width: 180,
    height: 110,
    zone: "Production",
  });

  const [qualityInspections, setQualityInspections] = useState<QualityInspection[]>([]);
  const [qualityAnalytics, setQualityAnalytics] = useState<QualityAnalytics | null>(null);
  const [qualityForm, setQualityForm] = useState({
    inspection_no: "",
    work_order_id: "",
    production_plan_id: "",
    machine_id: "",
    inspector: "Quality Inspector",
    inspected_quantity: 0,
    passed_quantity: 0,
    failed_quantity: 0,
    defect_category: "",
    rework_quantity: 0,
    scrap_quantity: 0,
    status: "Open",
    notes: "",
  });

  const [inventoryItems, setInventoryItems] = useState<InventoryItem[]>([]);
  const [inventoryTransactions, setInventoryTransactions] = useState<InventoryTransaction[]>([]);
  const [inventoryAnalytics, setInventoryAnalytics] = useState<InventoryAnalytics | null>(null);
  const [inventoryItemForm, setInventoryItemForm] = useState({
    item_code: "",
    item_name: "",
    category: "Raw Material",
    supplier: "",
    unit: "pcs",
    current_stock: 0,
    reorder_level: 0,
    location: "",
  });
  const [inventoryTransactionForm, setInventoryTransactionForm] = useState({
    item_id: "",
    transaction_type: "Receive",
    quantity: 0,
    reference: "",
    notes: "",
  });

  const [escalations, setEscalations] = useState<Escalation[]>([]);
  const [escalationAnalytics, setEscalationAnalytics] = useState<EscalationAnalytics | null>(null);
  const [escalationForm, setEscalationForm] = useState({
    machine_id: "",
    title: "",
    severity: "High",
    owner: "Unassigned",
    department: "Maintenance",
    status: "Open",
    source: "Manual",
    notes: "",
  });

  const [liveStatus, setLiveStatus] = useState<
    "connected" | "disconnected" | "error"
  >("disconnected");
  const [lastLiveEvent, setLastLiveEvent] = useState(
    "Waiting for live events..."
  );

  const [plan, setPlan] = useState<PlanName>("demo");
  const homeTenant = getUserTenant();          // the tenant baked into the login token
  const isFounder = homeTenant === "DEFAULT";  // only the internal/founder account may switch companies
  const [company, setCompany] = useState(homeTenant);
  const role = getUserRole();
  const userName = getUserName();
  const isAdmin = role === "Admin";
  const isSupervisor = role === "Supervisor";
  const isAdminOrSupervisor = isAdmin || isSupervisor;

  useEffect(() => {
    const stored = localStorage.getItem("plan") as PlanName | null;
    if (stored && stored in PLAN_MODULES) setPlan(stored);
    if (!isFounder) {
      // Client login: locked to its own company, cannot switch.
      setCompany(homeTenant);
      localStorage.setItem("company", homeTenant);
      setActiveView("inventory");
      return;
    }
    const storedCompany = localStorage.getItem("company");
    if (storedCompany) {
      setCompany(storedCompany);
      if (storedCompany === "GMATS") setActiveView("inventory");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function switchCompany(code: string) {
    if (!isFounder) return;   // clients cannot switch companies
    localStorage.setItem("company", code);
    // Full reload so every widget refetches under the new tenant scope — the
    // X-Tenant preview header is derived from localStorage in lib/api.
    window.location.reload();
  }

  const [users, setUsers] = useState<User[]>([]);

  async function loadUsers() {
    if (!isAdmin) return;
    try {
      setUsers(await apiGet<User[]>("/users"));
    } catch {
      setUsers([]);
    }
  }

  useEffect(() => {
    loadUsers();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function addEmployee(username: string, password: string, role: string) {
    await apiPost("/users", { username, password, role });
    await loadUsers();
  }

  async function updateUserRole(id: number, role: string) {
    try {
      await apiPatch(`/users/${id}/role`, { role });
      loadUsers();
    } catch (error) {
      console.error(error);
    }
  }

  async function deleteUserAccount(id: number) {
    try {
      await apiDelete(`/users/${id}`);
      loadUsers();
    } catch (error) {
      console.error(error);
    }
  }

  async function resetUserPassword(id: number, password: string) {
    await apiPatch(`/users/${id}/password`, { password });
  }

  // Per-tenant licensing + branding fetched from the platform layer.
  const [tenantCfg, setTenantCfg] = useState<{ enabled_modules: string[]; brand_name: string; brand_color: string } | null>(null);
  useEffect(() => {
    apiGet<{ enabled_modules: string[]; brand_name: string; brand_color: string }>("/tenant-config")
      .then(setTenantCfg)
      .catch(() => {});
  }, []);

  // Effective modules come from the tenant's licence; core + admin are always
  // available so no one is locked out of basics or account management.
  const enabledModules = (
    tenantCfg
      ? Array.from(new Set([...tenantCfg.enabled_modules, "core", "admin"]))
      : getEnabledModules(plan)
  ) as ReturnType<typeof getEnabledModules>;
  const brandName = tenantCfg?.brand_name || "AMP";

  // If the licence that just loaded doesn't cover the current view (e.g. a
  // Starter-plan tenant landing on the legacy inventory default), snap to
  // Overview instead of a locked-module screen.
  useEffect(() => {
    if (tenantCfg && !isViewEnabled(activeView, enabledModules)) setActiveView("overview");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantCfg]);

  function logout() {
    localStorage.clear();
    sessionStorage.clear();
    window.location.href = "/login";
  }

  async function fetchAll() {
    try {
      const [machineData, logData, shiftData] = await Promise.all([
        apiGet<Machine[]>("/machines"),
        apiGet<DowntimeLog[]>("/downtime-logs"),
        apiGet<Shift[]>("/shifts"),
      ]);

      setMachines(Array.isArray(machineData) ? machineData : []);
      setDowntimeLogs(Array.isArray(logData) ? logData : []);
      setShifts(Array.isArray(shiftData) ? shiftData : []);

      const optionalCalls = await Promise.allSettled([
        apiGet<MachineEvent[]>("/analytics/machine-timeline"),
        apiGet<MachineStateSummaryType[]>("/analytics/machine-state-summary"),
        apiGet<WorkOrder[]>("/work-orders"),
        apiGet<WorkOrderAnalytics>("/analytics/work-orders"),
        apiGet<PredictiveRisk[]>("/analytics/predictive-maintenance"),
        apiGet<ProductionPlan[]>("/production-plans"),
        apiGet<ProductionPlanAnalytics>("/analytics/production-plans"),
        apiGet<Escalation[]>("/escalations"),
        apiGet<EscalationAnalytics>("/analytics/escalations"),
        apiGet<InventoryItem[]>("/inventory/items"),
        apiGet<InventoryTransaction[]>("/inventory/transactions"),
        apiGet<InventoryAnalytics>("/analytics/inventory"),
        apiGet<QualityInspection[]>("/quality/inspections"),
        apiGet<QualityAnalytics>("/analytics/quality"),
        apiGet<ExecutiveOee>("/analytics/executive-oee"),
        apiGet<FactoryLayoutNode[]>("/factory-layout/nodes"),
        apiGet<FactoryCommandCenter>("/analytics/factory-command-center"),
        apiGet<CustomerOrder[]>("/customer-orders"),
        apiGet<CustomerOrderAnalytics>("/analytics/customer-orders"),
        apiGet<Supplier[]>("/suppliers"),
        apiGet<PurchaseOrder[]>("/purchase-orders"),
        apiGet<PurchasingAnalytics>("/analytics/purchasing"),
        apiGet<ComplianceDocument[]>("/documents"),
        apiGet<DocumentAnalytics>("/analytics/documents"),
        apiGet<MaintenanceTask[]>("/maintenance/tasks"),
        apiGet<MaintenanceAnalytics>("/analytics/maintenance"),
        apiGet<ProductionSchedule[]>("/production-schedules"),
        apiGet<ScheduleAnalytics>("/analytics/production-schedules"),
        apiGet<IoTTelemetry[]>("/iot/telemetry"),
        apiGet<IoTCommandCenter>("/analytics/iot-command"),
        apiGet<AIRecommendation[]>("/ai/recommendations"),
        apiGet<AIInsights>("/analytics/ai-insights"),
        apiGet<CompanyTenant[]>("/saas/tenants"),
        apiGet<SaaSAnalytics>("/analytics/saas"),
        apiGet<CostRecord[]>("/cost-records"),
        apiGet<CostingAnalytics>("/analytics/costing"),
        apiGet<OperatorJobExecution[]>("/operator/executions"),
        apiGet<OperatorAnalytics>("/analytics/operator-terminal"),
        apiGet<AuditLog[]>("/audit-logs"),
        apiGet<NotificationItem[]>("/notifications"),
        apiGet<ReportRequest[]>("/reports"),
        apiGet<SystemHealth>("/analytics/system-health"),
        apiGet<FinalExecutiveSummary>("/analytics/final-executive-summary"),
      ]);

      if (optionalCalls[0].status === "fulfilled") {
        setMachineEvents(
          Array.isArray(optionalCalls[0].value) ? optionalCalls[0].value : []
        );
      }

      if (optionalCalls[1].status === "fulfilled") {
        setMachineStateSummary(
          Array.isArray(optionalCalls[1].value) ? optionalCalls[1].value : []
        );
      }

      if (optionalCalls[2].status === "fulfilled") {
        setWorkOrders(
          Array.isArray(optionalCalls[2].value) ? optionalCalls[2].value : []
        );
      }

      if (optionalCalls[3].status === "fulfilled") {
        setWorkOrderAnalytics(optionalCalls[3].value);
      }

      if (optionalCalls[4].status === "fulfilled") {
        setPredictiveRisks(
          Array.isArray(optionalCalls[4].value) ? optionalCalls[4].value : []
        );
      }

      if (optionalCalls[5].status === "fulfilled") {
        setProductionPlans(
          Array.isArray(optionalCalls[5].value) ? optionalCalls[5].value : []
        );
      }

      if (optionalCalls[6].status === "fulfilled") {
        setProductionPlanAnalytics(optionalCalls[6].value);
      }

      if (optionalCalls[7].status === "fulfilled") {
        setEscalations(
          Array.isArray(optionalCalls[7].value) ? optionalCalls[7].value : []
        );
      }

      if (optionalCalls[8].status === "fulfilled") {
        setEscalationAnalytics(optionalCalls[8].value);
      }

      if (optionalCalls[9].status === "fulfilled") {
        setInventoryItems(
          Array.isArray(optionalCalls[9].value) ? optionalCalls[9].value : []
        );
      }

      if (optionalCalls[10].status === "fulfilled") {
        setInventoryTransactions(
          Array.isArray(optionalCalls[10].value) ? optionalCalls[10].value : []
        );
      }

      if (optionalCalls[11].status === "fulfilled") {
        setInventoryAnalytics(optionalCalls[11].value);
      }

      if (optionalCalls[12].status === "fulfilled") {
        setQualityInspections(
          Array.isArray(optionalCalls[12].value) ? optionalCalls[12].value : []
        );
      }

      if (optionalCalls[13].status === "fulfilled") {
        setQualityAnalytics(optionalCalls[13].value);
      }

      if (optionalCalls[14].status === "fulfilled") {
        setExecutiveOee(optionalCalls[14].value);
      }

      if (optionalCalls[15].status === "fulfilled") {
        setFactoryNodes(
          Array.isArray(optionalCalls[15].value) ? optionalCalls[15].value : []
        );
      }

      if (optionalCalls[16].status === "fulfilled") {
        setFactoryCommandCenter(optionalCalls[16].value);
      }

      if (optionalCalls[17].status === "fulfilled") {
        setCustomerOrders(
          Array.isArray(optionalCalls[17].value) ? optionalCalls[17].value : []
        );
      }

      if (optionalCalls[18].status === "fulfilled") {
        setCustomerOrderAnalytics(optionalCalls[18].value);
      }

      if (optionalCalls[19].status === "fulfilled") {
        setSuppliers(
          Array.isArray(optionalCalls[19].value) ? optionalCalls[19].value : []
        );
      }

      if (optionalCalls[20].status === "fulfilled") {
        setPurchaseOrders(
          Array.isArray(optionalCalls[20].value) ? optionalCalls[20].value : []
        );
      }

      if (optionalCalls[21].status === "fulfilled") {
        setPurchasingAnalytics(optionalCalls[21].value);
      }

      if (optionalCalls[22].status === "fulfilled") setDocuments(Array.isArray(optionalCalls[22].value) ? optionalCalls[22].value : []);
      if (optionalCalls[23].status === "fulfilled") setDocumentAnalytics(optionalCalls[23].value);
      if (optionalCalls[24].status === "fulfilled") setMaintenanceTasks(Array.isArray(optionalCalls[24].value) ? optionalCalls[24].value : []);
      if (optionalCalls[25].status === "fulfilled") setMaintenanceAnalytics(optionalCalls[25].value);
      if (optionalCalls[26].status === "fulfilled") setProductionSchedules(Array.isArray(optionalCalls[26].value) ? optionalCalls[26].value : []);
      if (optionalCalls[27].status === "fulfilled") setScheduleAnalytics(optionalCalls[27].value);
      if (optionalCalls[28].status === "fulfilled") setIotTelemetry(Array.isArray(optionalCalls[28].value) ? optionalCalls[28].value : []);
      if (optionalCalls[29].status === "fulfilled") setIotCommand(optionalCalls[29].value);
      if (optionalCalls[30].status === "fulfilled") setAiRecommendations(Array.isArray(optionalCalls[30].value) ? optionalCalls[30].value : []);
      if (optionalCalls[31].status === "fulfilled") setAiInsights(optionalCalls[31].value);
      if (optionalCalls[32].status === "fulfilled") setTenants(Array.isArray(optionalCalls[32].value) ? optionalCalls[32].value : []);
      if (optionalCalls[33].status === "fulfilled") setSaasAnalytics(optionalCalls[33].value);
      if (optionalCalls[34].status === "fulfilled") setCostRecords(Array.isArray(optionalCalls[34].value) ? optionalCalls[34].value : []);
      if (optionalCalls[35].status === "fulfilled") setCostingAnalytics(optionalCalls[35].value);
      if (optionalCalls[36].status === "fulfilled") setOperatorExecutions(Array.isArray(optionalCalls[36].value) ? optionalCalls[36].value : []);
      if (optionalCalls[37].status === "fulfilled") setOperatorAnalytics(optionalCalls[37].value);
      if (optionalCalls[38].status === "fulfilled") setAuditLogs(Array.isArray(optionalCalls[38].value) ? optionalCalls[38].value : []);
      if (optionalCalls[39].status === "fulfilled") setNotifications(Array.isArray(optionalCalls[39].value) ? optionalCalls[39].value : []);
      if (optionalCalls[40].status === "fulfilled") setReports(Array.isArray(optionalCalls[40].value) ? optionalCalls[40].value : []);
      if (optionalCalls[41].status === "fulfilled") setSystemHealth(optionalCalls[41].value);
      if (optionalCalls[42].status === "fulfilled") setFinalSummary(optionalCalls[42].value);
    } catch (error) {
      console.error(error);
    }
  }

  useEffect(() => {
    if (!getToken()) {
      window.location.href = "/login";
      return;
    }

    fetchAll();

    const interval = setInterval(() => {
      fetchAll();
    }, 3000);

    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (!getToken()) return;

    const socket = connectLiveSocket(
      (event: LiveEvent) => {
        if (event.event === "connected") {
          setLastLiveEvent(event.message || "Connected to live stream");
          return;
        }

        if (event.event === "heartbeat") return;

        if (event.event === "machine_update" && event.machine) {
          setLastLiveEvent(
            `${event.machine.name} → ${event.machine.status} | ${event.machine.utilization}%`
          );

          setMachines((prev) => {
            const exists = prev.some(
              (machine) => machine.id === event.machine!.id
            );

            if (!exists) {
              return [
                ...prev,
                {
                  id: event.machine!.id,
                  name: event.machine!.name,
                  status: event.machine!.status,
                  utilization: event.machine!.utilization,
                  downtime: event.machine!.downtime || "0 min",
                },
              ];
            }

            return prev.map((machine) =>
              machine.id === event.machine!.id
                ? {
                    ...machine,
                    status: event.machine!.status,
                    utilization: event.machine!.utilization,
                    downtime: event.machine!.downtime || machine.downtime,
                  }
                : machine
            );
          });

          if (event.timeline) {
            const syntheticEvent: MachineEvent = {
              id: Date.now(),
              machine_id: event.machine.id,
              machine_name: event.machine.name,
              old_status: event.timeline.old_status,
              new_status: event.timeline.new_status,
              utilization: event.machine.utilization,
              source: event.source || "websocket",
              created_at: new Date().toISOString(),
            };

            setMachineEvents((prev) => [syntheticEvent, ...prev].slice(0, 80));
          }

          fetchAll();
        }
      },
      setLiveStatus
    );

    return () => {
      socket.close();
    };
  }, []);

  async function addMachine(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<Machine>("/machines", {
        name,
        status,
        utilization,
        downtime,
      });

      setName("");
      setStatus("Running");
      setUtilization(0);
      setDowntime("0 min");

      fetchAll();
    } catch (error) {
      console.error(error);
    }
  }

  async function deleteMachine(id: number) {
    try {
      await apiDelete(`/machines/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
    }
  }

  async function updateMachineStatus(id: number, newStatus: string) {
    try {
      await apiPatch<Machine>(
        `/machines/${id}/status?status=${encodeURIComponent(newStatus)}`
      );
      fetchAll();
    } catch (error) {
      console.error(error);
    }
  }

  async function addDowntimeLog(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<DowntimeLog>("/downtime-logs", {
        machine_id: Number(selectedMachineId),
        reason,
        duration,
        notes,
      });

      setSelectedMachineId("");
      setReason("Material Shortage");
      setDuration("");
      setNotes("");

      fetchAll();
    } catch (error) {
      console.error(error);
    }
  }

  async function addShift(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<Shift>("/shifts", {
        shift_name: shiftName,
        target_output: targetOutput,
        actual_output: actualOutput,
      });

      setShiftName("");
      setTargetOutput(0);
      setActualOutput(0);

      fetchAll();
    } catch (error) {
      console.error(error);
    }
  }

  async function createWorkOrder(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<WorkOrder>("/work-orders", {
        ...workOrderForm,
        machine_id: Number(workOrderForm.machine_id),
        target_quantity: Number(workOrderForm.target_quantity),
        actual_quantity: Number(workOrderForm.actual_quantity),
      });

      setWorkOrderForm({
        work_order_no: "",
        part_number: "",
        batch_number: "",
        machine_id: "",
        target_quantity: 0,
        actual_quantity: 0,
        status: "Planned",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create work order. Check backend logs.");
    }
  }

  async function updateWorkOrder(
    id: number,
    actualQuantity: number,
    status?: string
  ) {
    try {
      await apiPatch<WorkOrder>(`/work-orders/${id}`, {
        actual_quantity: actualQuantity,
        ...(status ? { status } : {}),
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to update work order.");
    }
  }

  async function deleteWorkOrder(id: number) {
    try {
      await apiDelete(`/work-orders/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to delete work order.");
    }
  }

  async function createProductionPlan(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<ProductionPlan>("/production-plans", {
        ...productionPlanForm,
        work_order_id: Number(productionPlanForm.work_order_id),
        machine_id: Number(productionPlanForm.machine_id),
        planned_quantity: Number(productionPlanForm.planned_quantity),
        actual_quantity: Number(productionPlanForm.actual_quantity),
      });

      setProductionPlanForm({
        plan_no: "",
        work_order_id: "",
        machine_id: "",
        planned_quantity: 0,
        actual_quantity: 0,
        plan_date: new Date().toISOString().slice(0, 10),
        shift_name: "Shift A",
        status: "Planned",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create production plan. Check backend logs.");
    }
  }

  async function updateProductionPlan(
    id: number,
    actualQuantity: number,
    status?: string
  ) {
    try {
      await apiPatch<ProductionPlan>(`/production-plans/${id}`, {
        actual_quantity: actualQuantity,
        ...(status ? { status } : {}),
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to update production plan.");
    }
  }

  async function deleteProductionPlan(id: number) {
    try {
      await apiDelete(`/production-plans/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to delete production plan.");
    }
  }

  async function createEscalation(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<Escalation>("/escalations", {
        ...escalationForm,
        machine_id: escalationForm.machine_id
          ? Number(escalationForm.machine_id)
          : null,
      });

      setEscalationForm({
        machine_id: "",
        title: "",
        severity: "High",
        owner: "Unassigned",
        department: "Maintenance",
        status: "Open",
        source: "Manual",
        notes: "",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create escalation.");
    }
  }

  async function updateEscalation(
    id: number,
    status: string,
    owner?: string,
    department?: string,
    resolutionNotes?: string
  ) {
    try {
      await apiPatch<Escalation>(`/escalations/${id}`, {
        status,
        owner,
        department,
        resolution_notes: resolutionNotes,
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to update escalation.");
    }
  }

  async function deleteEscalation(id: number) {
    try {
      await apiDelete(`/escalations/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to delete escalation.");
    }
  }

  async function generateEscalationsFromSmartAlerts() {
    try {
      await apiPost<{ created: number }>("/escalations/from-smart-alerts", {});
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to generate smart escalations.");
    }
  }

  async function createInventoryItem(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<InventoryItem>("/inventory/items", inventoryItemForm);

      setInventoryItemForm({
        item_code: "",
        item_name: "",
        category: "Raw Material",
        supplier: "",
        unit: "pcs",
        current_stock: 0,
        reorder_level: 0,
        location: "",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create inventory item.");
    }
  }

  async function updateInventoryItem(
    id: number,
    currentStock: number,
    reorderLevel: number
  ) {
    try {
      await apiPatch<InventoryItem>(`/inventory/items/${id}`, {
        current_stock: currentStock,
        reorder_level: reorderLevel,
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to update inventory item.");
    }
  }

  async function deleteInventoryItem(id: number) {
    try {
      await apiDelete(`/inventory/items/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to delete inventory item.");
    }
  }

  async function createInventoryTransaction(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<InventoryTransaction>("/inventory/transactions", {
        ...inventoryTransactionForm,
        item_id: Number(inventoryTransactionForm.item_id),
        quantity: Number(inventoryTransactionForm.quantity),
      });

      setInventoryTransactionForm({
        item_id: "",
        transaction_type: "Receive",
        quantity: 0,
        reference: "",
        notes: "",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to post inventory transaction.");
    }
  }

  async function generateLowStockEscalations() {
    try {
      await apiPost<{ created: number }>("/inventory/generate-low-stock-escalations", {});
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to generate low stock escalations.");
    }
  }

  async function createQualityInspection(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<QualityInspection>("/quality/inspections", {
        ...qualityForm,
        work_order_id: qualityForm.work_order_id ? Number(qualityForm.work_order_id) : null,
        production_plan_id: qualityForm.production_plan_id ? Number(qualityForm.production_plan_id) : null,
        machine_id: qualityForm.machine_id ? Number(qualityForm.machine_id) : null,
        inspected_quantity: Number(qualityForm.inspected_quantity),
        passed_quantity: Number(qualityForm.passed_quantity),
        failed_quantity: Number(qualityForm.failed_quantity),
        rework_quantity: Number(qualityForm.rework_quantity),
        scrap_quantity: Number(qualityForm.scrap_quantity),
      });

      setQualityForm({
        inspection_no: "",
        work_order_id: "",
        production_plan_id: "",
        machine_id: "",
        inspector: "Quality Inspector",
        inspected_quantity: 0,
        passed_quantity: 0,
        failed_quantity: 0,
        defect_category: "",
        rework_quantity: 0,
        scrap_quantity: 0,
        status: "Open",
        notes: "",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create quality inspection.");
    }
  }

  async function updateQualityInspection(
    id: number,
    passed: number,
    failed: number,
    status?: string,
    defectCategory?: string,
    rework?: number,
    scrap?: number,
    notes?: string
  ) {
    try {
      await apiPatch<QualityInspection>(`/quality/inspections/${id}`, {
        passed_quantity: passed,
        failed_quantity: failed,
        status,
        defect_category: defectCategory,
        rework_quantity: rework,
        scrap_quantity: scrap,
        notes,
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to update quality inspection.");
    }
  }

  async function deleteQualityInspection(id: number) {
    try {
      await apiDelete(`/quality/inspections/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to delete quality inspection.");
    }
  }

  async function generateDefectEscalations() {
    try {
      await apiPost<{ created: number }>("/quality/generate-defect-escalations", {});
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to generate quality escalations.");
    }
  }

  async function createFactoryNode(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<FactoryLayoutNode>("/factory-layout/nodes", {
        ...factoryNodeForm,
        machine_id: factoryNodeForm.machine_id ? Number(factoryNodeForm.machine_id) : null,
      });

      setFactoryNodeForm({
        machine_id: "",
        node_name: "",
        node_type: "Machine",
        x_position: 40,
        y_position: 50,
        width: 180,
        height: 110,
        zone: "Production",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create factory layout node.");
    }
  }

  async function updateFactoryNode(id: number, x: number, y: number, zone: string) {
    try {
      await apiPatch<FactoryLayoutNode>(`/factory-layout/nodes/${id}`, {
        x_position: x,
        y_position: y,
        zone,
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to update factory layout node.");
    }
  }

  async function deleteFactoryNode(id: number) {
    try {
      await apiDelete(`/factory-layout/nodes/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to delete factory layout node.");
    }
  }

  async function autoGenerateFactoryLayout() {
    try {
      await apiPost<{ created: number }>("/factory-layout/auto-generate", {});
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to auto-generate factory layout.");
    }
  }

  async function createCustomerOrder(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<CustomerOrder>("/customer-orders", {
        ...customerOrderForm,
        linked_work_order_id: customerOrderForm.linked_work_order_id
          ? Number(customerOrderForm.linked_work_order_id)
          : null,
        linked_production_plan_id: customerOrderForm.linked_production_plan_id
          ? Number(customerOrderForm.linked_production_plan_id)
          : null,
        order_quantity: Number(customerOrderForm.order_quantity),
        dispatched_quantity: Number(customerOrderForm.dispatched_quantity),
      });

      setCustomerOrderForm({
        order_no: "",
        customer_name: "",
        product_name: "",
        linked_work_order_id: "",
        linked_production_plan_id: "",
        order_quantity: 0,
        dispatched_quantity: 0,
        priority: "Medium",
        due_date: new Date().toISOString().slice(0, 10),
        status: "Pending",
        notes: "",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create customer order.");
    }
  }

  async function updateCustomerOrder(
    id: number,
    dispatchedQty: number,
    status?: string,
    priority?: string
  ) {
    try {
      await apiPatch<CustomerOrder>(`/customer-orders/${id}`, {
        dispatched_quantity: dispatchedQty,
        status,
        priority,
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to update customer order.");
    }
  }

  async function deleteCustomerOrder(id: number) {
    try {
      await apiDelete(`/customer-orders/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to delete customer order.");
    }
  }

  async function generateLateOrderEscalations() {
    try {
      await apiPost<{ created: number }>("/customer-orders/generate-late-order-escalations", {});
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to generate late order escalations.");
    }
  }

  async function createSupplier(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<Supplier>("/suppliers", supplierForm);

      setSupplierForm({
        supplier_code: "",
        supplier_name: "",
        contact_person: "",
        email: "",
        phone: "",
        category: "",
        status: "Active",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create supplier.");
    }
  }

  async function updateSupplier(id: number, status: string) {
    try {
      await apiPatch<Supplier>(`/suppliers/${id}`, { status });
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to update supplier.");
    }
  }

  async function deleteSupplier(id: number) {
    try {
      await apiDelete(`/suppliers/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to delete supplier.");
    }
  }

  async function createPurchaseOrder(e: React.FormEvent) {
    e.preventDefault();

    try {
      await apiPost<PurchaseOrder>("/purchase-orders", {
        ...poForm,
        supplier_id: Number(poForm.supplier_id),
        item_id: poForm.item_id ? Number(poForm.item_id) : null,
        order_quantity: Number(poForm.order_quantity),
        received_quantity: Number(poForm.received_quantity),
      });

      setPoForm({
        po_no: "",
        supplier_id: "",
        item_id: "",
        item_name: "",
        order_quantity: 0,
        received_quantity: 0,
        unit: "pcs",
        expected_delivery_date: new Date().toISOString().slice(0, 10),
        status: "Open",
        notes: "",
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create purchase order.");
    }
  }

  async function updatePurchaseOrder(id: number, receivedQty: number, status?: string) {
    try {
      await apiPatch<PurchaseOrder>(`/purchase-orders/${id}`, {
        received_quantity: receivedQty,
        status,
      });

      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to update purchase order.");
    }
  }

  async function deletePurchaseOrder(id: number) {
    try {
      await apiDelete(`/purchase-orders/${id}`);
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to delete purchase order.");
    }
  }

  async function generateOverduePoEscalations() {
    try {
      await apiPost<{ created: number }>("/purchase-orders/generate-overdue-escalations", {});
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to generate overdue PO escalations.");
    }
  }

  async function createDocument(e: React.FormEvent) {
    e.preventDefault();
    try {
      await apiPost<ComplianceDocument>("/documents", documentForm);
      setDocumentForm({ document_no: "", title: "", document_type: "SOP", department: "Production", version: "1.0", owner: "QA Lead", approval_status: "Draft", review_due_date: new Date().toISOString().slice(0,10), storage_link: "", notes: "" });
      fetchAll();
    } catch (error) { console.error(error); alert("Failed to create document."); }
  }

  async function updateDocument(id: number, approvalStatus: string, version?: string) {
    try { await apiPatch<ComplianceDocument>(`/documents/${id}`, { approval_status: approvalStatus, version }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to update document."); }
  }

  async function deleteDocument(id: number) {
    try { await apiDelete(`/documents/${id}`); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to delete document."); }
  }

  async function generateDocumentReviewEscalations() {
    try { await apiPost<{ created: number }>("/documents/generate-review-escalations", {}); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to generate document escalations."); }
  }

  async function createMaintenanceTask(e: React.FormEvent) {
    e.preventDefault();
    try {
      await apiPost<MaintenanceTask>("/maintenance/tasks", { ...maintenanceForm, machine_id: Number(maintenanceForm.machine_id), downtime_minutes: Number(maintenanceForm.downtime_minutes) });
      setMaintenanceForm({ task_no: "", machine_id: "", task_type: "Preventive", priority: "Medium", assigned_to: "Maintenance", planned_date: new Date().toISOString().slice(0,10), downtime_minutes: 0, spare_parts_used: "", status: "Open", notes: "" });
      fetchAll();
    } catch (error) { console.error(error); alert("Failed to create maintenance task."); }
  }

  async function updateMaintenanceTask(id: number, status: string, downtime?: number) {
    try { await apiPatch<MaintenanceTask>(`/maintenance/tasks/${id}`, { status, downtime_minutes: downtime }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to update maintenance task."); }
  }

  async function deleteMaintenanceTask(id: number) {
    try { await apiDelete(`/maintenance/tasks/${id}`); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to delete maintenance task."); }
  }

  async function generateMaintenanceOverdueEscalations() {
    try { await apiPost<{ created: number }>("/maintenance/generate-overdue-escalations", {}); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to generate maintenance escalations."); }
  }

  async function createProductionSchedule(e: React.FormEvent) {
    e.preventDefault();
    try {
      await apiPost<ProductionSchedule>("/production-schedules", { ...scheduleForm, work_order_id: scheduleForm.work_order_id ? Number(scheduleForm.work_order_id) : null, production_plan_id: scheduleForm.production_plan_id ? Number(scheduleForm.production_plan_id) : null, machine_id: Number(scheduleForm.machine_id), planned_quantity: Number(scheduleForm.planned_quantity), estimated_minutes: Number(scheduleForm.estimated_minutes) });
      setScheduleForm({ schedule_no: "", work_order_id: "", production_plan_id: "", machine_id: "", shift_name: "Shift A", scheduled_date: new Date().toISOString().slice(0,10), priority: "Medium", planned_quantity: 0, estimated_minutes: 480, status: "Scheduled", notes: "" });
      fetchAll();
    } catch (error) { console.error(error); alert("Failed to create production schedule."); }
  }

  async function updateProductionSchedule(id: number, status: string, priority?: string) {
    try { await apiPatch<ProductionSchedule>(`/production-schedules/${id}`, { status, priority }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to update schedule."); }
  }

  async function deleteProductionSchedule(id: number) {
    try { await apiDelete(`/production-schedules/${id}`); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to delete schedule."); }
  }

  async function createIotTelemetry(e: React.FormEvent) {
    e.preventDefault();
    try {
      await apiPost<IoTTelemetry>("/iot/telemetry", { ...iotForm, machine_id: Number(iotForm.machine_id), numeric_value: Number(iotForm.numeric_value) });
      setIotForm({ machine_id: "", signal_name: "status", signal_value: "Running", numeric_value: 0, unit: "", source: "Manual" });
      fetchAll();
    } catch (error) { console.error(error); alert("Failed to post IoT signal."); }
  }

  async function generateAiRecommendations() {
    try { await apiPost<{ created: number }>("/ai/generate-recommendations", {}); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to generate AI recommendations."); }
  }

  async function updateAiRecommendation(id: number, status: string) {
    try { await apiPatch<AIRecommendation>(`/ai/recommendations/${id}`, { status }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to update AI recommendation."); }
  }

  async function createTenant(e: React.FormEvent) {
    e.preventDefault();
    try { await apiPost<CompanyTenant>("/saas/tenants", tenantForm); setTenantForm({ company_code: "", company_name: "", industry: "", plan_name: "Starter", subscription_status: "Trial", seats: 5, monthly_fee: 0 }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to create tenant."); }
  }

  async function updateTenant(id: number, status: string) {
    try { await apiPatch<CompanyTenant>(`/saas/tenants/${id}`, { subscription_status: status }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to update tenant."); }
  }

  async function deleteTenant(id: number) {
    const row = tenants.find((t) => t.id === id);
    const name = row ? `${row.company_name} (${row.company_code})` : "this tenant";
    if (!window.confirm(`Remove ${name} from the tenant registry?`)) return;
    // Second, separate decision: purging is irreversible and much bigger.
    const purge = window.confirm(
      `Also PERMANENTLY delete all of ${name}'s data — machines, production history, orders, users, licence?\n\n` +
      `OK = wipe everything (cannot be undone)\nCancel = keep the data, remove only the registry entry`
    );
    try { await apiDelete(`/saas/tenants/${id}${purge ? "?purge=true" : ""}`); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to delete tenant."); }
  }

  // Self-service password rotation (pairs with provisioned temp passwords).
  const [pwOpen, setPwOpen] = useState(false);
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwMsg, setPwMsg] = useState("");
  async function changeOwnPassword(e: React.FormEvent) {
    e.preventDefault();
    try {
      await apiPost("/auth/change-password", { current_password: pwCurrent, new_password: pwNew });
      setPwMsg("Password changed");
      setPwCurrent(""); setPwNew("");
    } catch {
      setPwMsg("Failed — check the current password");
    }
  }

  // One-click tenant admin provisioning. The temporary password appears only in
  // this response — it's held in state for a single display, never persisted.
  const [adminCreds, setAdminCreds] = useState<{ username: string; temporary_password: string; company_code: string } | null>(null);
  async function provisionAdmin(id: number) {
    try {
      setAdminCreds(await apiPost<{ username: string; temporary_password: string; company_code: string }>(`/saas/tenants/${id}/admin`, {}));
      fetchAll();
    } catch (error) {
      console.error(error);
      alert("Failed to create the admin login (it may already exist).");
    }
  }

  async function createCost(e: React.FormEvent) {
    e.preventDefault();
    try { await apiPost<CostRecord>("/cost-records", costForm); setCostForm({ cost_no: "", cost_type: "Material", reference_type: "", reference_id: 0, description: "", amount: 0, department: "Production" }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to create cost."); }
  }

  async function updateCost(id: number, amount: number) {
    try { await apiPatch<CostRecord>(`/cost-records/${id}`, { amount }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to update cost."); }
  }

  async function deleteCost(id: number) {
    try { await apiDelete(`/cost-records/${id}`); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to delete cost."); }
  }

  async function createOperatorExecution(e: React.FormEvent) {
    e.preventDefault();
    try {
      await apiPost<OperatorJobExecution>("/operator/executions", { ...operatorForm, machine_id: Number(operatorForm.machine_id), work_order_id: operatorForm.work_order_id ? Number(operatorForm.work_order_id) : null, production_plan_id: operatorForm.production_plan_id ? Number(operatorForm.production_plan_id) : null, good_count: Number(operatorForm.good_count), rejected_count: Number(operatorForm.rejected_count) });
      setOperatorForm({ execution_no: "", operator_name: "", machine_id: "", work_order_id: "", production_plan_id: "", job_status: "Started", good_count: 0, rejected_count: 0, notes: "" });
      fetchAll();
    } catch (error) { console.error(error); alert("Failed to create operator execution."); }
  }

  async function updateOperatorExecution(id: number, status: string, good: number, reject: number) {
    try { await apiPatch<OperatorJobExecution>(`/operator/executions/${id}`, { job_status: status, good_count: good, rejected_count: reject }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to update operator job."); }
  }

  async function deleteOperatorExecution(id: number) {
    try { await apiDelete(`/operator/executions/${id}`); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to delete operator job."); }
  }

  async function generateSystemNotifications() {
    try { await apiPost<{ created: number }>("/notifications/generate-system-notifications", {}); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to generate notifications."); }
  }

  async function updateNotification(id: number, status: string) {
    try { await apiPatch<NotificationItem>(`/notifications/${id}`, { status }); fetchAll(); }
    catch (error) { console.error(error); alert("Failed to update notification."); }
  }

  async function createReport(e: React.FormEvent) {
    e.preventDefault();
    try {
      await apiPost<ReportRequest>("/reports", reportForm);
      await apiPost<AuditLog>("/audit-logs", { actor: reportForm.requested_by, action: "Generated report request", entity_type: "Report", details: reportForm.report_type });
      setReportForm({ report_no: "", report_type: "Executive Summary", requested_by: "Admin", format: "PDF", status: "Generated", notes: "" });
      fetchAll();
    } catch (error) { console.error(error); alert("Failed to create report request."); }
  }

  function getMachineName(machineId: number) {
    return machines.find((m) => m.id === machineId)?.name || `Machine ${machineId}`;
  }

  const running = machines.filter((m) => m.status === "Running").length;
  const breakdown = machines.filter((m) => m.status === "Breakdown").length;

  const avgUtilization =
    machines.length > 0
      ? Math.round(
          machines.reduce((sum, m) => sum + m.utilization, 0) / machines.length
        )
      : 0;

  const totalDowntimeMinutes = downtimeLogs.reduce(
    (sum, log) => sum + parseDurationToMinutes(log.duration),
    0
  );

  const topReason =
    downtimeLogs.length > 0
      ? Object.entries(
          downtimeLogs.reduce<Record<string, number>>((acc, log) => {
            acc[log.reason] = (acc[log.reason] || 0) + 1;
            return acc;
          }, {})
        ).sort((a, b) => b[1] - a[1])[0][0]
      : "No data";

  const avgShiftEfficiency =
    shifts.length > 0
      ? Math.round(
          shifts.reduce((sum, shift) => {
            if (shift.target_output === 0) return sum;
            return sum + (shift.actual_output / shift.target_output) * 100;
          }, 0) / shifts.length
        )
      : 0;

  const downtimeReasonChartData = Object.entries(
    downtimeLogs.reduce<Record<string, number>>((acc, log) => {
      acc[log.reason] = (acc[log.reason] || 0) + 1;
      return acc;
    }, {})
  ).map(([reason, count]) => ({
    reason,
    count,
  }));

  const activeLabel =
    NAV_ITEMS.find((item) => item.key === activeView)?.label || "Overview";

  function renderSection(viewKey: string, node: React.ReactNode) {
    if (activeView !== viewKey) return null;
    if (!canRoleSeeView(viewKey, role, isFounder)) {
      return (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-10 text-center">
          <div className="text-3xl mb-3">🔒</div>
          <h2 className="text-lg font-semibold text-white">Restricted</h2>
          <p className="text-slate-400 mt-1 text-sm">
            Your role ({role || "Operator"}) doesn&apos;t have access to this section.
            Contact an administrator if you need it.
          </p>
        </div>
      );
    }
    if (!isViewEnabled(viewKey, enabledModules)) {
      return <LockedModuleView moduleKey={getViewModule(viewKey)} />;
    }
    return <>{node}</>;
  }


  return (
    <main className="phase29-shell min-h-screen bg-slate-950 text-white p-6">
<aside className={`phase29-sidebar ${mobileNavOpen ? "phase29-sidebar-open" : ""}`}>
  <div className="phase29-brand">
    <div className="phase29-brand-mark">⌁</div>
    <div>
      <div className="phase29-brand-title">{brandName}</div>
      <div className="phase29-brand-subtitle">Manufacturing Execution System</div>
    </div>
  </div>

  <nav className="phase29-nav">
    {MODULE_CATALOG.map((mod) => {
      const items = NAV_ITEMS.filter(
        (n) => n.module === mod.key && canRoleSeeView(n.key, role, isFounder)
      );
      if (items.length === 0) return null;   // hide groups with nothing for this role
      const unlocked = enabledModules.includes(mod.key);
      return (
        <div key={mod.key} className="phase29-nav-group">
          <div className="phase29-nav-group-title flex items-center gap-1">
            <span>{mod.label}</span>
            {!unlocked && <span className="text-slate-600 text-xs">🔒</span>}
          </div>
          {items.map((item) => (
            <button
              key={item.key}
              onClick={() => {
                setActiveView(item.key);
                setMobileNavOpen(false);
              }}
              className={`phase29-nav-item ${
                activeView === item.key ? "phase29-nav-item-active" : ""
              } ${!unlocked ? "opacity-40 cursor-pointer" : ""}`}
            >
              <span className="phase29-nav-icon">{item.icon}</span>
              <span>{item.label}</span>
              {!unlocked && <span className="ml-auto text-slate-600 text-xs">🔒</span>}
            </button>
          ))}
        </div>
      );
    })}
  </nav>
</aside>

<header className="phase29-topbar">
  <div className="flex items-center gap-3 min-w-0">
    <button
      type="button"
      className="phase29-menu-btn"
      onClick={() => setMobileNavOpen((o) => !o)}
      aria-label={mobileNavOpen ? "Close menu" : "Open menu"}
    >
      {mobileNavOpen ? "✕" : "☰"}
    </button>
    <div className="min-w-0">
      <p className="phase29-eyebrow">Welcome back, {userName || "there"}</p>
      <h1>{activeLabel}</h1>
    </div>
  </div>
  <div className="phase29-topbar-actions">
    <div className="relative" style={{width:280,minHeight:38,display:"flex",alignItems:"center",border:"1px solid rgba(255,255,255,0.1)",borderRadius:12,background:"rgba(15,23,42,0.6)"}}>
      <input
        style={{background:"transparent",border:"none",outline:"none",width:"100%",padding:"0 14px",fontSize:"0.84rem",color:"#94a3b8",cursor:"text",pointerEvents:"auto",userSelect:"text",WebkitUserSelect:"text"}}
        placeholder="Search modules / records"
        value={searchQuery}
        onChange={(e) => {
          const q = e.target.value;
          setSearchQuery(q);
          setSearchResults(
            q.trim().length > 0
              ? NAV_ITEMS.filter(
                  (n) =>
                    n.label.toLowerCase().includes(q.toLowerCase()) &&
                    canRoleSeeView(n.key, role, isFounder)
                )
              : []
          );
          queryEntities(q);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (searchResults.length > 0 || entityResults.length > 0)) {
            setActiveView(searchResults.length > 0 ? searchResults[0].key : entityResults[0].view);
            setSearchQuery("");
            setSearchResults([]);
            setEntityResults([]);
          }
          if (e.key === "Escape") {
            setSearchQuery("");
            setSearchResults([]);
            setEntityResults([]);
          }
        }}
      />
      {(searchResults.length > 0 || entityResults.length > 0) && (
        <div className="absolute top-full left-0 mt-1 w-80 bg-slate-900 border border-slate-700 rounded-xl shadow-xl z-50 overflow-hidden">
          {searchResults.map((r) => (
            <button
              key={r.key}
              className="w-full text-left px-4 py-2.5 text-sm hover:bg-slate-800 flex items-center gap-2"
              onClick={() => {
                setActiveView(r.key);
                setSearchQuery("");
                setSearchResults([]);
                setEntityResults([]);
              }}
            >
              <span>{r.icon}</span>
              <span>{r.label}</span>
            </button>
          ))}
          {entityResults.length > 0 && (
            <>
              {searchResults.length > 0 && <div className="border-t border-slate-800" />}
              {entityResults.map((h) => (
                <button
                  key={`${h.type}-${h.id}`}
                  className="w-full text-left px-4 py-2.5 text-sm hover:bg-slate-800 flex items-center gap-2.5"
                  onClick={() => {
                    setActiveView(h.view);
                    setSearchQuery("");
                    setSearchResults([]);
                    setEntityResults([]);
                    window.scrollTo({ top: 0, behavior: "smooth" });
                  }}
                >
                  <span className="shrink-0 rounded bg-slate-800 border border-slate-700 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-400">
                    {h.type}
                  </span>
                  <span className="min-w-0 flex-1 truncate">
                    {h.label} <span className="text-slate-500">· {h.sublabel}</span>
                  </span>
                </button>
              ))}
            </>
          )}
        </div>
      )}
    </div>
    {isFounder ? (
      <select
        className="phase29-pill"
        style={{ cursor: "pointer", appearance: "auto", color: company === "GMATS" ? "#a5b4fc" : undefined }}
        value={company}
        onChange={(e) => switchCompany(e.target.value)}
        title="Switch company / tenant"
      >
        <option value="DEFAULT">Default Factory</option>
        <option value="GMATS">GMATS Compressors</option>
        {tenants
          .filter((t) => t.company_code && t.company_code !== "DEFAULT" && t.company_code !== "GMATS")
          .map((t) => (
            <option key={t.company_code} value={t.company_code}>
              {t.company_name || t.company_code}
            </option>
          ))}
      </select>
    ) : (
      <div className="phase29-pill" style={{ color: "#a5b4fc" }}>
        {company === "GMATS" ? "GMATS Compressors" : company}
      </div>
    )}
    <div className="phase29-user">{userName || "User"} · {role || "—"}</div>
    <div style={{ position: "relative" }}>
      <button
        onClick={() => { setPwOpen(!pwOpen); setPwMsg(""); }}
        className="phase29-pill"
        style={{ cursor: "pointer" }}
        title="Change password"
      >
        Password
      </button>
      {pwOpen && (
        <form
          onSubmit={changeOwnPassword}
          style={{ position: "absolute", right: 0, top: "110%", zIndex: 60, background: "#0f172a", border: "1px solid #334155", borderRadius: 12, padding: 12, display: "flex", flexDirection: "column", gap: 8, width: 230 }}
        >
          <input type="password" placeholder="Current password" value={pwCurrent} onChange={(e) => setPwCurrent(e.target.value)} required style={{ background: "#020617", border: "1px solid #334155", borderRadius: 8, padding: "6px 10px", color: "#e2e8f0" }} />
          <input type="password" placeholder="New password (min 8)" value={pwNew} onChange={(e) => setPwNew(e.target.value)} required minLength={8} style={{ background: "#020617", border: "1px solid #334155", borderRadius: 8, padding: "6px 10px", color: "#e2e8f0" }} />
          <button type="submit" style={{ background: "#e2e8f0", color: "#0f172a", borderRadius: 8, padding: "6px 10px", fontWeight: 600, cursor: "pointer" }}>Change password</button>
          {pwMsg && <span style={{ fontSize: 12, color: pwMsg === "Password changed" ? "#6ee7b7" : "#fca5a5" }}>{pwMsg}</span>}
        </form>
      )}
    </div>
    <button
      onClick={logout}
      className="phase29-pill"
      style={{ cursor: "pointer", color: "#fca5a5", borderColor: "rgba(239,68,68,0.4)" }}
      title="Logout"
    >
      Logout
    </button>
  </div>
</header>

      {activeView === "overview" && (
        <>
      <section className="mb-8">
        <p className="text-sm text-slate-400">MES Lite SaaS MVP</p>
        <h1 className="text-4xl font-bold mt-2">AMP Dashboard</h1>
        <p className="text-slate-400 mt-2">
          Real-time machine downtime visibility for SME factories.
        </p>
      </section>

      <div className="mb-8">
        <ScorecardStrip
          onOpen={(view) => {
            setActiveView(view);
            window.scrollTo({ top: 0, behavior: "smooth" });
          }}
        />
      </div>

      <div className="mb-8">
        <BriefingSnapshot
          onOpen={(view) => {
            setActiveView(view);
            window.scrollTo({ top: 0, behavior: "smooth" });
          }}
          onOpenEscalation={(id) => {
            setFocusedEscalationId(id);
            setActiveView("escalations");
          }}
        />
      </div>

      <div className="mb-8">
        <FactoryPulse />
      </div>

      {/* Overview card groups — only the active tab's cards mount (lighter page,
          lighter polling). Scorecard, briefing and pulse above stay always-on. */}
      <div className="mb-4 flex flex-wrap items-center gap-1 rounded-xl border border-slate-800 bg-slate-900/60 p-1 w-fit">
        {[
          { key: "performance", label: "Performance" },
          { key: "qualitymaint", label: "Quality & Maintenance" },
          { key: "business", label: "Business" },
          { key: "reports", label: "Reports" },
        ].map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setOverviewTab(t.key)}
            className={`rounded-lg px-3 py-1.5 text-sm transition ${overviewTab === t.key ? "bg-slate-700 text-white" : "text-slate-400 hover:text-slate-200"}`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {overviewTab === "performance" && (
        <>
          <div className="mb-8"><OeeSnapshot /></div>
          <div className="mb-8"><LossesSnapshot /></div>
          <div className="mb-8"><ProductionSnapshot /></div>
          <div className="mb-8"><FlowSnapshot /></div>
          <div className="mb-8"><ShiftSnapshot /></div>
        </>
      )}

      {overviewTab === "qualitymaint" && (
        <>
          <div className="mb-8"><QualitySnapshot /></div>
          <div className="mb-8"><DowntimeSnapshot /></div>
          <div className="mb-8">
            <MaintenanceSnapshot
              onOpen={(view) => {
                setActiveView(view);
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            />
          </div>
          <div className="mb-8">
            <ComplianceSnapshot
              onOpen={(view) => {
                setActiveView(view);
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            />
          </div>
        </>
      )}

      {overviewTab === "business" && (
        <>
          <div className="mb-8">
            <DeliverySnapshot
              onOpen={(view) => {
                setActiveView(view);
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            />
          </div>
          <div className="mb-8">
            <CostSnapshot
              onOpen={(view) => {
                setActiveView(view);
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            />
          </div>
          <div className="mb-8"><InventorySnapshot /></div>
        </>
      )}

      {overviewTab === "reports" && (
        <>
          <div className="mb-8"><HandoverSnapshot /></div>
          <div className="mb-8"><WeeklyReportSnapshot /></div>
        </>
      )}

      <section className="mb-6 rounded-2xl bg-slate-900 border border-slate-800 p-4 flex flex-col md:flex-row md:items-center md:justify-between gap-3">
        <div>
          <p className="text-sm text-slate-400">Live WebSocket Status</p>
          <p className="text-white font-semibold">{lastLiveEvent}</p>
        </div>

        <span
          className={`px-3 py-1 rounded-full text-sm border w-fit ${
            liveStatus === "connected"
              ? "border-green-500/40 text-green-400 bg-green-500/10"
              : liveStatus === "error"
              ? "border-red-500/40 text-red-400 bg-red-500/10"
              : "border-yellow-500/40 text-yellow-300 bg-yellow-500/10"
          }`}
        >
          {liveStatus}
        </span>
      </section>

      <section className="grid grid-cols-1 md:grid-cols-3 xl:grid-cols-7 gap-4 mb-8">
        <KpiCard title="Running Machines" value={running} />
        <KpiCard title="Breakdowns" value={breakdown} />
        <KpiCard title="Avg Utilization" value={`${avgUtilization}%`} />
        <KpiCard title="Downtime Events" value={downtimeLogs.length} />
        <KpiCard title="Total Downtime" value={`${totalDowntimeMinutes}m`} />
        <KpiCard title="Avg Shift Eff." value={`${avgShiftEfficiency}%`} />
        <KpiCard title="Top Reason" value={topReason} small />
      </section>
        </>
      )}

      {(activeView === "overview" || activeView === "machines") && (
        <>
          <form
            onSubmit={addMachine}
            className="mb-8 rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-5 gap-4"
          >
            <input
              className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
              placeholder="Machine name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />

            <select
              className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              <option>Running</option>
              <option>Idle</option>
              <option>Breakdown</option>
              <option>Maintenance</option>
            </select>

            <input
              className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
              type="number"
              placeholder="Utilization"
              value={utilization}
              onChange={(e) => setUtilization(Number(e.target.value))}
              min={0}
              max={100}
              required
            />

            <input
              className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
              placeholder="Downtime"
              value={downtime}
              onChange={(e) => setDowntime(e.target.value)}
              required
            />

            <button
              type="submit"
              className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
            >
              Add Machine
            </button>
          </form>

          <section className="mb-10">
            <h2 className="text-2xl font-semibold mb-4">Machine Status</h2>

            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
              {machines.map((machine) => (
                <div
                  key={machine.id}
                  className="rounded-2xl bg-slate-900 border border-slate-800 p-5"
                >
                  <div className="flex items-center justify-between">
                    <h3 className="text-xl font-semibold">{machine.name}</h3>
                    <span
                      className={`text-xs px-3 py-1 rounded-full border ${getStatusStyle(
                        machine.status
                      )}`}
                    >
                      {machine.status}
                    </span>
                  </div>

                  <div className="mt-6">
                    <p className="text-sm text-slate-400">Utilization</p>
                    <div className="w-full bg-slate-800 rounded-full h-3 mt-2">
                      <div
                        className="bg-white h-3 rounded-full"
                        style={{ width: `${machine.utilization}%` }}
                      />
                    </div>

                    <p className="text-sm mt-2">{machine.utilization}%</p>

                    <p className="text-sm text-slate-400 mt-3">Estimated OEE</p>
                    <p className="text-lg font-semibold mt-1">
                      {calculateOEE(machine.utilization)}%
                    </p>
                  </div>

                  <div className="mt-5">
                    <p className="text-sm text-slate-400">Downtime Today</p>
                    <p className="text-lg font-semibold mt-1">
                      {machine.downtime}
                    </p>
                  </div>

                  <select
                    className="mt-5 w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-2 text-sm"
                    value={machine.status}
                    onChange={(e) =>
                      updateMachineStatus(machine.id, e.target.value)
                    }
                  >
                    <option>Running</option>
                    <option>Idle</option>
                    <option>Breakdown</option>
                    <option>Maintenance</option>
                  </select>

                  <button
                    onClick={() => deleteMachine(machine.id)}
                    className="mt-4 w-full rounded-xl border border-red-500/40 text-red-400 py-2 text-sm hover:bg-red-500/10"
                  >
                    Delete Machine
                  </button>
                </div>
              ))}
            </div>
          </section>
        </>
      )}

      {(activeView === "overview" || activeView === "downtime") && (
        <section className="grid grid-cols-1 xl:grid-cols-3 gap-6">
          <form
            onSubmit={addDowntimeLog}
            className="rounded-2xl bg-slate-900 border border-slate-800 p-5"
          >
            <h2 className="text-2xl font-semibold mb-4">Log Downtime</h2>

            <div className="space-y-4">
              <select
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
                value={selectedMachineId}
                onChange={(e) => setSelectedMachineId(e.target.value)}
                required
              >
                <option value="">Select machine</option>
                {machines.map((machine) => (
                  <option key={machine.id} value={machine.id}>
                    {machine.name}
                  </option>
                ))}
              </select>

              <select
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              >
                <option>Material Shortage</option>
                <option>Operator Unavailable</option>
                <option>Breakdown</option>
                <option>Maintenance</option>
                <option>Quality Issue</option>
                <option>Tool Change</option>
              </select>

              <input
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
                placeholder="Duration e.g. 25 min"
                value={duration}
                onChange={(e) => setDuration(e.target.value)}
                required
              />

              <textarea
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 min-h-28"
                placeholder="Operator notes"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />

              <button
                type="submit"
                className="w-full rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
              >
                Save Downtime Log
              </button>
            </div>
          </form>

          <div className="xl:col-span-2 rounded-2xl bg-slate-900 border border-slate-800 p-5">
            <h2 className="text-2xl font-semibold mb-4">
              Recent Downtime Events
            </h2>

            <div className="max-h-[620px] overflow-y-auto overflow-x-auto rounded-xl border border-slate-800">
              <table className="w-full min-w-[720px] text-left text-sm">
                <thead className="sticky top-0 bg-slate-900 text-slate-400 border-b border-slate-800">
                  <tr>
                    <th className="py-3 px-4">Machine</th>
                    <th className="py-3 px-4">Reason</th>
                    <th className="py-3 px-4">Duration</th>
                    <th className="py-3 px-4">Notes</th>
                  </tr>
                </thead>

                <tbody>
                  {downtimeLogs.slice(0, 50).map((log) => (
                    <tr key={log.id} className="border-b border-slate-800">
                      <td className="py-3 px-4 font-medium">
                        {getMachineName(log.machine_id)}
                      </td>
                      <td className="py-3 px-4">{log.reason}</td>
                      <td className="py-3 px-4">{log.duration}</td>
                      <td className="py-3 px-4 text-slate-400">
                        {log.notes || "-"}
                      </td>
                    </tr>
                  ))}

                  {downtimeLogs.length === 0 && (
                    <tr>
                      <td colSpan={4} className="py-6 px-4 text-slate-400">
                        No downtime logs yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      {(activeView === "overview" || activeView === "shifts") && (
        <section className="mt-8 grid grid-cols-1 xl:grid-cols-3 gap-6">
          <form
            onSubmit={addShift}
            className="rounded-2xl bg-slate-900 border border-slate-800 p-5"
          >
            <h2 className="text-2xl font-semibold mb-4">
              Shift Performance Entry
            </h2>

            <div className="space-y-4">
              <input
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
                placeholder="Shift Name"
                value={shiftName}
                onChange={(e) => setShiftName(e.target.value)}
                required
              />

              <input
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
                type="number"
                placeholder="Target Output"
                value={targetOutput}
                onChange={(e) => setTargetOutput(Number(e.target.value))}
                required
              />

              <input
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3"
                type="number"
                placeholder="Actual Output"
                value={actualOutput}
                onChange={(e) => setActualOutput(Number(e.target.value))}
                required
              />

              <button
                type="submit"
                className="w-full rounded-xl bg-white text-slate-950 font-semibold px-4 py-3"
              >
                Save Shift Data
              </button>
            </div>
          </form>

          <div className="xl:col-span-2 rounded-2xl bg-slate-900 border border-slate-800 p-5">
            <h2 className="text-2xl font-semibold mb-4">Shift Performance</h2>

            <div className="overflow-x-auto rounded-xl border border-slate-800">
              <table className="w-full min-w-[620px] text-left text-sm">
                <thead className="text-slate-400 border-b border-slate-800">
                  <tr>
                    <th className="py-3 px-4">Shift</th>
                    <th className="py-3 px-4">Target</th>
                    <th className="py-3 px-4">Actual</th>
                    <th className="py-3 px-4">Efficiency</th>
                  </tr>
                </thead>

                <tbody>
                  {shifts.map((shift) => {
                    const efficiency =
                      shift.target_output > 0
                        ? Math.round(
                            (shift.actual_output / shift.target_output) * 100
                          )
                        : 0;

                    return (
                      <tr key={shift.id} className="border-b border-slate-800">
                        <td className="py-3 px-4 font-medium">
                          {shift.shift_name}
                        </td>
                        <td className="py-3 px-4">{shift.target_output}</td>
                        <td className="py-3 px-4">{shift.actual_output}</td>
                        <td className="py-3 px-4">{efficiency}%</td>
                      </tr>
                    );
                  })}

                  {shifts.length === 0 && (
                    <tr>
                      <td colSpan={4} className="py-6 px-4 text-slate-400">
                        No shift data yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      {(activeView === "overview" || activeView === "analytics") && (
        <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-5 min-w-0">
          <h2 className="text-2xl font-semibold mb-4">
            Downtime Reason Analysis
          </h2>

          <div className="h-80 w-full min-w-0 overflow-hidden">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={downtimeReasonChartData}>
                <XAxis dataKey="reason" stroke="#94a3b8" />
                <YAxis stroke="#94a3b8" />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "#020617",
                    border: "1px solid #334155",
                    color: "#ffffff",
                  }}
                />
                <Bar dataKey="count" fill="#ffffff" radius={[8, 8, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
      )}

      {activeView === "timeline" && (
        <>
          <MachineStateSummary data={machineStateSummary} />
          <MachineTimeline events={machineEvents} />
        </>
      )}

      {renderSection("mission", (
        <MissionControlSection />
      ))}

      {renderSection("agentactivity", (
        <AgentActivitySection />
      ))}
      {renderSection("roi", (
        <AgentRoiSection />
      ))}

      {renderSection("machinehealth", (
        <MachineHealthSection />
      ))}

      {renderSection("workorders", (
        <>
          <WorkOrdersSection
            machines={machines}
            workOrders={workOrders}
            analytics={workOrderAnalytics}
            form={workOrderForm}
            setForm={setWorkOrderForm}
            createWorkOrder={isAdminOrSupervisor ? createWorkOrder : async () => {}}
            updateWorkOrder={updateWorkOrder}
            deleteWorkOrder={isAdmin ? deleteWorkOrder : undefined}
            getMachineName={getMachineName}
          />
          {isAdmin && <BomViewer />}
        </>
      ))}

      {renderSection("planning", (
        <ProductionPlanSection
          machines={machines}
          workOrders={workOrders}
          plans={productionPlans}
          analytics={productionPlanAnalytics}
          form={productionPlanForm}
          setForm={setProductionPlanForm}
          createPlan={isAdminOrSupervisor ? createProductionPlan : async () => {}}
          updatePlan={updateProductionPlan}
          deletePlan={isAdmin ? deleteProductionPlan : undefined}
          getMachineName={getMachineName}
        />
      ))}

      {renderSection("maintenance_ai", (
        <PredictiveMaintenanceSection risks={predictiveRisks} />
      ))}

      {renderSection("escalations", (
        <EscalationSection
          machines={machines}
          escalations={escalations}
          analytics={escalationAnalytics}
          focusedId={focusedEscalationId}
          form={escalationForm}
          setForm={setEscalationForm}
          createEscalation={createEscalation}
          updateEscalation={updateEscalation}
          deleteEscalation={isAdmin ? deleteEscalation : undefined}
          generateFromSmartAlerts={isAdminOrSupervisor ? generateEscalationsFromSmartAlerts : async () => {}}
          getMachineName={getMachineName}
        />
      ))}

      {renderSection("inventory", (
        company === "GMATS" ? (
          <GmatsInventory tenant="GMATS" isAdmin={isAdmin} />
        ) : (
        <>
          <EnterpriseInventory items={inventoryItems} />
          <InventorySection
            items={inventoryItems}
            transactions={inventoryTransactions}
            analytics={inventoryAnalytics}
            itemForm={inventoryItemForm}
            setItemForm={setInventoryItemForm}
            transactionForm={inventoryTransactionForm}
            setTransactionForm={setInventoryTransactionForm}
            createItem={isAdminOrSupervisor ? createInventoryItem : async () => {}}
            updateItem={isAdminOrSupervisor ? updateInventoryItem : async () => {}}
            deleteItem={isAdmin ? deleteInventoryItem : undefined}
            createTransaction={createInventoryTransaction}
            generateLowStockEscalations={isAdminOrSupervisor ? generateLowStockEscalations : async () => {}}
          />
        </>
        )
      ))}

      {renderSection("users", (
        isAdmin ? (
          <UsersSection
            users={users}
            company={company}
            addEmployee={addEmployee}
            updateUserRole={updateUserRole}
            deleteUser={deleteUserAccount}
            resetPassword={resetUserPassword}
          />
        ) : (
          <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-8 text-center">
            <h3 className="text-2xl font-semibold mb-2">User Management</h3>
            <p className="text-slate-400">Only an Admin can add or manage employees.</p>
          </section>
        )
      ))}

      {renderSection("copilot", (
        <AICopilot
          onOpen={(view) => {
            setActiveView(view);
            window.scrollTo({ top: 0, behavior: "smooth" });
          }}
        />
      ))}

      {renderSection("connectivity", (
        <IndustrialConnectivity />
      ))}

      {renderSection("quality", (
        <QualitySection
          machines={machines}
          workOrders={workOrders}
          productionPlans={productionPlans}
          inspections={qualityInspections}
          analytics={qualityAnalytics}
          form={qualityForm}
          setForm={setQualityForm}
          createInspection={createQualityInspection}
          updateInspection={updateQualityInspection}
          deleteInspection={isAdmin ? deleteQualityInspection : undefined}
          generateDefectEscalations={isAdminOrSupervisor ? generateDefectEscalations : async () => {}}
          getMachineName={getMachineName}
        />
      ))}

      {renderSection("executive", (
        <ExecutiveOeeSection data={executiveOee} />
      ))}

      {renderSection("digitaltwin", (
        <DigitalTwinSection
          machines={machines}
          nodes={factoryNodes}
          commandCenter={factoryCommandCenter}
          form={factoryNodeForm}
          setForm={setFactoryNodeForm}
          createNode={isAdminOrSupervisor ? createFactoryNode : async () => {}}
          updateNode={isAdminOrSupervisor ? updateFactoryNode : async () => {}}
          deleteNode={isAdmin ? deleteFactoryNode : undefined}
          autoGenerateLayout={isAdminOrSupervisor ? autoGenerateFactoryLayout : async () => {}}
        />
      ))}

      {renderSection("orders", (
        <OrdersDispatchSection
          workOrders={workOrders}
          productionPlans={productionPlans}
          orders={customerOrders}
          analytics={customerOrderAnalytics}
          form={customerOrderForm}
          setForm={setCustomerOrderForm}
          createOrder={isAdminOrSupervisor ? createCustomerOrder : async () => {}}
          updateOrder={updateCustomerOrder}
          deleteOrder={isAdmin ? deleteCustomerOrder : undefined}
          generateLateOrderEscalations={isAdminOrSupervisor ? generateLateOrderEscalations : async () => {}}
        />
      ))}

      {renderSection("purchasing", (
        <PurchasingSection
          suppliers={suppliers}
          purchaseOrders={purchaseOrders}
          inventoryItems={inventoryItems}
          analytics={purchasingAnalytics}
          supplierForm={supplierForm}
          setSupplierForm={setSupplierForm}
          poForm={poForm}
          setPoForm={setPoForm}
          createSupplier={isAdminOrSupervisor ? createSupplier : async () => {}}
          updateSupplier={isAdminOrSupervisor ? updateSupplier : async () => {}}
          deleteSupplier={isAdmin ? deleteSupplier : undefined}
          createPurchaseOrder={isAdminOrSupervisor ? createPurchaseOrder : async () => {}}
          updatePurchaseOrder={updatePurchaseOrder}
          deletePurchaseOrder={isAdmin ? deletePurchaseOrder : undefined}
          generateOverdueEscalations={isAdminOrSupervisor ? generateOverduePoEscalations : async () => {}}
        />
      ))}

      {renderSection("documents", (
        <DocumentsSection documents={documents} analytics={documentAnalytics} form={documentForm} setForm={setDocumentForm} createDocument={isAdminOrSupervisor ? createDocument : async () => {}} updateDocument={isAdminOrSupervisor ? updateDocument : async () => {}} deleteDocument={isAdmin ? deleteDocument : undefined} generateReviewEscalations={isAdminOrSupervisor ? generateDocumentReviewEscalations : async () => {}} />
      ))}

      {renderSection("cmms", (
        <MaintenanceSection machines={machines} tasks={maintenanceTasks} analytics={maintenanceAnalytics} form={maintenanceForm} setForm={setMaintenanceForm} createTask={createMaintenanceTask} updateTask={updateMaintenanceTask} deleteTask={isAdmin ? deleteMaintenanceTask : undefined} generateOverdueEscalations={isAdminOrSupervisor ? generateMaintenanceOverdueEscalations : async () => {}} getMachineName={getMachineName} />
      ))}

      {renderSection("scheduling", (
        <SchedulingSection machines={machines} workOrders={workOrders} productionPlans={productionPlans} schedules={productionSchedules} analytics={scheduleAnalytics} form={scheduleForm} setForm={setScheduleForm} createSchedule={isAdminOrSupervisor ? createProductionSchedule : async () => {}} updateSchedule={updateProductionSchedule} deleteSchedule={isAdmin ? deleteProductionSchedule : undefined} getMachineName={getMachineName} />
      ))}

      {renderSection("iot", (
        <IoTCommandSection machines={machines} telemetry={iotTelemetry} command={iotCommand} form={iotForm} setForm={setIotForm} createTelemetry={isAdminOrSupervisor ? createIotTelemetry : async () => {}} />
      ))}

      {renderSection("ai", (
        <>
          <div className="mt-8">
            <PlatformStatusCard />
          </div>
          <AIInsightsSection recommendations={aiRecommendations} insights={aiInsights} generateRecommendations={isAdminOrSupervisor ? generateAiRecommendations : async () => {}} updateRecommendation={updateAiRecommendation} />
        </>
      ))}

      {renderSection("saas", (
        <SaaSAdminSection tenants={tenants} analytics={saasAnalytics} form={tenantForm} setForm={setTenantForm} createTenant={isAdmin ? createTenant : async () => {}} updateTenant={isAdmin ? updateTenant : async () => {}} deleteTenant={isAdmin ? deleteTenant : undefined} provisionAdmin={isAdmin ? provisionAdmin : undefined} adminCreds={adminCreds} clearAdminCreds={() => setAdminCreds(null)} />
      ))}

      {renderSection("costing", (
        <CostingSection costs={costRecords} analytics={costingAnalytics} form={costForm} setForm={setCostForm} createCost={isAdminOrSupervisor ? createCost : async () => {}} updateCost={isAdminOrSupervisor ? updateCost : async () => {}} deleteCost={isAdmin ? deleteCost : undefined} />
      ))}

      {renderSection("operator", (
        <OperatorTerminalSection machines={machines} workOrders={workOrders} productionPlans={productionPlans} executions={operatorExecutions} analytics={operatorAnalytics} form={operatorForm} setForm={setOperatorForm} createExecution={createOperatorExecution} updateExecution={updateOperatorExecution} deleteExecution={isAdmin ? deleteOperatorExecution : undefined} getMachineName={getMachineName} />
      ))}

      {renderSection("inbox", (
        <ApprovalsInbox />
      ))}

      {renderSection("trends", (
        <TrendsSection />
      ))}

      {renderSection("notifications", (
        <NotificationsSection notifications={notifications} generateNotifications={generateSystemNotifications} updateNotification={updateNotification} />
      ))}

      {renderSection("enterprise", (
        <EnterprisePolishSection auditLogs={auditLogs} reports={reports} health={systemHealth} summary={finalSummary} reportForm={reportForm} setReportForm={setReportForm} createReport={createReport} />
      ))}
    </main>
  );
}

function KpiCard({
  title,
  value,
  small,
}: {
  title: string;
  value: string | number;
  small?: boolean;
}) {
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <p className="text-slate-400 text-sm">{title}</p>
      <h2 className={`${small ? "text-xl" : "text-3xl"} font-bold mt-2`}>
        {value}
      </h2>
    </div>
  );
}
