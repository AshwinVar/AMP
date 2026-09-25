"""DAY 2 CHECKPOINT: Modbus TCP through the SAME pipeline, into the same AMP machine.

The point of this test is not that Modbus works. It is that Modbus and OPC UA
arrive at AMP as the SAME THING. Two protocols with nothing in common — one
typed and self-describing, one a numbered 16-bit hole in a device's memory —
must produce identical canonical signals, go through the identical mapper,
normalizer and payload builder, and update the identical machine.

If that is not true, then every read-model, every OEE calculation and every
Copilot answer has to know which protocol a plant happens to use, and adding a
third protocol later means touching all of them.

WHAT IT PROVES AGAINST A REAL pymodbus SERVER:

  * coils, discrete inputs, input registers and holding registers all read
  * a 32-bit counter spanning two registers, with the word order declared
  * scale and offset turning a raw 485 into 48.5 degrees
  * an unreadable register refused — NOT read as the zero the wire offers
  * the 4xxxx documentation convention accepted as written
  * the same canonical payload shape the OPC UA checkpoint produced

Run: DATABASE_URL="sqlite:///./ci_edge.db" python edge/test_modbus_end_to_end.py
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci_edge.db")

from ampedge import mapping as mapping_mod        # noqa: E402
from ampedge import normalizer as normalizer_mod  # noqa: E402
from ampedge import payload as payload_mod        # noqa: E402
from ampedge.adapters import base                 # noqa: E402
from ampedge.adapters.modbus import ModbusAdapter, resolve_address  # noqa: E402

failures = []
HOST, PORT = "127.0.0.1", 45021


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ── the address convention, before any socket is opened ─────────────
section("1. THE 4xxxx CONVENTION IS ACCEPTED AS THE PLANT WRITES IT")
addr, kind, how = resolve_address(40001)
check("40001 is holding register 0", (addr, kind) == (0, "holding"), f"{addr} {kind}")
addr, kind, _ = resolve_address(30010)
check("30010 is input register 9", (addr, kind) == (9, "input"), f"{addr} {kind}")
addr, kind, _ = resolve_address(10005)
check("10005 is discrete input 4", (addr, kind) == (4, "discrete"), f"{addr} {kind}")
addr, kind, _ = resolve_address(7, "holding")
check("a plain 7 stays 7", (addr, kind) == (7, "holding"), f"{addr} {kind}")
clash = None
try:
    resolve_address(40001, "coil")
except base.AdapterError as e:
    clash = str(e)
check("a documentation address that contradicts register_type is refused, not guessed",
      clash is not None and "convention" in clash, str(clash))


async def main():
    from pymodbus.client import AsyncModbusTcpClient
    from pymodbus.datastore import (ModbusDeviceContext, ModbusSequentialDataBlock,
                                    ModbusServerContext)
    from pymodbus.server import ModbusTcpServer

    # A real Modbus device: coil 0 = running, discrete 0 = fault,
    # holding 0..1 = a 32-bit part counter, holding 2 = rejects,
    # input 0 = temperature in tenths.
    device = ModbusDeviceContext(
        co=ModbusSequentialDataBlock(1, [0] * 8),
        di=ModbusSequentialDataBlock(1, [0] * 8),
        hr=ModbusSequentialDataBlock(1, [0] * 16),
        ir=ModbusSequentialDataBlock(1, [0] * 16),
    )
    context = ModbusServerContext(devices=device, single=True)
    server = ModbusTcpServer(context, address=(HOST, PORT))
    serving = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.4)

    # Values are written OVER THE WIRE rather than into the server's datastore
    # object: it is the same path a PLC program takes, and it means this test
    # does not depend on pymodbus's internal storage API (which changed under
    # this test once already).
    writer = AsyncModbusTcpClient(HOST, port=PORT, timeout=3)
    await writer.connect()
    try:
        await run_checks(writer)
    finally:
        writer.close()
        await server.shutdown()
        serving.cancel()


async def poke(writer, kind, address, values):
    """Write to the device, as a PLC's program would."""
    if kind == "c":
        for i, value in enumerate(values):
            await writer.write_coil(address + i, bool(value), device_id=1)
    else:
        await writer.write_registers(address, [int(v) for v in values], device_id=1)


async def run_checks(writer):
    specs = [
        {"tag": "run", "address": 0, "register_type": "coil", "signal": "running",
         "datatype": "bool"},
        {"tag": "fault", "address": 10001, "signal": "fault_active", "datatype": "bool"},
        {"tag": "parts", "address": 40001, "signal": "part_count", "datatype": "int",
         "modbus_type": "uint32", "word_order": "big", "counter_mode": "cumulative"},
        {"tag": "rejects", "address": 40003, "signal": "reject_count", "datatype": "int",
         "modbus_type": "uint16", "counter_mode": "cumulative"},
        # The raw register holds tenths of a degree. `scale` is what stops a
        # plant seeing a spindle at 485 degrees. On a HOLDING register because
        # input registers are read-only on the wire -- this test writes the way
        # a PLC program does, so it can only set what a PLC could set remotely.
        {"tag": "temp", "address": 40005, "signal": "temperature", "datatype": "float",
         "modbus_type": "uint16", "scale": 0.1, "unit": "degC"},
        # An INPUT register, to prove that register type reads at all. Its value
        # is a genuine 0 from the device, which is a different thing from the
        # refusal in section 4 -- and telling those two apart is the whole point.
        {"tag": "line_pressure", "address": 30001, "signal": "pressure", "datatype": "float",
         "modbus_type": "uint16", "scale": 0.01, "unit": "bar"},
    ]
    mappings = mapping_mod.validate(specs)
    entries = [m.raw for m in mappings]

    section("2. A REAL MODBUS TCP SESSION")
    adapter = ModbusAdapter({"host": HOST, "port": PORT, "unit_id": 1, "timeout": 3})
    await adapter.connect()
    check("the adapter connected", adapter.state == base.CONNECTED, adapter.state)

    await poke(writer, "c", 0, [True])
    await poke(writer, "h", 0, [0, 1000])   # uint32, big word order -> 1000
    await poke(writer, "h", 2, [10])
    await poke(writer, "h", 4, [485])       # 48.5 degC once scaled

    readings = await adapter.read(entries)
    by_tag = {r.tag: r for r in readings}
    check("every register read", all(r.is_usable for r in readings),
          str([(r.tag, r.quality, r.detail) for r in readings if not r.is_usable]))
    check("a coil came back as a real bool", by_tag["0"].value is True, repr(by_tag["0"].value))
    check("the 32-bit counter spanned two registers correctly",
          by_tag["40001"].value == 1000, repr(by_tag["40001"].value))
    check("Modbus timestamps are the GATEWAY's, and say so",
          all(not r.source_time for r in readings), "a Modbus reading claimed a device clock")

    section("3. THE SAME CANONICAL PIPELINE AS OPC UA")
    norm = normalizer_mod.Normalizer(mappings)
    state = payload_mod.MachineState("PRESS-02")
    first = norm.absorb(readings)
    check("every reading found its mapping", not norm.rejections,
          str([(r.tag, r.reason) for r in norm.rejections]))
    state.absorb(first)
    check("the run bit became the canonical `running`", state.latest.get("running") is True,
          str(state.latest))
    check("the raw 485 became 48.5 degC through scale, not a 485 degree spindle",
          abs(state.telemetry["temperature"]["value"] - 48.5) < 1e-9,
          str(state.telemetry.get("temperature")))
    check("an input register read too, as a REAL zero from the device",
          state.telemetry.get("pressure", {}).get("value") == 0.0,
          str(state.telemetry.get("pressure")))
    check("...and a real zero is not the same as the refusal in section 4",
          all(r.is_usable for r in readings if r.tag == "30001"),
          "the input register was refused rather than read")
    check("the counter's first reading is a baseline", state.deltas.get("part_count") is None,
          str(state.deltas))

    # 12 more parts, 2 of them scrap.
    await poke(writer, "h", 0, [0, 1012])
    await poke(writer, "h", 2, [12])
    state.absorb(norm.absorb(await adapter.read(entries)))
    check("the 32-bit counter produced 12 parts, not 1012",
          state.deltas.get("part_count") == 12, str(state.deltas.get("part_count")))
    check("...and 2 rejects", state.deltas.get("reject_count") == 2,
          str(state.deltas.get("reject_count")))

    section("4. AN UNREADABLE REGISTER IS REFUSED, NOT READ AS ZERO")
    # Register 500 is outside the server's 16-word block: a real device answers
    # with an exception, and the wire would happily be read as 0.
    out_of_range = await adapter.read([{"tag": "ghost", "address": 40501,
                                        "modbus_type": "uint16"}])
    ghost = out_of_range[0]
    check("the out-of-range register is unusable", not ghost.is_usable, repr(ghost))
    check("...with NO value at all", ghost.value is None, repr(ghost.value))
    check("...and names the device's own refusal", "refused" in ghost.detail, ghost.detail)
    check("the session is DEGRADED, not disconnected", adapter.state == base.DEGRADED,
          adapter.state)

    section("5. A BAD WORD ORDER IS A WRONG NUMBER, WHICH IS WHY IT IS DECLARED")
    swapped = mapping_mod.validate([
        {"tag": "parts_le", "address": 40001, "signal": "part_count", "datatype": "int",
         "modbus_type": "uint32", "word_order": "little", "counter_mode": "cumulative"}])
    wrong = await adapter.read([m.raw for m in swapped])
    check("the same registers read little-endian give a different number entirely",
          wrong[0].value != 1012, f"{wrong[0].value} == 1012, so the test proves nothing")
    check("...and it is the plausible-looking kind of wrong, not an error",
          wrong[0].is_usable, "it errored, so nobody would be fooled")

    await adapter.disconnect()

    section("6. INTO AMP, ONTO A MACHINE THAT ALREADY EXISTS")
    import models            # noqa: E402
    import mqtt_service      # noqa: E402
    import tenancy           # noqa: E402
    from database import Base, SessionLocal, engine   # noqa: E402

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tok = tenancy.set_current_tenant(None)
    db.add(models.TenantConfig(tenant_code="PILOT"))
    existing = models.Machine(tenant_code="PILOT", site="", name="PRESS-02", status="Idle",
                              utilization=0, downtime="0 min")
    db.add(existing)
    db.commit()
    original_id = existing.id
    tenancy.reset_current_tenant(tok)

    body = payload_mod.build(state, machine_name="PRESS-02")
    check("a payload was built from Modbus data", body is not None, "nothing to send")
    check("...with the same shape the OPC UA path produced",
          body.get("status") == "Running" and body.get("total_count") == 12,
          json.dumps(body)[:200])

    class Msg:
        def __init__(self, topic, b):
            self.topic = topic
            self.payload = json.dumps(b).encode()

    mqtt_service.on_message(None, None, Msg(
        f"{mqtt_service.TOPIC_PREFIX}/PILOT/plant-1/machines", body))

    db.expire_all()
    tok = tenancy.set_current_tenant(None)
    rows = db.query(models.Machine).filter(models.Machine.tenant_code == "PILOT",
                                           models.Machine.name == "PRESS-02").all()
    records = db.query(models.ProductionRecord).filter(
        models.ProductionRecord.tenant_code == "PILOT").all()
    tenancy.reset_current_tenant(tok)
    check("exactly one PRESS-02 — no duplicate from the gateway", len(rows) == 1,
          str([(m.id, m.site) for m in rows]))
    check("...the one the customer created", rows and rows[0].id == original_id,
          f"{original_id} -> {rows[0].id if rows else None}")
    check("...now Running, from a coil", rows and rows[0].status == "Running",
          rows[0].status if rows else "none")
    check("the production record is the 12 parts of this window",
          len(records) == 1 and records[0].total_count == 12,
          str([(r.total_count, r.good_count, r.rejected_count) for r in records]))
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
    print()
    print("=" * 74)
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED - Modbus TCP to AMP machine, same pipeline as OPC UA")
