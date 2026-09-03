"""Telemetry reaches the browser on the SERVER's event loop (ADR-0016).

THE DEFECT THIS PINS
--------------------
`mqtt_service.safe_broadcast` did `asyncio.run(broadcast_live_event(event))`,
but `live_ws.broadcast_live_event` is a plain `def`. So the argument was
evaluated first (doing the real work), returned None, and `asyncio.run(None)`
raised `ValueError: a coroutine was expected, got None` — swallowed and logged
as "WebSocket broadcast error" on EVERY message.

The delivery that did happen was worse than the error. `broadcast_live_event`
ran on the MQTT worker thread, where `asyncio.get_event_loop()` raises
RuntimeError, so it fell through to `asyncio.run(manager.broadcast(...))` —
a BRAND-NEW event loop. The WebSocket objects belong to uvicorn's loop, and
writing to them from a second loop is undefined behaviour: it can appear to
work, interleave frames, or fail under load. Nothing in the suite would notice,
because the delivery still "happened".

WHAT THE FIX IS
---------------
The MQTT ingest runs on a worker THREAD; the sockets live on the server's loop.
Crossing that boundary has exactly one correct primitive:
`asyncio.run_coroutine_threadsafe(coro, loop)`. So the server's loop is captured
once at startup (`live_ws.bind_loop`), and the bridge schedules onto it.

THE GUARANTEE THAT MUST SURVIVE
-------------------------------
A broken or absent WebSocket layer costs the live view, never the database
write. test_mqtt_resilience.py pins that from the ingest side; the checks here
must not weaken it — a bridge that raises into `on_message` would roll back a
whole stoppage.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_live_broadcast_bridge.py
"""
import asyncio
import threading
import time

import live_ws
import mqtt_service

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class Recorder:
    """Stands in for ConnectionManager, recording WHICH loop delivered."""

    def __init__(self):
        self.payloads = []
        self.loops = []

    async def broadcast(self, payload):
        self.payloads.append(payload)
        self.loops.append(asyncio.get_running_loop())


def run_server_loop():
    """A loop on its own thread, standing in for uvicorn's."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    ready.wait(5)
    return loop, t


def main():
    print("=" * 74)
    print("1. THE DEFECT — a worker thread must not raise, and must deliver")
    print("=" * 74)

    rec = Recorder()
    original_manager = live_ws.manager
    live_ws.manager = rec
    loop, thread = run_server_loop()
    live_ws.bind_loop(loop)

    errors = []

    def worker():
        try:
            # EXACTLY what the MQTT thread does.
            mqtt_service.safe_broadcast({"tenant_code": "T1", "event": "machine_update"})
        except BaseException as e:                      # noqa: BLE001 - reporting
            errors.append(repr(e))

    w = threading.Thread(target=worker)
    w.start()
    w.join(5)
    time.sleep(0.3)                                     # let the loop drain

    check("the worker thread raises nothing", not errors, str(errors))
    check("the payload was delivered", len(rec.payloads) == 1, str(rec.payloads))
    check("...exactly once, not twice",
          len(rec.payloads) == 1, f"{len(rec.payloads)} deliveries")

    # The half that the old code got wrong even when it 'worked'.
    check("DELIVERED ON THE SERVER'S LOOP, not a new one",
          rec.loops and rec.loops[0] is loop,
          f"delivered on {rec.loops[0] if rec.loops else None}, server loop is {loop}")

    print()
    print("=" * 74)
    print("2. NO ValueError FROM THE SYNC/ASYNC MISMATCH")
    print("=" * 74)
    # The original symptom, asserted directly: safe_broadcast must never hand
    # asyncio.run a non-coroutine. Calling the bridge must not return a
    # coroutine that nobody awaits either.
    result = live_ws.broadcast_live_event({"tenant_code": "T1", "event": "x"})
    time.sleep(0.2)
    check("the bridge returns nothing awaitable", result is None, repr(result))
    check("...and delivered that one too", len(rec.payloads) == 2, str(len(rec.payloads)))

    print()
    print("=" * 74)
    print("3. THE GUARANTEE: a broken WS layer never breaks the ingest")
    print("=" * 74)

    class Broken:
        async def broadcast(self, payload):
            raise OSError("socket gone")

    live_ws.manager = Broken()
    raised = None
    try:
        mqtt_service.safe_broadcast({"tenant_code": "T1", "event": "boom"})
        time.sleep(0.2)
    except BaseException as e:                          # noqa: BLE001
        raised = repr(e)
    check("a failing broadcast does not raise into the caller", raised is None, str(raised))

    print()
    print("=" * 74)
    print("4. NO LOOP BOUND — still works (tests, CLI, simulator)")
    print("=" * 74)
    live_ws.manager = rec
    live_ws.bind_loop(None)
    before = len(rec.payloads)
    raised = None
    try:
        mqtt_service.safe_broadcast({"tenant_code": "T1", "event": "no-loop"})
    except BaseException as e:                          # noqa: BLE001
        raised = repr(e)
    check("no bound loop still delivers", len(rec.payloads) == before + 1,
          f"{before} -> {len(rec.payloads)}")
    check("...and still raises nothing", raised is None, str(raised))

    print()
    print("=" * 74)
    print("5. A DEAD LOOP IS NOT A CRASH")
    print("=" * 74)
    # A loop that has been closed (shutdown, reload) must degrade to a logged
    # miss, not an exception into the ingest path.
    loop.call_soon_threadsafe(loop.stop)
    time.sleep(0.3)
    loop.close()
    live_ws.bind_loop(loop)
    raised = None
    try:
        mqtt_service.safe_broadcast({"tenant_code": "T1", "event": "dead-loop"})
    except BaseException as e:                          # noqa: BLE001
        raised = repr(e)
    check("a closed server loop does not raise into the ingest", raised is None, str(raised))

    live_ws.bind_loop(None)
    live_ws.manager = original_manager

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
