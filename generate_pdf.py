from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT

OUTPUT = "AMP_Overview.pdf"

doc = SimpleDocTemplate(
    OUTPUT,
    pagesize=A4,
    rightMargin=2*cm, leftMargin=2*cm,
    topMargin=2*cm, bottomMargin=2*cm,
)

W, H = A4
styles = getSampleStyleSheet()

# ── Custom styles ──────────────────────────────────────────────
DARK   = colors.HexColor("#0f172a")
ACCENT = colors.HexColor("#6366f1")
LIGHT  = colors.HexColor("#e2e8f0")
GREEN  = colors.HexColor("#22c55e")
YELLOW = colors.HexColor("#eab308")
RED    = colors.HexColor("#ef4444")
MUTED  = colors.HexColor("#64748b")

cover_title = ParagraphStyle("cover_title", fontSize=32, leading=40,
    textColor=ACCENT, alignment=TA_CENTER, fontName="Helvetica-Bold")
cover_sub   = ParagraphStyle("cover_sub",   fontSize=14, leading=20,
    textColor=LIGHT, alignment=TA_CENTER, fontName="Helvetica")
cover_tag   = ParagraphStyle("cover_tag",   fontSize=11, leading=16,
    textColor=MUTED, alignment=TA_CENTER, fontName="Helvetica-Oblique")

h1 = ParagraphStyle("h1", fontSize=20, leading=26, textColor=ACCENT,
    fontName="Helvetica-Bold", spaceAfter=6)
h2 = ParagraphStyle("h2", fontSize=14, leading=20, textColor=LIGHT,
    fontName="Helvetica-Bold", spaceAfter=4, spaceBefore=10)
h3 = ParagraphStyle("h3", fontSize=11, leading=16, textColor=ACCENT,
    fontName="Helvetica-Bold", spaceAfter=3, spaceBefore=6)
body = ParagraphStyle("body", fontSize=10, leading=15, textColor=LIGHT,
    fontName="Helvetica", spaceAfter=4)
bullet = ParagraphStyle("bullet", fontSize=10, leading=15, textColor=LIGHT,
    fontName="Helvetica", leftIndent=16, spaceAfter=2,
    bulletIndent=6, bulletFontName="Helvetica")
small  = ParagraphStyle("small", fontSize=9, leading=13, textColor=MUTED,
    fontName="Helvetica-Oblique")

def hr():
    return HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=8, spaceBefore=8)

def sp(n=8):
    return Spacer(1, n)

def bul(text):
    return Paragraph(f"• {text}", bullet)

def module_table(rows):
    t = Table(rows, colWidths=[4.5*cm, 4*cm, 8.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), ACCENT),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1, 0), 9),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.HexColor("#1e293b"), colors.HexColor("#0f172a")]),
        ("TEXTCOLOR",   (0,1), (-1,-1), LIGHT),
        ("FONTNAME",    (0,1), (-1,-1), "Helvetica"),
        ("FONTSIZE",    (0,1), (-1,-1), 9),
        ("GRID",        (0,0), (-1,-1), 0.4, colors.HexColor("#334155")),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 7),
    ]))
    return t

def plan_table(rows):
    t = Table(rows, colWidths=[4*cm, 3.5*cm, 3.5*cm, 3.5*cm, 2.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), ACCENT),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,0), 9),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.HexColor("#1e293b"), colors.HexColor("#0f172a")]),
        ("TEXTCOLOR",   (0,1), (-1,-1), LIGHT),
        ("FONTNAME",    (0,1), (-1,-1), "Helvetica"),
        ("FONTSIZE",    (0,1), (-1,-1), 9),
        ("GRID",        (0,0), (-1,-1), 0.4, colors.HexColor("#334155")),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 7),
        ("ALIGN",       (1,1), (-1,-1), "CENTER"),
    ]))
    return t

# ══════════════════════════════════════════════════════════════════
story = []

# ── COVER PAGE ────────────────────────────────────────────────────
story += [
    sp(60),
    Paragraph("AMP", cover_title),
    sp(12),
    Paragraph("Enterprise Manufacturing Execution System", cover_sub),
    sp(20),
    hr(),
    sp(12),
    Paragraph("Complete Project Overview — Architecture, Modules, Deployment & Roadmap", cover_tag),
    sp(8),
    Paragraph("Confidential · Precision Parts Pvt Ltd Demo Edition · June 2026", cover_tag),
    PageBreak(),
]

# ── 1. WHAT IS AMP ────────────────────────────────────────────
story += [
    Paragraph("1. What is AMP?", h1), hr(),
    Paragraph(
        "AMP is a cloud-based Manufacturing Execution System (MES) built for small and "
        "medium-sized manufacturers. It replaces spreadsheets, WhatsApp groups, and paper-based "
        "shop-floor tracking with a single real-time platform that connects every department — "
        "from raw material receiving to final shipment.", body),
    sp(),
    Paragraph("The core promise:", h3),
    bul("Every work order, machine, operator, and inventory item in one place"),
    bul("Real-time OEE, downtime, and quality data from the shop floor"),
    bul("AI-driven insights that flag problems before they become costly"),
    bul("Modular — start with Core MES, add packs as you grow"),
    sp(16),
]

# ── 2. TECH STACK ─────────────────────────────────────────────────
story += [
    Paragraph("2. Technology Stack", h1), hr(),
    Paragraph("Backend", h2),
    bul("FastAPI (Python 3.11) — REST API with 80+ endpoints"),
    bul("SQLAlchemy ORM — database-agnostic, runs on PostgreSQL"),
    bul("Paho MQTT — receives real-time IoT/PLC telemetry from machines"),
    bul("WebSocket (/ws/live) — pushes live events to the frontend dashboard"),
    bul("python-jose — JWT authentication with role-based access control"),
    bul("python-dotenv — all secrets in environment variables, none in code"),
    sp(),
    Paragraph("Frontend", h2),
    bul("Next.js 16 + React 19 + TypeScript — server-side rendering + fast SPA"),
    bul("Tailwind CSS — dark-mode industrial UI, fully responsive"),
    bul("Recharts — OEE, production, and quality trend charts"),
    bul("WebSocket client — live machine status and IoT feed"),
    sp(),
    Paragraph("Infrastructure", h2),
    bul("Railway — backend + PostgreSQL hosted on Railway (US West)"),
    bul("Vercel — frontend CDN deployment, auto-deploys on GitHub push"),
    bul("GitHub (AshwinVar/AMP) — monorepo with backend/ and frontend/"),
    bul("MQTT Broker — configurable via env var (local Mosquitto or cloud)"),
    sp(16),
]

# ── 3. MODULE ARCHITECTURE ────────────────────────────────────────
story += [
    Paragraph("3. Module Architecture", h1), hr(),
    Paragraph(
        "AMP is split into 5 packs. Each pack is a group of related modules. "
        "Access is gated by the customer's plan — the plan is stored in localStorage "
        "and checked on every navigation item. A locked module shows a 'Contact Sales' "
        "screen instead of the data.", body),
    sp(8),
    module_table([
        ["Pack", "Plan Required", "Modules Included"],
        ["Core MES",        "Starter+",    "Overview, Machines, Downtime, Shifts, Analytics, Timeline"],
        ["Operations Pack", "Growth+",     "Work Orders, Production Plan, Scheduling, Operator Terminal, Orders & Dispatch"],
        ["Factory Pack",    "Growth+",     "Maintenance AI, CMMS, Quality, Inventory, Purchasing, Digital Twin"],
        ["Intelligence Pack","Enterprise", "IoT Command, AI Insights, Executive OEE"],
        ["Admin Pack",      "Enterprise",  "Users, Audit Log, Compliance, System Config"],
    ]),
    sp(16),
]

# ── 4. MODULE-BY-MODULE ───────────────────────────────────────────
story += [
    Paragraph("4. Module-by-Module Breakdown", h1), hr(),
]

modules = [
    ("Overview", "Core MES", "Live KPI dashboard — OEE, active WOs, machine status, shift performance, recent escalations. The first screen every manager sees on login."),
    ("Machines", "Core MES", "Machine master list with real-time status (Running / Idle / Breakdown). Shows utilization % and current downtime reason. Fed by MQTT IoT signals."),
    ("Downtime", "Core MES", "Log and categorise every machine stoppage. Reasons tracked: Breakdown, Maintenance, No Material, Setup, Power Cut. Feeds OEE calculation."),
    ("Shifts", "Core MES", "3-shift setup (A/B/C). Logs planned vs actual production per shift per machine. Shift efficiency % calculated automatically."),
    ("Analytics", "Core MES", "Charts for OEE trend, production output, quality rejection rate, downtime by reason. Data aggregated across all modules."),
    ("Timeline", "Core MES", "Activity feed — every event across the factory (WO started, breakdown raised, quality failed, PO received) in chronological order."),
    ("Work Orders", "Operations", "Create and manage production jobs. Each WO has part number, target qty, machine assignment, and status (Planned / In Progress / Completed). Completing a WO auto-updates inventory via BOM."),
    ("Production Plan", "Operations", "Links WOs to daily/weekly production targets. Tracks planned vs actual output per plan."),
    ("Scheduling", "Operations", "Assigns WOs to machines and time slots. Prevents double-booking. Shows Gantt-style machine calendar."),
    ("Operator Terminal", "Operations", "Simplified interface for shop-floor operators. Log job start/end, report issues, view their assigned WOs. Role-gated — operators can't see management data."),
    ("Orders & Dispatch", "Operations", "Customer sales orders with line items, delivery dates, and shipment status. Links to finished goods inventory for dispatch confirmation."),
    ("Maintenance AI", "Factory", "Predictive maintenance alerts based on machine run hours and downtime history. Flags machines due for PM before they break."),
    ("CMMS", "Factory", "Computerised Maintenance Management System. Log planned and unplanned maintenance jobs, track technician assignments, spare parts used."),
    ("Quality", "Factory", "First-article and in-process inspection records. Pass/fail per WO. Rejection reasons tracked. Links to customer orders for traceability."),
    ("Inventory", "Factory", "Raw material, tooling, consumables, packaging, finished goods, and spare parts. Real-time stock levels with reorder alerts. Auto-deducted by WO completion. Auto-restocked by PO receipt."),
    ("Purchasing", "Factory", "Raise purchase orders against inventory items. Track supplier, expected delivery, received quantity. Auto-posts stock-IN transaction on receipt."),
    ("Digital Twin", "Factory", "Visual floor-plan representation of machines with live status overlays. Click a machine to see its current WO, OEE, and recent IoT signals."),
    ("IoT Command", "Intelligence", "Send commands to machines via MQTT (start/stop/speed change). View raw IoT telemetry stream. Configure alert thresholds per machine."),
    ("AI Insights", "Intelligence", "LLM-powered analysis of production data. Surfaces patterns — which machine has the most recurring breakdowns, which shift underperforms, which part has highest rejection rate."),
    ("Executive OEE", "Intelligence", "Single-screen board-level view. OEE by plant, by shift, by product line. Designed for management presentations."),
    ("Users", "Admin", "User management — create accounts, assign roles (Admin / Supervisor / Operator / Viewer). Password reset and last-login tracking."),
    ("Audit Log", "Admin", "Immutable record of every data change — who changed what and when. Required for ISO 9001 and customer audits."),
    ("Compliance", "Admin", "Document repository for SOPs, quality plans, maintenance schedules, and compliance certificates. Version-controlled with review-due alerts."),
    ("System Config", "Admin", "Plant name, shift timings, OEE targets, MQTT broker settings, email alert configuration."),
]

for name, pack, desc in modules:
    story += [
        Paragraph(f"{name}  <font color='#6366f1' size='9'>({pack})</font>", h3),
        Paragraph(desc, body),
    ]

story.append(PageBreak())

# ── 5. DATA FLOW ──────────────────────────────────────────────────
story += [
    Paragraph("5. How Data Flows Through the System", h1), hr(),
    Paragraph("There are four main data flows that make the system feel live and connected:", body),
    sp(6),
    Paragraph("Flow 1 — IoT / Machine Telemetry", h2),
    bul("PLC or factory simulator publishes JSON to MQTT topic flowmes/machines"),
    bul("FastAPI MQTT service subscribes, parses the message, writes to machine_iot_signals table"),
    bul("WebSocket broadcasts the event to all connected browser clients"),
    bul("Dashboard updates machine status card in real time — no page refresh needed"),
    sp(),
    Paragraph("Flow 2 — Work Order Lifecycle", h2),
    bul("Supervisor creates WO with part number, target qty, and machine assignment"),
    bul("Operator picks up WO on the Operator Terminal, logs start time"),
    bul("As actual_quantity is updated, WO status auto-advances to Completed when target is hit"),
    bul("On Completed: BOM lookup deducts raw material stock, adds finished goods stock, posts 2 inventory transactions"),
    bul("Quality inspection is triggered — inspector logs pass/fail against the WO"),
    sp(),
    Paragraph("Flow 3 — Purchase Order to Stock", h2),
    bul("Stores raises a PO against an inventory item, specifying supplier and expected delivery date"),
    bul("When goods arrive, received_quantity is updated on the PO"),
    bul("System auto-posts a Receive transaction, increments current_stock on the item"),
    bul("If stock was below reorder level, the Low Stock escalation auto-resolves"),
    sp(),
    Paragraph("Flow 4 — Escalation Engine", h2),
    bul("Any module can raise an escalation: machine breakdown, low stock, overdue PO, quality failure"),
    bul("Escalations have severity (Low / Medium / High / Critical), owner, and department"),
    bul("Open escalations appear on the Overview dashboard and can be resolved from any module"),
    bul("The Generate Low Stock Escalations button scans all items below reorder level and creates one escalation per item (skips duplicates)"),
    sp(16),
]

# ── 6. INVENTORY IN DETAIL ────────────────────────────────────────
story += [
    Paragraph("6. Inventory Module — Deep Dive", h1), hr(),
    Paragraph("Categories tracked:", h2),
    bul("Raw Material — Steel rods, aluminium billets, copper strip, SS rod, rubber sheet"),
    bul("Tooling — Carbide inserts, end mills, drill bits, boring bars"),
    bul("Consumables — Cutting fluid, grinding wheels, sandpaper, safety gloves"),
    bul("Packaging — Corrugated boxes, bubble wrap, stretch film"),
    bul("Finished Goods — Precision shafts, laser cut plates, spur gears, bracket assemblies"),
    bul("Spare Parts — Bearings, V-belts, hydraulic seal kits, servo motor drives"),
    sp(),
    Paragraph("Stock health indicators:", h2),
    bul("Green (Healthy) — current stock above reorder level"),
    bul("Yellow (Low Stock) — current stock at or below reorder level"),
    bul("Red (Stockout) — current stock is zero"),
    sp(),
    Paragraph("Automatic stock movements:", h2),
    bul("WO Completed — BOM-driven Issue (raw material out) + Receive (finished goods in)"),
    bul("PO Received — Receive transaction auto-posted, stock incremented"),
    bul("Manual — operators can post Receive / Issue / Return / Adjustment via the Post Transaction form"),
    sp(16),
]

# ── 7. PLANS & PRICING ────────────────────────────────────────────
story += [
    Paragraph("7. Plans & Pricing Structure", h1), hr(),
    Paragraph(
        "AMP is sold as a SaaS subscription. The plan controls which module packs are "
        "unlocked. The current demo runs on the 'demo' plan which unlocks everything.", body),
    sp(8),
    plan_table([
        ["Plan",       "Core MES", "Operations", "Factory", "Intelligence + Admin"],
        ["Starter",    "Yes",      "No",          "No",      "No"],
        ["Growth",     "Yes",      "Yes",         "Yes",     "No"],
        ["Enterprise", "Yes",      "Yes",         "Yes",     "Yes"],
        ["Demo",       "Yes",      "Yes",         "Yes",     "Yes"],
    ]),
    sp(16),
]

# ── 8. DEPLOYMENT ─────────────────────────────────────────────────
story += [
    Paragraph("8. Deployment Architecture", h1), hr(),
    Paragraph("Current setup (as of June 2026):", h2),
    bul("Backend: Railway — Python 3.13, FastAPI, PostgreSQL addon, auto-deploy on push to master"),
    bul("Frontend: Vercel — Next.js, auto-deploy on push to master, CDN-served globally"),
    bul("Database: Railway PostgreSQL — DATABASE_URL injected automatically as env var"),
    bul("Domain: Namecheap domain → Vercel (A record @ → 76.76.21.21)"),
    bul("MQTT: Local Mosquitto broker for dev; configurable via MQTT_BROKER env var for production"),
    sp(),
    Paragraph("Environment variables (Railway service):", h2),
    bul("DATABASE_URL — auto-injected by Railway PostgreSQL addon"),
    bul("SECRET_KEY — JWT signing secret"),
    bul("ALLOWED_ORIGINS — comma-separated list of allowed frontend URLs (CORS)"),
    bul("MQTT_BROKER, MQTT_PORT, MQTT_TOPIC — IoT broker connection"),
    sp(),
    Paragraph("Environment variables (Vercel):", h2),
    bul("NEXT_PUBLIC_API_URL — Railway backend URL (e.g. https://flowmes-production.up.railway.app)"),
    sp(16),
]

# ── 9. FACTORY SIMULATOR ──────────────────────────────────────────
story += [
    Paragraph("9. Factory Simulator", h1), hr(),
    Paragraph(
        "backend/factory_simulator.py seeds all 18 modules with realistic interconnected data "
        "representing 'Precision Parts Pvt Ltd' — a CNC machining shop producing parts for "
        "Bharat Forge, Tata AutoComp, and Mahindra Gears.", body),
    sp(),
    Paragraph("What it seeds:", h2),
    bul("6 machines: CNC-01, CNC-02, CNC-03, Laser-Cutter-01, Packaging-01, Assembly-Robot-01"),
    bul("5 suppliers: Metallica Steels, Kennametal India, SKF India, Bosch Rexroth, Siemens India"),
    bul("25 inventory items across 6 categories with 42 transaction history records"),
    bul("15 work orders (Planned / In Progress / Completed) across 6 part types"),
    bul("Customer orders from 6 real Indian automotive manufacturers"),
    bul("3-shift schedule with planned vs actual production logs"),
    bul("Quality inspections linked to WOs with pass/fail results"),
    sp(),
    Paragraph("Live simulation loop (every 5 seconds):", h2),
    bul("Pushes IoT telemetry for random machines via MQTT"),
    bul("Logs shift production actuals"),
    bul("Consumes raw material inventory to simulate production"),
    bul("Raises escalations for breakdowns and low stock"),
    sp(16),
]

# ── 10. AUTHENTICATION ────────────────────────────────────────────
story += [
    Paragraph("10. Authentication & Roles", h1), hr(),
    Paragraph(
        "Every API endpoint is protected by JWT authentication. Roles control "
        "which operations each user can perform.", body),
    sp(6),
    Table(
        [
            ["Role",       "Can Do"],
            ["Admin",      "Full access — all read/write including user management, delete, config"],
            ["Supervisor", "Read/write on production, quality, maintenance, inventory. Cannot manage users."],
            ["Operator",   "Can update their own WOs and log downtime. Read-only on most modules."],
            ["Viewer",     "Read-only across all modules. For customer or auditor access."],
        ],
        colWidths=[4*cm, 13*cm]
    ).setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), ACCENT),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1),(-1,-1), [colors.HexColor("#1e293b"), colors.HexColor("#0f172a")]),
        ("TEXTCOLOR",   (0,1),(-1,-1), LIGHT),
        ("FONTNAME",    (0,1),(-1,-1), "Helvetica"),
        ("FONTSIZE",    (0,0),(-1,-1), 9),
        ("GRID",        (0,0),(-1,-1), 0.4, colors.HexColor("#334155")),
        ("TOPPADDING",  (0,0),(-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING", (0,0),(-1,-1), 7),
    ])),
    sp(16),
]

# ── 11. ROADMAP ───────────────────────────────────────────────────
story += [
    Paragraph("11. Roadmap", h1), hr(),
    Paragraph("Immediate (before first paid customer):", h2),
    bul("Finish Railway + Vercel production deployment"),
    bul("Connect Namecheap domain to Vercel frontend"),
    bul("Run factory_simulator.py against Railway PostgreSQL to seed demo data"),
    bul("Set ALLOWED_ORIGINS to Vercel URL in Railway env vars"),
    sp(),
    Paragraph("Short-term (next 30 days):", h2),
    bul("Multi-tenant support — each customer gets isolated data"),
    bul("Email alerts when escalations are raised (SendGrid integration)"),
    bul("PDF export for quality inspection reports and shift summaries"),
    bul("Mobile-responsive Operator Terminal for tablet use on shop floor"),
    sp(),
    Paragraph("Medium-term (next 90 days):", h2),
    bul("Barcode/QR scan support for inventory receiving and WO tracking"),
    bul("Full BOM management UI — configure part-to-material mappings per product"),
    bul("Customer portal — read-only view of their orders and quality certificates"),
    bul("Stripe billing integration for plan upgrades"),
    sp(16),
]

# ── 12. KEY FILES ─────────────────────────────────────────────────
story += [
    Paragraph("12. Key Files Reference", h1), hr(),
    Table(
        [
            ["File", "Purpose"],
            ["backend/main.py",             "All 80+ API endpoints (~3,300 lines)"],
            ["backend/models.py",           "SQLAlchemy ORM models — 25+ database tables"],
            ["backend/schemas.py",          "Pydantic request/response schemas"],
            ["backend/auth.py",             "JWT token creation and role verification"],
            ["backend/database.py",         "DB engine setup, reads DATABASE_URL from env"],
            ["backend/mqtt_service.py",     "MQTT subscriber — writes IoT signals to DB"],
            ["backend/factory_simulator.py","Seeds demo data + runs live simulation ticks"],
            ["backend/requirements.txt",    "Python dependencies with pinned versions"],
            ["backend/railway.toml",        "Railway build/deploy config (NIXPACKS builder)"],
            ["frontend/app/dashboard/page.tsx", "Main dashboard — all 26 module sections"],
            ["frontend/components/InventorySection.tsx", "Inventory module UI component"],
            ["frontend/lib/api.ts",         "Typed fetch wrappers (apiGet, apiPost, apiPatch)"],
            ["frontend/lib/modules.ts",     "Module catalog, nav items, plan-to-module mapping"],
            ["frontend/lib/live.ts",        "WebSocket client for real-time dashboard updates"],
        ],
        colWidths=[7*cm, 10*cm]
    ).setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), ACCENT),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1),(-1,-1), [colors.HexColor("#1e293b"), colors.HexColor("#0f172a")]),
        ("TEXTCOLOR",   (0,1),(-1,-1), LIGHT),
        ("FONTNAME",    (0,1),(-1,-1), "Helvetica"),
        ("FONTSIZE",    (0,0),(-1,-1), 8.5),
        ("GRID",        (0,0),(-1,-1), 0.4, colors.HexColor("#334155")),
        ("TOPPADDING",  (0,0),(-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ("LEFTPADDING", (0,0),(-1,-1), 6),
    ])),
    sp(20),
    hr(),
    Paragraph(
        "AMP · Confidential · ashwin.vardharajan@outlook.com · June 2026",
        ParagraphStyle("footer", fontSize=8, textColor=MUTED, alignment=TA_CENTER)
    ),
]

doc.build(story)
print(f"PDF created: {OUTPUT}")
