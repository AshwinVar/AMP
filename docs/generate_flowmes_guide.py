# Generates "FlowMES_Complete_Guide.pdf" — an end-to-end walkthrough of the
# whole product: architecture, where it starts, and every module's functionality
# plus where its data comes from in a real factory and how to connect it.
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak,
)

OUTPUT = "FlowMES_Complete_Guide.pdf"

DARK = colors.HexColor("#0f172a")
ACCENT = colors.HexColor("#4f46e5")
RED = colors.HexColor("#e11d2a")
MUTED = colors.HexColor("#475569")
LINE = colors.HexColor("#cbd5e1")
SOFT = colors.HexColor("#eef2ff")

styles = getSampleStyleSheet()
h1 = ParagraphStyle("h1", fontSize=18, leading=23, textColor=ACCENT, fontName="Helvetica-Bold", spaceBefore=10, spaceAfter=6)
h2 = ParagraphStyle("h2", fontSize=13, leading=17, textColor=DARK, fontName="Helvetica-Bold", spaceBefore=10, spaceAfter=3)
body = ParagraphStyle("body", fontSize=9.5, leading=14, textColor=DARK, fontName="Helvetica", spaceAfter=4)
small = ParagraphStyle("small", fontSize=8.5, leading=12, textColor=MUTED, fontName="Helvetica", spaceAfter=3)
cover_t = ParagraphStyle("ct", fontSize=30, leading=36, textColor=ACCENT, alignment=TA_CENTER, fontName="Helvetica-Bold")
cover_s = ParagraphStyle("cs", fontSize=13, leading=18, textColor=DARK, alignment=TA_CENTER, fontName="Helvetica")
cover_m = ParagraphStyle("cm", fontSize=10, leading=15, textColor=MUTED, alignment=TA_CENTER, fontName="Helvetica-Oblique")

story = []


def hr():
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=0.7, color=LINE))
    story.append(Spacer(1, 6))


def module(title, what, source, connect):
    story.append(Paragraph(title, h2))
    story.append(Paragraph("<b>What it does.</b> " + what, body))
    story.append(Paragraph("<b>Where the data comes from (real factory).</b> " + source, body))
    story.append(Paragraph("<b>How to connect.</b> " + connect, body))
    story.append(Spacer(1, 5))


# ── Cover ─────────────────────────────────────────────────────────
story.append(Spacer(1, 5 * cm))
story.append(Paragraph("FlowMES", cover_t))
story.append(Spacer(1, 0.3 * cm))
story.append(Paragraph("Manufacturing Execution System — Complete Walkthrough", cover_s))
story.append(Spacer(1, 0.2 * cm))
story.append(Paragraph("Architecture, every module, where each module's data comes from in a real factory, and how to connect it.", cover_m))
story.append(Spacer(1, 6 * cm))
story.append(Paragraph("Live: flow-mes.vercel.app &nbsp;|&nbsp; API: flowmes-production.up.railway.app", cover_m))
story.append(PageBreak())

# ── 1. What FlowMES is + where it starts ──────────────────────────
story.append(Paragraph("1. What FlowMES is, and where it starts", h1))
hr()
story.append(Paragraph(
    "FlowMES is a Manufacturing Execution System (MES) for small and mid-sized manufacturers. It puts the whole factory "
    "— machines, materials, production, quality, maintenance and live machine data — into one web dashboard the team can "
    "use from any device, replacing spreadsheets, WhatsApp updates and guesswork.", body))
story.append(Paragraph("The stack", h2))
story.append(Paragraph(
    "<b>Frontend</b> — a Next.js web app (hosted on Vercel) that runs in the browser. "
    "<b>Backend</b> — a FastAPI service (hosted on Railway) that holds all the logic and talks to the database. "
    "<b>Database</b> — PostgreSQL, the single source of truth. "
    "<b>Live data</b> — MQTT for machine telemetry and a WebSocket for pushing live updates to the dashboard.", body))
story.append(Paragraph("Where a session starts (the request flow)", h2))
story.append(Paragraph(
    "1. You open <b>flow-mes.vercel.app</b> and sign in. "
    "2. The backend checks your password (bcrypt) and returns a signed token (JWT) that carries your <b>role</b> "
    "(Admin / Supervisor / Operator) and your <b>company</b>. "
    "3. The dashboard loads and asks the backend for data — machines, work orders, inventory, analytics, and so on. "
    "4. The backend reads PostgreSQL and returns it. "
    "5. A live WebSocket keeps the dashboard moving in real time. "
    "Every screen you see is the dashboard asking the backend a question and drawing the answer.", body))
story.append(Spacer(1, 4))
story.append(Paragraph("Two kinds of data — this is the key idea", h2))
story.append(Paragraph(
    "<b>(A) Human / system data</b> — work orders, inventory, quality checks, purchase orders, maintenance. In a real "
    "factory this is entered by people (operators, supervisors, store, QC) or imported from an existing system "
    "(Tally, an ERP, Excel). FlowMES then keeps it live and connected.", body))
story.append(Paragraph(
    "<b>(B) Machine / sensor data</b> — temperature, vibration, RPM, power, run status, part counts. This comes "
    "straight off the machines and their PLCs through a small <b>edge agent</b> on your shop floor (explained in "
    "Section 4). No one types this in.", body))
story.append(PageBreak())

# ── 2. Core MES ───────────────────────────────────────────────────
story.append(Paragraph("2. Core MES (included in every plan)", h1))
story.append(Paragraph("Real-time machine monitoring — the foundation every other module builds on.", small))
hr()
module("Overview",
       "The top-level pulse of the plant: running machines, breakdowns, average utilization and OEE, total downtime, shift efficiency and the top downtime reason.",
       "It doesn't have its own input — it aggregates everything from the modules below.",
       "Automatic. As soon as machines, downtime, shifts and production records exist, this fills in.")
module("Machines",
       "A register of every machine with its live status (Running / Idle / Breakdown / Maintenance) and utilization %.",
       "You set up the machine list once. The live status comes from each machine's PLC/sensors via the edge agent, or from a manual status update by a supervisor.",
       "Add machines on this tab. Live status then flows in from the Connectivity / IoT layer, or the edge agent updates a machine's status automatically.")
module("Downtime",
       "A log of every stoppage with its reason, duration and notes — and the management report's 'top loss reason'.",
       "Operators record why a machine stopped; or the system auto-creates an entry the moment a machine flips to Breakdown.",
       "Manual entry on the Downtime tab, or automatic from machine status changes / PLC fault signals.")
module("Shifts",
       "Per-shift target vs actual output and the resulting efficiency %.",
       "The shift supervisor enters actual output, or it rolls up automatically from operator/production counts.",
       "Manual entry, or derived from production records.")
module("Analytics",
       "OEE broken into Availability x Performance x Quality, shift KPIs, and a downtime pareto (which reasons cost the most time).",
       "Computed from production records, downtime logs and shifts — the headline OEE metric of any MES.",
       "Fully derived — nothing to enter.")
module("Timeline",
       "A chronological feed of machine status-change events (Running to Breakdown, etc.).",
       "Every time a machine changes state, the change is logged.",
       "Automatic — whenever a machine changes state, whether by a person or a PLC signal.")
story.append(PageBreak())

# ── 3. Operations Pack ────────────────────────────────────────────
story.append(Paragraph("3. Operations Pack", h1))
story.append(Paragraph("Planning and running production jobs end to end.", small))
hr()
module("Work Orders",
       "Production jobs: part number, batch, target quantity, assigned machine, deadline and status (Planned to In Progress to Completed). "
       "Marking a work order Completed automatically issues the raw material it consumed and adds the finished goods to stock, using the Bill of Materials (BOM).",
       "In a real factory a work order originates in the ERP or planning system, or from a confirmed customer/sales order; the planner then releases it to the floor.",
       "Three ways: (1) create it manually here; (2) sync from your ERP (Tally / SAP / Excel) via CSV import or an API integration; (3) auto-generate it from a customer order. The BOM-driven stock deduction then happens on its own.")
module("Production Plan",
       "Assigns work orders to specific machines, shifts and dates, tracking planned vs actual quantity so you can spot slippage early.",
       "The production planning team.",
       "Manual, built on top of work orders.")
module("Scheduling",
       "A calendar view of all jobs across machines and shifts; reassign and reschedule when a machine goes down for maintenance.",
       "The planning team.",
       "Manual scheduling; reflects work orders and machine availability.")
module("Operator Terminal",
       "Where a shop-floor operator starts and finishes a job at the machine and logs good and rejected piece counts.",
       "Operators on a tablet or terminal at the line. In more automated lines, the good/reject counts come from the machine's own part counter via the PLC.",
       "Manual entry by operators, or automatic counts pulled from the PLC through the edge agent.")
module("Orders and Dispatch",
       "Customer orders and dispatch/shipment tracking, linked back to the work orders that fulfil them.",
       "Sales, ERP or CRM.",
       "Manual entry, or integrate with your sales/ERP system.")
story.append(PageBreak())

# ── 4. Factory Pack ───────────────────────────────────────────────
story.append(Paragraph("4. Factory Pack", h1))
story.append(Paragraph("Full shop-floor visibility — materials, quality and maintenance.", small))
hr()
module("Maintenance AI",
       "Predictive-maintenance risk scoring per machine, flagging machines likely to need attention.",
       "Computed from each machine's downtime history and vibration/telemetry.",
       "Derived from the machine and IoT data already in FlowMES.")
module("CMMS",
       "Maintenance management: preventive and breakdown tasks, schedules, spare parts used and downtime per task, building a service history per machine.",
       "The maintenance team.",
       "Manual; preventive schedules automatically raise upcoming tasks.")
module("Quality",
       "Inspections logged against each work order: quantity inspected, passed, failed, defect category, rework and scrap — full batch traceability.",
       "QC inspectors at inspection stations.",
       "Manual entry; in advanced setups, results can come from digital gauges or machine-vision systems.")
module("Inventory",
       "Live stock of raw materials, spares and finished goods, with every receive / issue / return / adjustment recorded and low-stock alerts. "
       "For a compressor manufacturer like GMATS it adds the enterprise model: 4-bucket stock (Physical / Reserved / Available), item aliases (many names to one item), "
       "Proforma reservation, Tax-Invoice deduction (with a printable GST invoice), free-spares issue notes, cycle counts and a variance report.",
       "The store and purchase teams. Opening stock is imported from Tally.",
       "Manual entry; Tally/Excel CSV import; automatic deduction when a work order completes (BOM); and the full Proforma to Tax-Invoice flow for sales.")
module("Purchasing",
       "Purchase orders and a supplier master; receiving a PO updates stock and creates a receipt transaction.",
       "The purchase team.",
       "Manual; can integrate with supplier portals or ERP.")
module("Digital Twin",
       "A live map of the factory floor showing machine positions and their real-time status.",
       "Machine coordinates configured once, plus live status.",
       "Lay the machines out once; status then streams in from the IoT / Connectivity layer.")
story.append(PageBreak())

# ── 5. Intelligence Pack ──────────────────────────────────────────
story.append(Paragraph("5. Intelligence Pack", h1))
story.append(Paragraph("Live machine data, AI and connectivity.", small))
hr()
module("IoT Command",
       "Live machine telemetry — temperature, vibration, spindle speed, power — refreshed every few seconds, with alerts when a machine runs outside safe limits.",
       "Sensors on the machines.",
       "An edge device on your network reads the sensors and publishes the readings over MQTT to FlowMES.")
module("AI Insights",
       "Rule-based recommendations (schedule a bearing inspection, change tooling, lubrication overdue) each with a confidence score.",
       "Computed from machine, quality and downtime patterns.",
       "Derived automatically.")
module("AI Copilot",
       "Ask questions about your factory in plain English ('why is OEE low?', 'what should I reorder?'), get AI root-cause analysis and a one-click daily management report — powered by Claude.",
       "It reads the live data already in FlowMES; nothing extra to enter.",
       "Off by default. To switch on: add ANTHROPIC_API_KEY in Railway (optionally AI_MODEL, default a cheap fast model). That single environment variable is the only step.")
module("Connectivity",
       "The industrial protocol adapter layer — OPC UA, Modbus TCP, Siemens S7, Allen-Bradley, Beckhoff and Omron — listing connected PLCs and their live signals.",
       "The PLCs that control your machines.",
       "Run the FlowMES edge agent on a small PC on your shop-floor network. It speaks each PLC's native protocol (using the vendor library), reads the tags/registers, normalises them and pushes them to FlowMES over an outbound connection. See Section 7.")
module("Executive OEE",
       "Management-level OEE dashboards and trends for the plant.",
       "Production records.",
       "Derived.")
module("Escalations",
       "Automatic alerts — low stock, breakdown, overdue work order, OEE below target — routed to an owner and department so nothing slips.",
       "Rules running over the live data.",
       "Auto-generated; can also be raised manually.")
module("Notifications",
       "A system notification feed (alerts, warnings, info) for the team.",
       "Events across the system.",
       "Automatic.")
story.append(PageBreak())

# ── 6. Admin Pack ─────────────────────────────────────────────────
story.append(Paragraph("6. Admin Pack", h1))
story.append(Paragraph("Management, compliance and the SaaS platform layer.", small))
hr()
module("Documents",
       "Controlled documents — SOPs, quality plans, maintenance plans, compliance certificates — with version, owner and review-due date.",
       "Your document-control process.",
       "Manual entries / links (e.g. links to cloud storage).")
module("SaaS Admin",
       "Tenant / company management for the platform: plan, seats, subscription status and monthly fee.",
       "You, the platform operator.",
       "Platform-owner view.")
module("User Management",
       "Add employees and assign roles (Admin / Supervisor / Operator) and reset passwords — scoped to your own company. Self-registration is disabled; only an Admin can add people.",
       "Your company's Admin.",
       "The Admin adds users in-app; each new user can only sign in with the role assigned.")
module("Costing",
       "Cost records (material, labour, overhead) linked to jobs, for basic cost visibility.",
       "Finance / accounts.",
       "Manual; can be derived from production and inventory movements.")
module("Enterprise Polish",
       "The platform plumbing: audit log (who did what, when), system health, an executive summary and report requests.",
       "The system itself.",
       "Automatic.")
story.append(PageBreak())

# ── 7. How machine data physically gets in ────────────────────────
story.append(Paragraph("7. How real machine data gets into FlowMES", h1))
hr()
story.append(Paragraph(
    "This is the question every factory asks. The path is the same whether the machine speaks MQTT or a PLC protocol:", body))
story.append(Spacer(1, 3))
flow = Table([[
    Paragraph("<b>Machine sensors / PLC</b><br/>on the shop floor", small),
    Paragraph("&#8594;", h2),
    Paragraph("<b>Edge agent</b><br/>small PC on your local network", small),
    Paragraph("&#8594;", h2),
    Paragraph("<b>FlowMES cloud</b><br/>FastAPI + PostgreSQL", small),
    Paragraph("&#8594;", h2),
    Paragraph("<b>Dashboard</b><br/>live in the browser", small),
]], colWidths=[3.3 * cm, 0.8 * cm, 3.6 * cm, 0.8 * cm, 3.3 * cm, 0.8 * cm, 3.0 * cm])
flow.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (0, 0), SOFT), ("BACKGROUND", (2, 0), (2, 0), SOFT),
    ("BACKGROUND", (4, 0), (4, 0), SOFT), ("BACKGROUND", (6, 0), (6, 0), SOFT),
    ("BOX", (0, 0), (0, 0), 0.5, LINE), ("BOX", (2, 0), (2, 0), 0.5, LINE),
    ("BOX", (4, 0), (4, 0), 0.5, LINE), ("BOX", (6, 0), (6, 0), 0.5, LINE),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
]))
story.append(flow)
story.append(Spacer(1, 8))
story.append(Paragraph("The edge agent — what it is", h2))
story.append(Paragraph(
    "A lightweight program on a small industrial PC or Raspberry Pi sitting on your factory's local network. It reads from your "
    "machines two ways: (1) modern machines that already publish data — it subscribes over <b>MQTT</b>; (2) PLC-controlled machines "
    "— it speaks the PLC's protocol directly (OPC UA, Modbus, Siemens S7, Allen-Bradley, Beckhoff, Omron) using that protocol's "
    "library. It then sends the readings to the FlowMES cloud.", body))
story.append(Paragraph('"But our PLCs don\'t allow external connections"', h2))
story.append(Paragraph(
    "That's exactly why the edge agent exists, and it's the common case. You do <b>not</b> open any port on the PLC to the internet. "
    "The edge agent talks to the PLC <b>locally</b>, and makes only <b>outbound</b> connections to FlowMES — the same direction your "
    "web browser uses. The PLC never sees external traffic. If a machine has no digital output at all, simple add-on sensors "
    "(temperature, current, vibration) feed the edge agent instead — no change to the machine.", body))
story.append(Paragraph("Latency and safety", h2))
story.append(Paragraph(
    "For monitoring and dashboards, data is live within 1-3 seconds. Safety-critical control (e.g. auto-shutdown on overpressure) "
    "stays in the local PLC/edge layer so it responds in milliseconds without depending on the cloud. The cloud handles analytics, "
    "inventory, work orders and reporting — not real-time safety control.", body))
story.append(PageBreak())

# ── 8. Security, multi-company, deployment, running ───────────────
story.append(Paragraph("8. Security, multi-company and running it", h1))
hr()
story.append(Paragraph("Security and access", h2))
story.append(Paragraph(
    "Passwords are stored with bcrypt. All traffic is over HTTPS. Access is role-based — Admin (full), Supervisor "
    "(create/approve), Operator (update only) — enforced on the server, not just hidden in the UI. Each company's data is "
    "<b>isolated by tenant</b> and enforced on the backend, so one client can never see another's data.", body))
story.append(Paragraph("Multi-company (white-label SaaS)", h2))
story.append(Paragraph(
    "FlowMES is multi-tenant: each customer is a 'company' with its own data, its own logins, its own branding (name, logo, "
    "colour) and its own licensed module packs (Starter / Growth / Enterprise). Onboarding a new client is a checklist — create "
    "the company, pick its plan, import or seed its data, create its admin login — no code change.", body))
story.append(Paragraph("Where it's deployed", h2))
story.append(Paragraph(
    "Frontend on <b>Vercel</b> (flow-mes.vercel.app), backend + PostgreSQL on <b>Railway</b> "
    "(flowmes-production.up.railway.app). Both auto-deploy from the Git repository. HTTPS is automatic on both.", body))
story.append(Paragraph("How to run it", h2))
story.append(Paragraph(
    "<b>Use it now:</b> open flow-mes.vercel.app and sign in (it's always live). "
    "<b>Run locally for development:</b> start the backend (FastAPI on port 8000, needs a PostgreSQL DATABASE_URL) and the "
    "frontend (Next.js on port 3000) in two terminals; the frontend is already pointed at the local backend.", body))
story.append(Spacer(1, 8))
story.append(HRFlowable(width="100%", thickness=0.7, color=LINE))
story.append(Spacer(1, 4))
story.append(Paragraph(
    "FlowMES — one system for machines, materials, production and quality, built for the way Indian SME manufacturers actually work.",
    cover_m))

doc = SimpleDocTemplate(OUTPUT, pagesize=A4, rightMargin=1.8 * cm, leftMargin=1.8 * cm, topMargin=1.8 * cm, bottomMargin=1.6 * cm,
                        title="FlowMES — Complete Guide", author="FlowMES")
doc.build(story)
print("Wrote", OUTPUT)
