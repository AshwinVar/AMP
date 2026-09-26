"""DAY 1 CHECKPOINT: a real OPC UA server, through AMP Edge, into a real AMP machine.

This is the test the sprint's definition of done is written against, and it is
deliberately end to end rather than a stack of mocks:

    a real asyncua SERVER (not a fake client, not a recorded response)
      -> OpcUaAdapter, over a real socket, with the server's own timestamps
      -> TagMapping, from configuration, with no Python edited
      -> Normalizer, where a cumulative counter becomes production
      -> payload.build, the message AMP has always read
      -> mqtt_service.on_message, the REAL handler
      -> the machine the customer already created, updated in place

The two claims that matter most, and could not be checked any other way:

  * CHANGING A VALUE ON THE SERVER CHANGES WHAT AMP RECEIVES. Everything else
    in the pipeline could be right while the client reads a cached value.
  * NO DUPLICATE MACHINE IS CREATED. The whole identity fix exists for this, and
    the failure it prevents only appears when a gateway meets a machine that was
    typed in by hand — which is exactly what this sets up.

Run: DATABASE_URL="sqlite:///./ci.db" python edge/test_opcua_end_to_end.py
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)                       # ampedge
sys.path.insert(0, os.path.join(ROOT, "backend"))   # the AMP side, for the last hop

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_edge.db")

from ampedge import mapping as mapping_mod     # noqa: E402
from ampedge import normalizer as normalizer_mod  # noqa: E402
from ampedge import payload as payload_mod     # noqa: E402
from ampedge.adapters import base              # noqa: E402
from ampedge.adapters.opcua import OpcUaAdapter  # noqa: E402

failures = []
ENDPOINT = "opc.tcp://127.0.0.1:48401/amp/edge/"


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


async def main():
    from asyncua import Server, ua

    # ── a real OPC UA server, shaped like a small machine ───────────
    server = Server()
    await server.init()
    server.set_endpoint(ENDPOINT)
    server.set_server_name("AMP Edge test PLC")
    idx = await server.register_namespace("http://amp.local/edge/test")
    machine_obj = await server.nodes.objects.add_object(idx, "Machine")
    running = await machine_obj.add_variable(idx, "Running", False)
    parts = await machine_obj.add_variable(idx, "PartCounter", ua.Variant(1000, ua.VariantType.Int32))
    rejects = await machine_obj.add_variable(idx, "RejectCounter", ua.Variant(10, ua.VariantType.Int32))
    temperature = await machine_obj.add_variable(idx, "SpindleTemp", 41.5)
    mode = await machine_obj.add_variable(idx, "Mode", ua.Variant(2, ua.VariantType.Int32))
    for node in (running, parts, rejects, temperature, mode):
        await node.set_writable()
    await server.start()

    try:
        await run_checks(running, parts, rejects, temperature, mode)
    finally:
        await server.stop()


async def run_checks(running, parts, rejects, temperature, mode):
    from asyncua import ua
    # ── the mapping. DATA. No Python is edited to change any of this ─
    specs = [
        {"tag": "run", "address": running.nodeid.to_string(), "signal": "running",
         "datatype": "bool"},
        {"tag": "parts", "address": parts.nodeid.to_string(), "signal": "part_count",
         "datatype": "int", "counter_mode": "cumulative"},
        {"tag": "rejects", "address": rejects.nodeid.to_string(), "signal": "reject_count",
         "datatype": "int", "counter_mode": "cumulative"},
        {"tag": "temp", "address": temperature.nodeid.to_string(), "signal": "temperature",
         "datatype": "float", "unit": "degC"},
        {"tag": "mode", "address": mode.nodeid.to_string(), "signal": "machine_mode",
         "datatype": "int", "enum": {"1": "Manual", "2": "Auto", "3": "Setup"}},
    ]
    mappings = mapping_mod.validate(specs)
    addresses = [m.address for m in mappings]

    section("1. A REAL OPC UA SESSION, AGAINST A REAL SERVER")
    adapter = OpcUaAdapter({"url": ENDPOINT, "timeout": 5})
    await adapter.connect()
    check("the adapter connected", adapter.state == base.CONNECTED, adapter.state)
    check("...and the endpoint it reports carries no credentials",
          "@" not in adapter.endpoint(), adapter.endpoint())

    readings = await adapter.read(addresses)
    check("every mapped tag came back", len(readings) == len(addresses), str(len(readings)))
    check("...all of them usable", all(r.is_usable for r in readings),
          str([(r.tag, r.quality) for r in readings if not r.is_usable]))
    check("...carrying the SERVER's timestamp, not ours",
          all(r.source_time for r in readings),
          str([r.tag for r in readings if not r.source_time]))

    section("2. A BAD NODE ID FAILS ALONE, AND IS NOT A ZERO")
    mixed = await adapter.read(addresses + ["ns=9;s=NoSuchTag"])
    bad = [r for r in mixed if r.tag == "ns=9;s=NoSuchTag"][0]
    check("the unknown tag came back unusable", not bad.is_usable, repr(bad))
    check("...with NO value, rather than 0 or False", bad.value is None, repr(bad.value))
    check("...and says why", bool(bad.detail), bad.detail)
    check("...while every other tag still read", all(r.is_usable for r in mixed if r is not bad),
          "a bad tag broke the good ones")
    check("the session reports DEGRADED, not disconnected", adapter.state == base.DEGRADED,
          adapter.state)

    section("3. CHANGING A VALUE ON THE SERVER CHANGES WHAT AMP EDGE READS")
    norm = normalizer_mod.Normalizer(mappings)
    state = payload_mod.MachineState("CNC-01")

    # First pass: the counter baseline. 1000 parts were made before the gateway
    # existed, and NONE of them are this shift's.
    first = norm.absorb(await adapter.read(addresses))
    # PINNED EXPLICITLY: the first version of this check read "deltas has no
    # part_count", which passes just as well when NOTHING was mapped at all --
    # and that is precisely the bug the first run had (readings come back keyed
    # by address, the normalizer indexed by tag). An empty pipeline must never
    # be able to pass for a correct one.
    check("every reading found its mapping", not norm.rejections,
          str([(r.tag, r.reason) for r in norm.rejections]))
    check("...and produced a sample for every non-counter signal", len(first) >= 3,
          str([s_.signal for s_ in first]))
    state.absorb(first)
    check("the first counter reading is a baseline, not 1000 parts",
          state.deltas.get("part_count") is None, str(state.deltas))
    check("the run bit read False, as the server has it",
          state.latest.get("running") is False, str(state.latest.get("running")))

    # Now the machine starts and makes 7 parts, 1 of them scrap.
    await running.write_value(True)
    await parts.write_value(ua.Variant(1007, ua.VariantType.Int32))
    await rejects.write_value(ua.Variant(11, ua.VariantType.Int32))
    await temperature.write_value(48.25)
    await asyncio.sleep(0.05)

    state.absorb(norm.absorb(await adapter.read(addresses)))
    check("the changed run bit reached the gateway", state.latest.get("running") is True,
          str(state.latest.get("running")))
    check("the counter became 7 PARTS, not 1007", state.deltas.get("part_count") == 7,
          str(state.deltas.get("part_count")))
    check("...and 1 reject, not 11", state.deltas.get("reject_count") == 1,
          str(state.deltas.get("reject_count")))
    check("the enum became a word the plant uses, not the number 2",
          state.latest.get("machine_mode") == "Auto", str(state.latest.get("machine_mode")))
    check("telemetry arrived with its unit",
          state.telemetry.get("temperature", {}).get("value") == 48.25
          and state.telemetry["temperature"]["unit"] == "degC", str(state.telemetry))

    section("3b. SUBSCRIPTION: A PULSE SHORTER THAN THE POLL INTERVAL")
    # A 400ms stoppage between two 5-second polls is invisible to polling, and
    # it is exactly the kind of micro-stop that OEE exists to find. Subscribing
    # is how the fast signals stay honest without hammering the PLC.
    await adapter.subscribe([running.nodeid.to_string(), parts.nodeid.to_string()],
                            period_ms=50)
    await asyncio.sleep(0.2)
    adapter.drain_subscription()          # clear the initial value burst
    await running.write_value(False)
    await asyncio.sleep(0.15)
    await running.write_value(True)
    await asyncio.sleep(0.3)
    pushed = adapter.drain_subscription()
    check("the server pushed changes without being polled", len(pushed) >= 1,
          str([(p_.tag, p_.value) for p_ in pushed]))
    check("...carrying real values, not placeholders",
          all(p_.is_usable for p_ in pushed),
          str([(p_.tag, p_.quality) for p_ in pushed if not p_.is_usable]))
    check("...and the adapter reports that it is subscribed",
          adapter.describe().get("subscribed") is True, str(adapter.describe().get("subscribed")))
    check("draining twice does not re-deliver the same change",
          adapter.drain_subscription() == [], "a change was delivered twice")

    section("4. THE PAYLOAD AMP ALREADY UNDERSTANDS")
    body = payload_mod.build(state, machine_name="CNC-01")
    check("a message was built", body is not None, "nothing to send")
    check("it says the machine is Running", body.get("status") == "Running", str(body.get("status")))
    check("the counts balance, which is what AMP requires",
          body.get("good_count", 0) + body.get("rejected_count", 0) == body.get("total_count"),
          f"{body.get('good_count')} + {body.get('rejected_count')} != {body.get('total_count')}")
    check("...and they are this window's 7, not the counter's 1007",
          body.get("total_count") == 7, str(body.get("total_count")))
    check("no credential rode along in the payload",
          not any(k in json.dumps(body).lower() for k in ("password", "secret", "token")),
          json.dumps(body)[:160])

    await adapter.disconnect()
    check("the adapter disconnected cleanly", adapter.state == base.DISCONNECTED, adapter.state)

    # ── 4b. the commissioning command, against the live server ──────
    section("4b. `preview` — THE COMMAND THAT SAVES THE COMMISSIONING DAY")
    import argparse                      # noqa: E402
    import tempfile                      # noqa: E402
    from io import StringIO              # noqa: E402

    from ampedge import __main__ as cli  # noqa: E402
    from ampedge import config as config_mod  # noqa: E402

    os.environ["TEST_EDGE_KEY"] = "sentinel-key-do-not-print"
    cfg = {
        "amp": {"host": "broker.example", "tenant": "PILOT", "site": "plant-1"},
        "gateway": {"id": "gw-1", "key_env": "TEST_EDGE_KEY"},
        "machines": [{"name": "CNC-01", "protocol": "opcua", "poll_interval": 1.0,
                      "connection": {"url": ENDPOINT, "timeout": 5}, "tags": specs}],
    }
    path = os.path.join(tempfile.mkdtemp(), "gateway.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)

    captured, sys.stdout = sys.stdout, StringIO()
    try:
        code = await cli._preview(config_mod.validate(config_mod.load(path)),
                                  argparse.Namespace(machine=None))
        printed = sys.stdout.getvalue()
    finally:
        sys.stdout = captured

    check("preview connected to the live server and returned clean", code == 0, printed[-400:])
    check("...printing the RAW value beside the CANONICAL one, which is the whole point",
          "RAW" in printed and "CANONICAL" in printed, printed[:200])
    check("...showing the enum resolved to a word the plant uses", "'Auto'" in printed,
          printed[:600])
    check("...and telling the engineer to check it against the machine itself",
          "plausible wrong number" in printed, printed[-300:])
    check("preview prints no credential", "sentinel-key-do-not-print" not in printed,
          "the gateway key appeared in preview output")
    # THE CLOCK, before anyone streams. A real server shares our clock, so this
    # is the in-sync case; the skewed cases are driven directly in
    # test_edge_pipeline.py §8 and test_edge_health.py §9.
    check("...and reports the PLC's clock against this gateway's",
          "clock:" in printed.lower(), printed[-500:])
    check("...saying it is in step when it is, rather than staying silent",
          "within" in printed, printed[-500:])

    # ── 5. the last hop: into AMP itself ────────────────────────────
    section("5. INTO THE REAL AMP HANDLER, ONTO THE MACHINE THAT ALREADY EXISTED")
    import models            # noqa: E402
    import mqtt_service      # noqa: E402
    import tenancy           # noqa: E402
    from database import Base, SessionLocal, engine   # noqa: E402

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code="PILOT"))
    # THE TRAP: typed in by hand, so it has no site. Before the identity fix,
    # the gateway's first packet registered a SECOND CNC-01 and the customer's
    # history split in two.
    typed_in = models.Machine(tenant_code="PILOT", site="", name="CNC-01", status="Idle",
                              utilization=0, downtime="0 min")
    db.add(typed_in)
    db.commit()
    original_id = typed_in.id
    tenancy.reset_current_tenant(tok)

    class Msg:
        def __init__(self, topic, body):
            self.topic = topic
            self.payload = json.dumps(body).encode()

    topic = f"{mqtt_service.TOPIC_PREFIX}/PILOT/plant-1/machines"
    mqtt_service.on_message(None, None, Msg(topic, body))

    db.expire_all()
    tok = tenancy.set_current_tenant(None)
    rows = db.query(models.Machine).filter(models.Machine.tenant_code == "PILOT",
                                           models.Machine.name == "CNC-01").all()
    records = db.query(models.ProductionRecord).filter(
        models.ProductionRecord.tenant_code == "PILOT").all()
    tenancy.reset_current_tenant(tok)

    # ── 5b. the same message again, through the queue ───────────────
    #
    # THE JOIN BETWEEN THE TWO HALVES. edge/test_edge_pipeline.py proves the
    # buffer gives every record an id; backend/test_gateway_ingest_authentication
    # proves AMP writes one record per id. Neither proves they are the SAME id,
    # and a mismatch would look exactly like working software until a flaky link
    # doubled somebody's shift.
    #
    # Delivery is at-least-once by design: a publish that timed out may or may
    # not have arrived, so the gateway re-sends. That is what is simulated here.
    import tempfile                              # noqa: E402

    from ampedge import buffer as buffer_mod     # noqa: E402

    queue = buffer_mod.Buffer(os.path.join(tempfile.mkdtemp(), "q.db"))
    queue.put(body)
    row_id, record_id, queued_at, queued_body = queue.peek(1)[0]
    check("the queue gave the record an id", bool(record_id), "no record_id assigned")

    for _ in range(3):
        mqtt_service.on_message(None, None, Msg(
            topic, buffer_mod.stamp_for_publish(queued_body, queued_at)))
    queue.close()

    db.expire_all()
    tok = tenancy.set_current_tenant(None)
    replayed = db.query(models.ProductionRecord).filter(
        models.ProductionRecord.tenant_code == "PILOT").all()
    tenancy.reset_current_tenant(tok)
    check("re-delivering the queued message three times adds ONE record, not three",
          len(replayed) == len(records) + 1,
          f"{len(records)} -> {len(replayed)}")
    check("...and AMP stored the gateway's own id for it",
          any(r.source_record_id == record_id for r in replayed),
          str([r.source_record_id for r in replayed]))

    check("EXACTLY ONE CNC-01 exists — no duplicate was registered",
          len(rows) == 1, str([(m.id, m.site) for m in rows]))
    check("...and it is the SAME machine the customer created",
          rows and rows[0].id == original_id, f"{original_id} -> {rows[0].id if rows else None}")
    check("...now carrying the gateway's site", rows and rows[0].site == "plant-1",
          rows[0].site if rows else "none")
    check("the machine is Running in AMP, from the PLC's own run bit",
          rows and rows[0].status == "Running", rows[0].status if rows else "none")
    check("the production AMP recorded is the 7 parts made in the window",
          len(records) == 1 and records[0].total_count == 7,
          str([(r.total_count, r.good_count, r.rejected_count) for r in records]))
    check("...with the reject counted as a reject",
          records and records[0].rejected_count == 1,
          str(records[0].rejected_count) if records else "none")
    db.close()


    # ── 7. the check-amp probe is inert against the REAL handler ────
    section("7. `check-amp`'s PROBE REACHES AMP AND WRITES NOTHING")
    # check-amp publishes to the workspace's REAL topic, because proving the
    # broker will accept a publish to any OTHER topic proves nothing about the
    # one that matters. That is only safe while the probe stays unroutable, and
    # "stays" is the word that needs a test: the day somebody makes the handler
    # tolerate a nameless payload, this command starts creating junk machines on
    # customers' sites and nothing else would notice.
    from ampedge.__main__ import selftest_probe   # noqa: E402

    tok = tenancy.set_current_tenant(None)
    before = {m.__name__: db.query(m).count() for m in
              (models.Machine, models.ProductionRecord, models.MachineEvent,
               models.DowntimeLog, models.Notification)}
    tenancy.reset_current_tenant(tok)

    mqtt_service.on_message(None, None, Msg(topic, selftest_probe()))

    db.expire_all()
    tok = tenancy.set_current_tenant(None)
    after = {m.__name__: db.query(m).count() for m in
             (models.Machine, models.ProductionRecord, models.MachineEvent,
              models.DowntimeLog, models.Notification)}
    tenancy.reset_current_tenant(tok)
    check("the probe writes NOTHING to any table", after == before, f"{before} -> {after}")
    check("...in particular it does not register a machine",
          after["Machine"] == before["Machine"], f"{before['Machine']} -> {after['Machine']}")
    check("...and carries no machine name, which is WHY it is inert",
          "machine" not in selftest_probe(), str(selftest_probe()))


if __name__ == "__main__":
    asyncio.run(main())
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED — OPC UA server to AMP machine, end to end")
