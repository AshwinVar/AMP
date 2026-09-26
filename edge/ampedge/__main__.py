"""`python -m ampedge` — the commissioning engineer's whole toolkit.

THIS IS THE COMMISSIONING EXPERIENCE. Not a screen in AMP: a command on the
plant PC, because that is where the problem is and where the person is standing.
The order of the subcommands is the order of the job, and each one answers the
question that must be answered before the next is worth attempting:

    validate     is the config even coherent?          (no network needed)
    check-amp    can we reach AMP, and may we publish?  (no PLC needed)
    browse       what does this PLC actually have?     (OPC UA only)
    preview      what do MY tags read, right now?      (the decisive one)
    run          stream it
    diagnose     it is not working and I need help

The two halves are testable SEPARATELY, and that is the point of `check-amp`.
Until it existed the first test of the broker hostname, the TLS setting or the
credentials was `run` -- with the PLC side working perfectly and no way to tell
a wrong hostname from a firewall from a bad password.

`preview` is the one that saves the pilot. It prints the RAW value beside the
CANONICAL value for every mapped tag, so a controls engineer can look at the
machine, look at the screen, and say "no, that is the wrong register" in ten
seconds. Every other way of finding that out costs a day.

NOTHING HERE PRINTS A CREDENTIAL. Not in output, not in the diagnostic bundle,
not in an error.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time

from . import config as config_mod
from . import health as health_mod
from . import normalizer as normalizer_mod
from . import publisher as publisher_mod
from . import runner as runner_mod
from .adapters import base


def selftest_probe(now=None):
    """The routing probe `check-amp` publishes. Inert by construction.

    It carries NO `machine`, which is what makes it safe: AMP's handler calls
    mqtt_identity.machine_identifier() before it touches the database, that
    raises RouteError, and the message is dropped with a logged reason having
    written nothing -- no machine created, no reading recorded.

    A function rather than a literal inside the command, so the test that proves
    it is inert against the REAL handler uses the same bytes the command sends.
    A probe that drifted from its own proof would be a command that quietly
    started creating junk machines on a customer's site.
    """
    return {"amp_edge_selftest": True, "sent_at": round(time.time() if now is None else now, 3)}


def _load(path):
    try:
        return config_mod.validate(config_mod.load(path))
    except config_mod.ConfigError as e:
        print("This configuration will not start:\n")
        for problem in str(e).split("; "):
            print(f"  * {problem}")
        print("\nNothing was connected to. Fix these and run `validate` again.")
        sys.exit(2)


def cmd_validate(args):
    resolved = _load(args.config)
    print(f"OK — {args.config} is valid.\n")
    print(f"  workspace   {resolved['amp']['tenant']} / site {resolved['amp']['site']}")
    print(f"  publishes   {resolved['amp'].get('host')} "
          f"topic flowmes/{resolved['amp']['tenant']}/{resolved['amp']['site']}/machines")
    signed = bool(resolved.get("gateway", {}).get("id"))
    print(f"  signed      {'yes, as ' + resolved['gateway']['id'] if signed else 'NO — see the pilot guide'}")
    print()
    for machine in resolved["machines"]:
        print(f"  {machine['name']}  ({machine['protocol']}, every {machine['poll_interval']}s)")
        for m in machine["mappings"]:
            print(f"      {str(m.address):<28} -> {m.signal}"
                  + (f"  [{m.unit}]" if m.unit else "")
                  + (f"  ({m.counter_mode})" if m.counter_mode else ""))
    print("\nNOTHING WAS CONNECTED TO. `preview` reads the PLC; this only read the file.")
    return 0


async def _connect(machine):
    adapter = runner_mod.build_adapter(machine)
    await adapter.connect()
    return adapter


def cmd_preview(args):
    resolved = _load(args.config)
    return asyncio.run(_preview(resolved, args))


async def _preview(resolved, args):
    bad = 0
    for machine in resolved["machines"]:
        if args.machine and machine["name"] != args.machine:
            continue
        print(f"\n{machine['name']}  ({machine['protocol']} {machine['connection'].get('url') or machine['connection'].get('host')})")
        print("-" * 96)
        try:
            adapter = await _connect(machine)
        except base.AdapterError as e:
            print(f"  COULD NOT CONNECT: {e}")
            bad += 1
            continue
        try:
            entries = [m.raw for m in machine["mappings"]]
            readings = await adapter.read(entries)
            norm = normalizer_mod.Normalizer(machine["mappings"])
            samples = {s.signal: s for s in norm.absorb(readings)}
            rejected = {r.tag: r.reason for r in norm.rejections}
            print(f"  {'ADDRESS':<28} {'RAW':<16} {'SIGNAL':<16} {'CANONICAL':<18} NOTE")
            for m in machine["mappings"]:
                reading = next((r for r in readings if str(r.tag) == str(m.address)), None)
                raw = (repr(reading.value) if reading and reading.is_usable else "--")
                sample = samples.get(m.signal)
                # A counter's first reading is a baseline and produces no
                # sample. Saying "--" there without explaining it looks exactly
                # like a broken tag, which is the one thing preview must not do.
                if sample is not None:
                    canonical = repr(sample.value)
                    note = sample.note or ""
                elif m.signal in [s.signal for s in norm.rejections if s.signal]:
                    canonical = "REFUSED"
                    note = rejected.get(str(m.address), "")
                elif m.counter_mode:
                    canonical = "(baseline)"
                    note = "first reading of a counter; the next poll shows the increment"
                else:
                    canonical = "--"
                    note = rejected.get(str(m.address), "")
                if canonical in ("REFUSED", "--"):
                    bad += 1
                print(f"  {str(m.address):<28} {raw:<16} {m.signal:<16} {canonical:<18} {note}")
            # THE CLOCK, said out loud before anyone starts streaming. One
            # wrong clock produces two failures that look unrelated: readings
            # more than a minute ahead are refused by the normalizer, and a
            # host more than five minutes out cannot sign anything AMP will
            # accept. Neither error mentions a clock, and both present as
            # "connected, nothing arriving".
            if norm.clock_skew is not None:
                skew = norm.clock_skew
                if abs(skew) <= 5:
                    print(f"  clock: the PLC is within {abs(skew):.0f}s of this gateway.")
                else:
                    print(f"  CLOCK: the PLC's clock is {abs(skew):.0f}s "
                          f"{'AHEAD OF' if skew > 0 else 'BEHIND'} this gateway's.")
                    if skew > normalizer_mod.FUTURE_TOLERANCE_S:
                        bad += 1
                        print(f"         More than {int(normalizer_mod.FUTURE_TOLERANCE_S)}s "
                              f"ahead: every reading will be REFUSED as a future timestamp. "
                              f"Fix the clock on the PLC or on this PC before streaming.")
                    else:
                        print("         Not fatal on its own, but check NTP on both: a "
                              "gateway more than 5 minutes out cannot sign messages AMP "
                              "will accept, and that fault looks nothing like this one.")
            else:
                print("  clock: this protocol reports no timestamp of its own, so the "
                      "readings are stamped by THIS PC. Check its clock is right — a "
                      "gateway more than 5 minutes out cannot authenticate to AMP.")
        finally:
            await adapter.disconnect()
    print()
    if bad:
        print(f"{bad} tag(s) did not produce a value. Compare the RAW column against what the "
              f"machine is actually doing: a tag that reads but produces no canonical value is a "
              f"mapping problem, and a tag with no RAW value is an address problem.")
        return 1
    print("Every mapped tag produced a value. Check the CANONICAL column against the machine "
          "itself before starting the stream — a plausible wrong number is the failure this "
          "step exists to catch.")
    return 0


def cmd_browse(args):
    resolved = _load(args.config)
    return asyncio.run(_browse(resolved, args))


async def _browse(resolved, args):
    for machine in resolved["machines"]:
        if args.machine and machine["name"] != args.machine:
            continue
        if machine["protocol"] != "opcua":
            print(f"{machine['name']}: {machine['protocol']} cannot describe itself. A Modbus "
                  f"register is a number with no name — you need the register map from the "
                  f"machine builder.")
            continue
        adapter = await _connect(machine)
        try:
            found = await adapter.browse(args.root)
            print(f"\n{machine['name']}: {len(found)} readable variable(s)")
            for entry in found:
                print(f"  {entry['node']:<40} {entry['name']:<28} {entry['datatype'] or ''}")
            if len(found) >= 500:
                print("\n  (stopped at 500 — pass --root to browse a subtree)")
        finally:
            await adapter.disconnect()
    return 0


def cmd_check_amp(args):
    """Prove the CLOUD half works, before a single reading is read.

    THE GAP THIS FILLS. `validate` connects to nothing, `browse` and `preview`
    connect to the PLC. So the first time anybody discovered that the broker
    hostname was wrong, or TLS was refused, or the credentials were for a
    different site, was when they ran `run` and watched nothing arrive -- with
    the PLC side working perfectly and no way to tell the two apart.

    It answers the four questions in the order they fail, because a single
    "connection failed" is worth almost nothing on a plant network:

        DNS     does the name resolve at all
        TCP     is anything listening, or is a firewall eating it
        MQTT    does the broker accept this client and these credentials
        ACL     may this gateway actually PUBLISH to its own topic

    The last one matters more than it sounds: a broker can accept a connection
    and then silently refuse the publish, which looks exactly like a working
    gateway sending into a void.
    """
    import socket

    resolved = _load(args.config)
    amp = dict(resolved["amp"])
    gateway = resolved.get("gateway") or {}
    if gateway.get("id"):
        amp["gateway_id"] = gateway["id"]
        amp["gateway_key"] = os.environ.get(str(gateway.get("key_env") or ""), "")

    pub = publisher_mod.Publisher(amp)
    print(f"broker   {pub.host}:{pub.port}  TLS {'on' if pub.tls else 'OFF'}")
    print(f"topic    {pub.topic()}")
    print(f"signing  {'as ' + pub.gateway_id if pub.gateway_id and pub._key else 'NOT SIGNED'}")
    print()

    # 1. DNS, on its own, so a typo in the hostname does not present as a
    #    firewall problem.
    try:
        socket.getaddrinfo(pub.host, pub.port)
        print(f"  OK    {pub.host} resolves")
    except socket.gaierror as e:
        print(f"  FAIL  {pub.host} does not resolve ({e.strerror or e}). Check the hostname, "
              f"and whether this PC has DNS at all — many plant networks do not.")
        return 1

    # 2. TCP, on its own, so "nothing is listening" (refused, immediate) is
    #    distinguishable from "a firewall is dropping this" (timeout).
    try:
        with socket.create_connection((pub.host, pub.port), timeout=10):
            print(f"  OK    {pub.host}:{pub.port} accepts connections")
    except socket.timeout:
        print(f"  FAIL  {pub.host}:{pub.port} did not answer within 10s. A silent timeout is "
              f"usually a firewall DROPPING the packets rather than refusing them — ask IT "
              f"whether outbound {pub.port} is permitted from this PC.")
        return 1
    except OSError as e:
        print(f"  FAIL  {pub.host}:{pub.port} refused the connection ({e.strerror or e}). "
              f"Something answered, so the route is fine and the port is probably wrong.")
        return 1

    # 3. The MQTT handshake, which is where credentials and TLS actually fail.
    try:
        pub.connect(timeout=20)
        print("  OK    the broker accepted this client and its credentials")
    except base.AdapterError as e:
        print(f"  FAIL  {e}")
        if pub.tls:
            print("        TLS is ON. If the broker is plain MQTT, set `tls: false`; if it "
                  "uses a private CA, this PC has to trust it.")
        else:
            print("        TLS is OFF. If the broker requires it, set `tls: true` and use "
                  "port 8883.")
        return 1

    # 4. Publish permission. A ROUTING PROBE: it carries no `machine`, so AMP
    #    parses it, refuses it as unroutable and writes NOTHING -- no machine is
    #    created, no reading recorded. What it proves is that the broker let
    #    this gateway publish to this exact topic, which an ACL can refuse long
    #    after it has accepted the connection.
    published = True
    if not args.no_publish:
        published = pub.publish(selftest_probe())
        if published:
            print("  OK    the broker accepted a publish to that topic")
        else:
            print(f"  FAIL  connected, but the broker would not accept a publish to "
                  f"{pub.topic()}. That is an ACL: this gateway's credentials are valid and "
                  f"are not permitted to write to its own workspace's topic.")
    pub.disconnect()

    print()
    if not published:
        return 1
    print("The cloud half is reachable and writable.")
    print()
    print("WHAT THIS DOES NOT PROVE: that AMP ACCEPTED the message. A signature is checked")
    print("by AMP, not by the broker, so a wrong or revoked gateway key looks exactly like")
    print("success from here. AMP raises a notification in the workspace when it refuses a")
    print("gateway — if readings do not appear after `run`, look there first.")
    if not args.no_publish:
        print()
        print("AMP's log will show one 'REJECTED (unroutable)' line from the probe above.")
        print("That is this command working, not a fault.")
    return 0


def cmd_run(args):
    resolved = _load(args.config)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    gateway = runner_mod.Gateway(resolved)
    return asyncio.run(_run(gateway, args))


async def _run(gateway, args):
    async def heartbeat():
        while True:
            await asyncio.sleep(args.health_every)
            for line in health_mod.lines(gateway.health()):
                print(line)
    beat = asyncio.create_task(heartbeat())
    try:
        await gateway.run()
    except KeyboardInterrupt:
        pass
    finally:
        beat.cancel()
        await gateway.stop()
    return 0


def cmd_diagnose(args):
    """Everything a support engineer needs and nothing they must not have."""
    resolved = _load(args.config)
    bundle = {
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "config": config_mod.redact(resolved),
        "environment": sorted(k for k in os.environ
                              if k.startswith(("AMP_", "PLC_")) ),
    }
    # NAMES of the environment variables that are set, never values — knowing
    # AMP_GATEWAY_KEY exists is useful; knowing what it is, is a breach.
    bundle["environment_note"] = ("names only; no values are included in this bundle")
    path = args.out or "amp-edge-diagnostics.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2, default=str)
    print(f"Wrote {path}. It contains no passwords, keys or tokens — the configuration is "
          f"redacted and only the NAMES of environment variables are listed. Check it before "
          f"sending if your site requires it.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ampedge",
        description="AMP Edge Gateway — read a PLC, send it to AMP.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="check the config; connects to nothing")
    p.add_argument("config")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("browse", help="list what an OPC UA server actually has")
    p.add_argument("config")
    p.add_argument("--machine")
    p.add_argument("--root", help="a node id to browse under")
    p.set_defaults(func=cmd_browse)

    p = sub.add_parser("preview", help="read every mapped tag once: raw beside canonical")
    p.add_argument("config")
    p.add_argument("--machine")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("check-amp", help="test the AMP/broker connection without streaming")
    p.add_argument("config")
    p.add_argument("--no-publish", action="store_true",
                   help="connect only; skip the routing probe (for a broker whose ACL "
                        "permits nothing but live telemetry)")
    p.set_defaults(func=cmd_check_amp)

    p = sub.add_parser("run", help="stream to AMP")
    p.add_argument("config")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--health-every", type=float, default=60.0)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("diagnose", help="write a redacted bundle for a support ticket")
    p.add_argument("config")
    p.add_argument("--out")
    p.set_defaults(func=cmd_diagnose)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
