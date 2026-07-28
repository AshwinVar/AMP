"""Tests for the standalone MQTT listener (mqtt_listener.py).

The standalone listener had drifted into a buggy second copy of the app-wired
handler: it called ``broadcast_live_event`` without importing it (a NameError
swallowed as an "MQTT DB ERROR" on every message, so the live feed never fired),
wrote raw un-canonicalised statuses, stored utilization unclamped, and — because
its client.connect()/loop_forever() ran at MODULE LEVEL — could not even be
imported without connecting to a broker. It now delegates to the single hardened
handler (mqtt_service.on_message) behind an ``if __name__ == "__main__"`` guard.

These lock that down:
  1. the module imports side-effect-free (no broker connect at import) and wires
     the SAME handler as the app-wired service — not a divergent copy;
  2. driving that handler applies the parity fixes the old copy lacked — a
     lowercase status is canonicalised, an out-of-range utilization is clamped,
     and the live broadcast fires (proving the NameError is gone).

Run:  python backend/test_mqtt_listener.py     (exit 0 = pass)
"""
import io
import json
from contextlib import redirect_stdout

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import mqtt_service
import mqtt_listener
from database import Base


class _Msg:
    topic = "flowmes/machines"

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()


def test_import_is_side_effect_free_and_reuses_the_hardened_handler():
    # Importing the module reached this line, so the old module-level
    # client.connect()/loop_forever() (which would hang/raise against no broker)
    # is gone. build_client() must wire the shared handler, not a second copy.
    client = mqtt_listener.build_client()
    assert client.on_message is mqtt_service.on_message
    assert client.on_connect is mqtt_service.on_connect
    print("PASS listener imports side-effect-free and reuses the hardened handler")


def test_handler_canonicalises_status_clamps_utilization_and_broadcasts():
    # Drive the exact callback the listener wires and prove the three parity fixes
    # the old standalone copy lacked, in one message:
    #   * "running" (lowercase)  -> canonical "Running"  (still appears in reports)
    #   * utilization 150        -> clamped to 100       (never stored raw)
    #   * the live broadcast FIRES (the old copy raised NameError here)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    mqtt_service.SessionLocal = sessionmaker(bind=engine)
    broadcasts = []
    mqtt_service.safe_broadcast = lambda event: broadcasts.append(event)

    handler = mqtt_listener.build_client().on_message
    with redirect_stdout(io.StringIO()):
        handler(None, None, _Msg({"machine": "PRESS-01", "status": "running",
                                  "utilization": 150}))

    db = mqtt_service.SessionLocal()
    machine = db.query(models.Machine).filter(models.Machine.name == "PRESS-01").first()
    db.close()

    assert machine is not None
    assert machine.status == "Running", machine.status          # canonicalised, not raw "running"
    assert machine.utilization == 100, machine.utilization      # clamped into [0, 100]
    assert len(broadcasts) == 1, broadcasts                     # broadcast fired (NameError gone)
    assert broadcasts[0]["event"] == "machine_update"
    assert broadcasts[0]["machine"]["status"] == "Running"
    print("PASS listener handler canonicalises status, clamps utilization, and broadcasts")


if __name__ == "__main__":
    test_import_is_side_effect_free_and_reuses_the_hardened_handler()
    test_handler_canonicalises_status_clamps_utilization_and_broadcasts()
    print("MQTT LISTENER OK: standalone path reuses the hardened, tested handler")
