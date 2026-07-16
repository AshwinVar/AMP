# AMP

**An AI operating system for manufacturing.**

AMP is a multi-tenant platform that pairs a real-time MES (Manufacturing Execution System) core with an **AI platform** of autonomous agents and live read-models. The MES is the foundation — machine monitoring, downtime, work orders, quality, inventory — and on top of it a fleet of AI agents observes the factory's event stream, proposes bounded actions under human oversight, and a layer of read-models projects everything into decision surfaces the shopfloor and the owner actually use.

Built for SME manufacturers and smart factories; evolving from an MES into an AI OS via incremental (strangler) architecture — never a rewrite, backward-compatible with the live GMATS pilot.

---

## The exec cockpit

The Overview home opens with four self-contained, live read-model cards — *how's my factory* answered in one glance:

| Card | Answers | Shows |
|------|---------|-------|
| **Factory Pulse** | How's my factory, and what needs me? | Fleet health · agent workload · approvals awaiting you |
| **Quality** | How good is what we're making? | First-pass yield · defect Pareto · worst machines |
| **Downtime** | What's stopping my machines? | Top reasons (Pareto) · most-affected machines · 7-day trend |
| **Production** | How much are we making? | Good rate · throughput sparkline · top producers |

Each card fetches its own data, refreshes live, and hides itself when there's nothing to show.

---

## AI platform

The AI is a platform, not scattered scripts. Business modules consume `ai.<service>` through stable contracts; concrete engines (rule-based today) sit behind them, so a scorer can become an ML model or an LLM later without any consumer changing.

- **Event-driven backbone** — an in-process domain event bus (`ProductionCompleted`, `DowntimeStarted`, `InventoryLow`, `QualityInspectionFailed`, …) that the AI platform and agents subscribe to.
- **Multi-tenant by construction** — every query is auto-scoped to the caller's tenant at the ORM layer; stamped tables are filtered explicitly. Leak-proof by default.
- **Prediction · Recommendations · Copilot** — predictive-maintenance risk scoring, AI recommendations, and a natural-language copilot (LLM-optional) behind one platform surface.

### Autonomous agents (with oversight)

Four agents observe the stream and **propose** bounded actions — they create the item in a pending state, log an `AgentAction` (audit trail + approval queue), and either auto-approve trusted low-risk actions by policy or wait for a human:

| Agent | Watches | Proposes |
|-------|---------|----------|
| **Maintenance** | Critical machine risk | A maintenance task |
| **Quality** | High-fail inspections | A machine inspection |
| **Reorder** | Low stock | A replenishment PO *(auto-approved by policy)* |
| **Escalation** | Repeated downtime | An escalation to the maintenance lead |

Which agents auto-approve is configurable via the `AUTO_APPROVE_AGENTS` env var. Approve advances the item; reject cancels it.

### Read-model layer

Nine pure projections that each *answer one question* by composing signals from existing tables (and from other read-models) — no new storage, tenant-scoped, unit-tested in isolation, surfaced through self-contained UI components:

`insights` (Mission Control feed) · `twin` (Machine Health + single-machine cockpit) · `impact` (agent-fleet ROI) · `pulse` (Factory Pulse) · `roster` (the AI workforce) · `trends` (agent activity) · `downtime` (Pareto) · `quality` (first-pass yield) · `production` (throughput).

---

## Core MES

* Real-time machine monitoring & live WebSocket status
* Downtime tracking & root-cause visibility
* Shift management, production planning, scheduling
* Work order execution & operator terminal
* Orders & dispatch

## Factory operations

* Inventory management & purchasing
* Quality inspections
* CMMS (maintenance)
* Escalations, notifications, timeline, documents

## Industrial IoT

* MQTT integration & PLC signal mapping
* Device telemetry & industrial gateway layer
* OPC-UA-ready architecture
* Real-time telemetry simulation (no physical hardware required)

---

## Architecture Decision Records

Significant decisions are recorded in [`docs/adr/`](docs/adr/) so the *why* survives as the codebase evolves:

| ADR | Decision |
|-----|----------|
| [0001](docs/adr/0001-domain-event-bus.md) | Introduce a domain event bus |
| [0002](docs/adr/0002-tenant-scope-core-domain.md) | Tenant-scope the core domain |
| [0003](docs/adr/0003-ai-as-event-consuming-platform.md) | AI as an event-consuming platform |
| [0004](docs/adr/0004-ai-agents-act-on-the-stream.md) | AI agents — act on the stream |
| [0005](docs/adr/0005-agent-oversight.md) | Agent oversight: propose, log, approve |
| [0006](docs/adr/0006-machine-health-twin.md) | Machine Health twin (per-machine read-model) |
| [0007](docs/adr/0007-read-models-projections.md) | Read-models: projections that answer one question |

---

## Tech stack

**Frontend** — Next.js 16 · React 19 · TypeScript · Tailwind CSS · deployed on Vercel
**Backend** — FastAPI · SQLAlchemy · PostgreSQL / SQLite · Paho MQTT · WebSockets · deployed on Railway
**Infrastructure** — Railway (backend + Postgres) · Vercel (frontend) · GitHub Actions CI (backend + frontend)

```text
AMP/
├── backend/
│   ├── main.py            # FastAPI app (~210 routes)
│   ├── models.py          # SQLAlchemy models
│   ├── events.py          # domain event bus (ADR-0001)
│   ├── ai/                # AI platform: prediction, recommendations, copilot,
│   │                      #   agents, and the read-models (twin, pulse, impact,
│   │                      #   roster, trends, downtime, quality, production)
│   ├── predictive_engine.py
│   └── test_*.py          # per-read-model / per-agent tests
│
├── frontend/
│   ├── app/dashboard/     # the dashboard shell + Overview cockpit
│   ├── components/        # self-contained section + snapshot components
│   └── lib/
│
└── docs/adr/              # Architecture Decision Records
```

---

## Running locally

**Backend**

```bash
cd backend
.\venv\Scripts\python.exe -m uvicorn main:app --reload
```

**Frontend**

```bash
cd frontend
npm install
npm run dev
```

**Tests** (each is a standalone script; exit 0 = pass)

```bash
cd backend
python test_agents.py      # the agent fleet
python test_pulse.py       # a read-model (twin / impact / pulse / …)
```

---

## Real-time & PLC simulation

AMP includes a simulated PLC telemetry generator so you can exercise the live pipeline without physical hardware. It publishes MQTT telemetry (status, utilization, downtime, production/rejection counts, temperature, vibration) to the broker, which flows through the AMP MQTT service into the database and out over WebSockets to the dashboard.

```text
PLC Simulator → MQTT Broker → AMP MQTT Service → PostgreSQL → WebSocket → Live Dashboard
```

```bash
cd backend
.\venv\Scripts\python.exe phase30_plc_simulator.py    # PLC telemetry
.\venv\Scripts\python.exe mqtt_machine_publisher.py    # machine publisher
```

Point the broker at `127.0.0.1:1883` for local runs.

---

## Roadmap

**Shipped**
* Event-driven core & multi-tenant isolation (ADR-0001/0002)
* AI platform: prediction, recommendations, copilot (ADR-0003)
* Autonomous agent fleet + oversight (ADR-0004/0005)
* Read-model layer + exec cockpit (ADR-0006/0007)
* Live MQTT / PLC simulation & industrial gateway

**Next**
* ML/LLM engines behind the existing AI interfaces
* OPC-UA integration & real PLC connectivity
* ERP integration
* Customer onboarding portal

---

## Author

**Ashwin Vardharajan** — Founder
MSc Data Science & Analytics · Autonomous Systems • MES • Robotics • AI • Industrial IoT

Part of [MARX8](https://marx8.com).
