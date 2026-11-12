# VDA 5050: driving real (and really-simulated) vehicles from AMP

AMP's fleet pack speaks [VDA 5050](https://github.com/VDA5050/VDA5050) as the
**master control**: vehicles publish `state`/`connection`, AMP publishes
`order`/`instantActions`, all JSON over MQTT on

    uagv/v2/{manufacturer}/{serial}/{channel}

The adapter is `backend/fleet_vda5050.py`. It is off unless configured.

## Turn it on

Set on the AMP backend (local `.env` or Railway variables):

| variable | meaning | default |
|---|---|---|
| `VDA5050_BROKER` | broker host; **setting this is the on-switch** | unset (off) |
| `VDA5050_PORT` | broker port | `1883` |
| `VDA5050_USERNAME` / `VDA5050_PASSWORD` | broker account | anonymous |
| `VDA5050_TLS` | `1` to enable TLS | off |
| `VDA5050_INTERFACE` / `VDA5050_VERSION` | topic prefix segments | `uagv` / `v2` |
| `VDA5050_CLIENT_ID` | stable subscriber id | `amp-vda5050` |

## Register the vehicle (fail-closed, like gateways)

A VDA 5050 topic carries no tenant, so the vehicle's identity must be
registered to a workspace **before** its messages count; unregistered
identities are dropped with a log line, never auto-created.

    POST /fleet/vehicles
    {"name": "CARTER-01", "vehicle_type": "AMR", "adapter": "vda5050",
     "vda_manufacturer": "RobotCompany", "vda_serial": "carter-01"}

From then on the row is driven by the vehicle: `fleet_engine.advance()` skips
it, and its position/battery/status/task-progress come from its own reports.
Dispatch, the assistance queue and every dashboard read are unchanged —
simulated and real vehicles share one roster.

## Test rig A — no Docker (any machine)

```bash
# 1. a broker (Windows service install also works):
mosquitto -v
# 2. AMP with the adapter on:
VDA5050_BROKER=127.0.0.1 python -m uvicorn main:app --port 8000
# 3. spec-faithful simulated vehicles (same behaviour as NVIDIA's):
python vda5050_vehicle_sim.py --robots RobotCompany/carter-01,RobotCompany/carter-02
```

Register the identities, raise a transport task (or let a Running machine
raise one), and watch Fleet Control: the vehicles drive the orders, deliver,
and plead when `inject_fatal` fires.

## Test rig B — NVIDIA Isaac Mission Simulator (Docker)

NVIDIA publishes the simulated robots its own Mission Dispatch tests with
([mission-simulator on NGC](https://catalog.ngc.nvidia.com/orgs/nvidia/teams/isaac/containers/mission-simulator),
[isaac_mission_dispatch on GitHub](https://github.com/nvidia-isaac/isaac_mission_dispatch)).

```bash
cd infra/vda5050
docker compose up
# then point AMP at it:
VDA5050_BROKER=127.0.0.1 python -m uvicorn main:app --port 8000
```

Check the container's `--robots` syntax against the image tag you pull — NGC
versions have varied the argument format; `docker run <image> --help` settles
it. The robots' manufacturer/serial must match what you registered.

## What maps to what

| VDA 5050 | AMP |
|---|---|
| `state.agvPosition.x/y` | `FleetVehicle.x/y` (metres, `amp-floor` map = the 100×100 grid) |
| `state.batteryState.batteryCharge` | `FleetVehicle.battery_pct` (clamped 0–100) |
| `state.errors[].errorLevel == FATAL` | status `Stuck` + ONE open plea in the assistance queue |
| `connection OFFLINE/CONNECTIONBROKEN` | status `Offline` (task kept — a human decides) |
| order node `pickup` reached | task `InTransit`, vehicle `Carrying` |
| order finished (dropoff, not driving) | task `Delivered`, counters advance |
| advice `resume` | the same order re-published, `orderUpdateId + 1` |
| advice `return_task` / `send_to_charge`, task cancel | `instantActions: cancelOrder` |

Orders are three released nodes (start → pickup → dropoff) with **no VDA
actions** — node traversal is the part of the spec every vendor implements
identically, so the adapter works against any compliant vehicle out of the
box; vendor-specific pick/drop actions are a later, per-integration layer.
