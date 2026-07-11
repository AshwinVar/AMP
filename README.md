# AMP Enterprise

Enterprise-grade MES (Manufacturing Execution System) platform for SMEs and smart factories.

AMP provides real-time shopfloor visibility, machine monitoring, downtime tracking, production analytics, predictive maintenance, operator execution, and industrial IoT integration.

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
AMP/
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

AMP supports:

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

## PLC Simulator (Phase 30)

AMP includes a simulated PLC telemetry generator for industrial testing and real-time MES validation.

The simulator publishes live MQTT telemetry to the AMP broker and updates:

* Machine status
* Utilization %
* Downtime
* Production counts
* Rejection counts
* Temperature
* Vibration
* Machine events

### Start MQTT Broker

Ensure your MQTT broker is running locally on:

```text
127.0.0.1:1883
```

### Run PLC Simulator

```bash
cd backend
.\venv\Scripts\python.exe phase30_plc_simulator.py
```

### Example Live Payload

```json
{
  "machine": "CNC-01",
  "status": "Running",
  "utilization": 87,
  "downtime": "0 min",
  "planned_minutes": 480,
  "runtime_minutes": 390,
  "ideal_cycle_time_seconds": 60,
  "total_count": 412,
  "good_count": 401,
  "rejected_count": 11,
  "temperature": 67,
  "vibration": 4,
  "source": "phase30_plc_simulator"
}
```

### Real-Time Flow

```text
PLC Simulator
      ↓
MQTT Broker
      ↓
AMP MQTT Service
      ↓
PostgreSQL / SQLite
      ↓
WebSocket Broadcast
      ↓
Live Dashboard Updates
```

This enables realistic smart factory simulation without requiring physical PLC hardware.

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



