"""Standalone MQTT listener — a foreground runner for the ingest handler.

This is the dev / standalone entry point (`python mqtt_listener.py`) for the MQTT
ingest path. It now reuses the SAME hardened, tested message handler as the
app-wired service (``mqtt_service.on_message``, which main.py starts in-process at
boot) instead of keeping a second, divergent copy.

The previous standalone copy had silently drifted out of parity with the wired
handler and carried real bugs the wired path had already fixed:

  * it called ``broadcast_live_event({...})`` WITHOUT importing it, so every
    message raised ``NameError`` — swallowed by the broad ``except`` as a
    misleading "MQTT DB ERROR" — while the live WebSocket feed never fired once
    (the whole point of the listener). The DB write had already committed, so it
    looked like an intermittent DB fault rather than a missing import;
  * it wrote the raw device status straight onto the machine (no
    ``normalize_machine_status``), so a gateway publishing "running"/"RUNNING"
    dropped the machine from every status-based rollup and — because the
    breakdown check was case-sensitive — skipped its DowntimeLog;
  * it stored utilization unclamped and coerced production counts with a bare
    ``int()``, so a glitching sensor could persist a >100% / negative utilization
    or a negative good_count (dragging pooled OEE below zero), and a non-numeric
    value aborted the whole message.

Rather than re-apply each guard a second time (a second implementation is exactly
what let this copy rot), the listener delegates to ``mqtt_service`` — one source
of truth for MQTT ingest. The runtime is guarded under ``__main__`` so importing
the module is side-effect-free (the old copy connected to a broker and looped at
import time, which also made it impossible to unit-test).
"""
import paho.mqtt.client as mqtt

import mqtt_service


def build_client():
    """Wire an MQTT client to the shared, hardened ingest callbacks.

    ``on_connect`` subscribes to the configured topic and ``on_message`` is the
    same handler the app-wired service uses (canonicalises status, clamps
    utilization, rejects negative/non-numeric counts, and broadcasts the live
    event), so the standalone and in-process paths can never diverge again.
    """
    client = mqtt.Client()
    client.on_connect = mqtt_service.on_connect
    client.on_message = mqtt_service.on_message
    return client


def main():
    client = build_client()
    print("CONNECTING TO MQTT BROKER...")
    client.connect(mqtt_service.MQTT_BROKER, mqtt_service.MQTT_PORT, 60)
    print("AMP MQTT LISTENER RUNNING...")
    client.loop_forever()


if __name__ == "__main__":
    main()
