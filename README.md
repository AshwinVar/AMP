# FlowMES Enterprise

Enterprise-grade MES (Manufacturing Execution System) platform for SMEs and smart factories.

FlowMES provides real-time shopfloor visibility, machine monitoring, downtime tracking, production analytics, predictive maintenance, operator execution, and industrial IoT integration.

---

## Features

### Core MES

* Real-time machine monitoring
* Downtime tracking & root cause visibility
* Shift management
* Production planning
* Work order execution
* Operator terminal
* Scheduling
* Orders & dispatch

### Manufacturing Intelligence

* OEE analytics
* Executive dashboards
* AI maintenance insights
* Predictive recommendations
* Escalation management
* Notifications system
* Digital twin simulation

### Factory Operations

* Inventory management
* Purchasing
* Quality management
* CMMS (Maintenance)
* Timeline tracking
* Document management

### Industrial IoT (Phase 30)

* MQTT integration
* PLC signal mapping
* Device telemetry
* Industrial gateway layer
* OPC-UA ready architecture
* Real-time telemetry simulation

---

## Tech Stack

### Frontend

* Next.js 16
* React
* TypeScript
* Tailwind CSS

### Backend

* FastAPI
* SQLAlchemy
* PostgreSQL / SQLite
* MQTT (Paho MQTT)
* WebSockets

### Infrastructure

* Vercel (Frontend deployment)
* GitHub
* PostgreSQL
* MQTT Broker

---

## Project Architecture

```text
FlowMES/
├── backend/
│   ├── main.py
│   ├── models.py
│   ├── mqtt_service.py
│   ├── predictive_engine.py
│   ├── analytics_engine.py
│   └── ...
│
├── frontend/
│   ├── app/
│   ├── components/
│   ├── lib/
│   └── ...
│
└── database/
```

---

## Current Modules

* Overview Dashboard
* Machines
* Downtime
* Shifts
* Analytics
* Timeline
* Work Orders
* Production Planning
* Scheduling
* Operator Terminal
* Maintenance AI
* Escalations
* Inventory
* Quality
* Executive OEE
* Digital Twin
* Orders & Dispatch
* Purchasing
* Documents
* Notifications
* Enterprise Admin
* Industrial Gateway

---

## Real-Time Capabilities

FlowMES supports:

* MQTT live machine telemetry
* WebSocket live updates
* Real-time dashboard refresh
* Simulated PLC communication
* Industrial device monitoring

---

## Running Locally

### Backend

```bash
cd backend
.\venv\Scripts\python.exe -m uvicorn main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### MQTT Simulator

```bash
cd backend
.\venv\Scripts\python.exe mqtt_machine_publisher.py
```

---

## Roadmap

### Completed

* Phase 1–30 development
* Enterprise dashboard UI
* Live MQTT simulation
* Industrial gateway architecture

### Upcoming

* OPC-UA integration
* Real PLC connectivity
* ERP integration
* SaaS multi-tenancy
* Customer onboarding portal
* Cloud deployment

---

## Author

**Ashwin Vardharajan**
Senior Developer & Technical Consultant

MSc Data Science & Analytics
Autonomous Systems • MES • Robotics • AI • Industrial IoT
