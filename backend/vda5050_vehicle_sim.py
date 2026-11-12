"""A VDA 5050 vehicle simulator — the counterparty for fleet_vda5050.

WHAT THIS IS. A small, spec-faithful VDA 5050 *client* (the vehicle side),
behaviourally equivalent to the robots NVIDIA's Isaac Mission Simulator runs:
it announces itself on `connection` (retained, with a last-will), publishes
`state` at 1 Hz, accepts `order`s and drives their released nodes in straight
lines at a fixed speed, and honours the `cancelOrder` instantAction. Nothing
AMP-specific is in here — point it at any VDA 5050 master control and it will
behave; point Isaac Mission Dispatch at it and it will drive.

WHY IT EXISTS WHEN NVIDIA SHIPS ONE. Two reasons. (1) The NVIDIA simulator is
a Docker container; this is `python vda5050_vehicle_sim.py`, which runs on the
dev laptop and in any shell with paho installed — the integration can be
demonstrated end-to-end with mosquitto alone. (2) Its movement core is pure
(Vehicle.step below), so test_fleet_vda5050.py exercises the full round trip —
order out, motion, state in, task flipped — without a broker or a thread.
infra/vda5050/README.md has the commands for running the real NVIDIA container
against the same adapter.

Run:
    python vda5050_vehicle_sim.py --broker 127.0.0.1 --port 1883 \
        --robots RobotCompany/carter-01,RobotCompany/carter-02 \
        --x 8 --y 85 --speed 1.4

Importing is side-effect-free (the mqtt_listener lesson: runtime under
__main__ only).
"""
import argparse
import json
import math
import threading
import time
import uuid
from itertools import count

INTERFACE = "uagv"
MAJOR_VERSION = "v2"
PROTOCOL_VERSION = "2.0.0"


def _ts():
    t = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
    return f"{base}.{int((t % 1) * 1000):03d}Z"


class Vehicle:
    """One simulated AGV: pure motion + message building, no I/O.

    The spec's execution model, reduced to what node-only orders need:
    accepted nodes are traversed in sequence order; `lastNodeId`/-SequenceId
    advance as each node is reached; `nodeStates` holds what remains;
    `driving` is true between nodes. An order update with the same orderId
    and a HIGHER orderUpdateId replaces the order (resume); a lower or equal
    one is rejected onto `errors`, exactly as the spec demands — that
    rejection is what fleet_vda5050's fresh-orderId-per-assignment exists to
    avoid.
    """

    def __init__(self, manufacturer, serial, x=0.0, y=0.0, speed=1.4, battery=100.0):
        self.manufacturer = manufacturer
        self.serial = serial
        self.x, self.y = float(x), float(y)
        self.speed = float(speed)
        self.battery = float(battery)
        self.order_id = ""
        self.order_update_id = 0
        self.nodes = []             # nodes still to traverse, in sequence order
        self.last_node_id = ""
        self.last_node_seq = 0
        self.driving = False
        self.errors = []
        self.header_ids = count(1)
        self.seen_orders = {}       # orderId -> highest orderUpdateId accepted

    # ── inbound ──────────────────────────────────────────────────────
    def accept_order(self, order):
        order_id = order.get("orderId", "")
        update_id = int(order.get("orderUpdateId", 0))
        if order_id in self.seen_orders and update_id <= self.seen_orders[order_id]:
            self.errors.append({
                "errorType": "orderUpdateError", "errorLevel": "WARNING",
                "errorDescription": f"stale orderUpdateId {update_id} for {order_id}"})
            return False
        self.seen_orders[order_id] = update_id
        self.order_id = order_id
        self.order_update_id = update_id
        released = [n for n in order.get("nodes", []) if n.get("released")]
        self.nodes = sorted(released, key=lambda n: n.get("sequenceId", 0))
        self.errors = [e for e in self.errors if e.get("errorLevel") != "FATAL"]
        self.driving = bool(self.nodes)
        return True

    def accept_instant_actions(self, payload):
        for action in payload.get("actions", []):
            if action.get("actionType") == "cancelOrder":
                self.nodes = []
                self.driving = False
                self.order_id = ""
                self.order_update_id = 0

    def inject_fatal(self, error_type="navigationBlocked"):
        """A test / demo hook: the vehicle hits something it cannot resolve."""
        self.errors.append({"errorType": error_type, "errorLevel": "FATAL"})
        self.driving = False

    def clear_errors(self):
        self.errors = []
        self.driving = bool(self.nodes)

    # ── motion ───────────────────────────────────────────────────────
    def step(self, seconds):
        """Advance toward the next released node. Fatal errors park it."""
        if any(e.get("errorLevel") == "FATAL" for e in self.errors):
            return
        while seconds > 0 and self.nodes:
            node = self.nodes[0]
            pos = node.get("nodePosition") or {}
            tx, ty = float(pos.get("x", self.x)), float(pos.get("y", self.y))
            d = math.hypot(tx - self.x, ty - self.y)
            reach = self.speed * seconds
            if reach >= d:
                self.x, self.y = tx, ty
                seconds -= (d / self.speed) if self.speed else 0
                self.last_node_id = node.get("nodeId", "")
                self.last_node_seq = int(node.get("sequenceId", 0))
                self.nodes.pop(0)
                self.battery = max(0.0, self.battery - 0.02 * d)
            else:
                f = reach / d if d else 0
                self.x += (tx - self.x) * f
                self.y += (ty - self.y) * f
                self.battery = max(0.0, self.battery - 0.02 * reach)
                seconds = 0
        self.driving = bool(self.nodes)

    # ── outbound ─────────────────────────────────────────────────────
    def topic(self, channel):
        return f"{INTERFACE}/{MAJOR_VERSION}/{self.manufacturer}/{self.serial}/{channel}"

    def _header(self):
        return {"headerId": next(self.header_ids), "timestamp": _ts(),
                "version": PROTOCOL_VERSION, "manufacturer": self.manufacturer,
                "serialNumber": self.serial}

    def state_message(self):
        body = {
            "orderId": self.order_id,
            "orderUpdateId": self.order_update_id,
            "lastNodeId": self.last_node_id,
            "lastNodeSequenceId": self.last_node_seq,
            "driving": self.driving,
            "paused": False,
            "operatingMode": "AUTOMATIC",
            "nodeStates": [{"nodeId": n.get("nodeId"),
                            "sequenceId": n.get("sequenceId"),
                            "released": True} for n in self.nodes],
            "edgeStates": [],
            "actionStates": [],
            "errors": list(self.errors),
            "batteryState": {"batteryCharge": round(self.battery, 1),
                             "charging": False},
            "agvPosition": {"x": round(self.x, 3), "y": round(self.y, 3),
                            "theta": 0.0, "mapId": "amp-floor",
                            "positionInitialized": True},
            "safetyState": {"eStop": "NONE", "fieldViolation": False},
        }
        body.update(self._header())
        return body

    def connection_message(self, connection_state):
        body = {"connectionState": connection_state}
        body.update(self._header())
        return body


def run_vehicle(vehicle, broker, port, username=None, password=None, hz=1.0):
    """Wire one Vehicle to a broker and loop forever. Imported lazily so the
    pure core above needs no paho at test time."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(client_id=f"vda-sim-{vehicle.serial}-{uuid.uuid4().hex[:6]}")
    if username:
        client.username_pw_set(username, password)
    client.will_set(vehicle.topic("connection"),
                    json.dumps(vehicle.connection_message("CONNECTIONBROKEN")),
                    qos=1, retain=True)

    def on_connect(c, u, flags, rc):
        print(f"[{vehicle.serial}] connected rc={rc}")
        c.subscribe(vehicle.topic("order"), qos=1)
        c.subscribe(vehicle.topic("instantActions"), qos=1)
        c.publish(vehicle.topic("connection"),
                  json.dumps(vehicle.connection_message("ONLINE")),
                  qos=1, retain=True)

    def on_message(c, u, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if msg.topic.endswith("/order"):
            ok = vehicle.accept_order(payload)
            print(f"[{vehicle.serial}] order {payload.get('orderId')} "
                  f"update {payload.get('orderUpdateId')} "
                  f"{'accepted' if ok else 'REJECTED (stale)'}")
        elif msg.topic.endswith("/instantActions"):
            vehicle.accept_instant_actions(payload)
            print(f"[{vehicle.serial}] instantActions handled")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker, port, 60)
    client.loop_start()
    interval = 1.0 / hz
    try:
        while True:
            vehicle.step(interval)
            client.publish(vehicle.topic("state"),
                           json.dumps(vehicle.state_message()), qos=0)
            time.sleep(interval)
    except KeyboardInterrupt:
        client.publish(vehicle.topic("connection"),
                       json.dumps(vehicle.connection_message("OFFLINE")),
                       qos=1, retain=True)
        client.loop_stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--broker", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--robots", default="RobotCompany/carter-01",
                        help="comma-separated manufacturer/serial pairs")
    parser.add_argument("--x", type=float, default=8.0)
    parser.add_argument("--y", type=float, default=85.0)
    parser.add_argument("--speed", type=float, default=1.4)
    parser.add_argument("--battery", type=float, default=100.0)
    args = parser.parse_args()

    threads = []
    for i, pair in enumerate(p.strip() for p in args.robots.split(",") if p.strip()):
        manufacturer, _, serial = pair.partition("/")
        if not serial:
            raise SystemExit(f"--robots entries must be manufacturer/serial, got {pair!r}")
        v = Vehicle(manufacturer, serial, x=args.x + i * 3.0, y=args.y,
                    speed=args.speed, battery=args.battery)
        t = threading.Thread(target=run_vehicle,
                             args=(v, args.broker, args.port,
                                   args.username, args.password),
                             daemon=True)
        t.start()
        threads.append(t)
        print(f"started {manufacturer}/{serial} at ({v.x}, {v.y})")
    while any(t.is_alive() for t in threads):
        time.sleep(1)


if __name__ == "__main__":
    main()
