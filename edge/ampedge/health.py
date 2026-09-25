"""One screen that tells a commissioning engineer WHICH SIDE IS BROKEN.

THE FAILURE THIS PREVENTS is a phone call. Nothing is arriving in AMP. The
engineer is standing at a plant PC with a laptop bag and forty minutes before
the line restarts, and the question is whether to look at the PLC, the tag list,
the firewall, the broker credentials, or AMP itself. Without this, the answer is
"open a ticket", and the pilot loses a day per round trip.

There are exactly five places it can be broken, and this report separates them:

    EDGE     is the gateway process even running
    PLC      can it reach the controller at all
    TAGS     can it read the addresses it was given
    CLOUD    can it reach AMP
    BUFFER   is it holding data it has not managed to send

`verdict` names the first one that is broken, in that order, because that is the
order they must be fixed in: a tag list cannot be debugged through a PLC that is
unreachable, and an empty AMP dashboard means nothing while the queue is full.

WHAT IT REFUSES TO SAY. "Healthy" when it has never read anything. A gateway
that started two seconds ago and has read nothing is STARTING, not GOOD — the
same rule as AMP's read-models, where absence of data is never presented as a
good result.
"""
import time

from .adapters import base

# Verdicts, most-broken first. The order IS the diagnosis.
NO_PLC = "PLC_UNREACHABLE"
BAD_TAGS = "TAGS_FAILING"
NO_CLOUD = "AMP_UNREACHABLE"
BACKLOG = "BACKLOG_GROWING"
STARTING = "STARTING"
GOOD = "STREAMING"

# A gateway that has not managed a read in this long is not merely slow.
READ_SILENCE_S = 60.0
# A queue older than this is not a blip: something has been wrong for a while.
BACKLOG_AGE_S = 300.0


def report(*, started_at, adapters, publisher, buffers, normalizers=None, now=None) -> dict:
    """Assemble the whole picture. Pure — takes state, returns a dict, prints nothing.

    `adapters` and `buffers` are keyed by machine name so the report says WHICH
    machine is silent. One broken machine in a cell of twelve is the common
    case, and a single global "PLC: connected" would hide it.
    """
    now = time.time() if now is None else now
    normalizers = normalizers or {}

    machines = {}
    for name, adapter in (adapters or {}).items():
        described = adapter.describe() if adapter is not None else {
            "protocol": None, "state": base.DISCONNECTED, "last_read_at": None,
            "endpoint": "", "last_error": "never started"}
        last_read = described.get("last_read_at")
        norm = normalizers.get(name)
        machines[name] = {
            "protocol": described.get("protocol"),
            "plc": described.get("state"),
            "endpoint": described.get("endpoint"),
            "last_read_at": last_read,
            # None, not 0: never having read is not "0 seconds ago".
            "since_last_read_s": round(now - last_read, 1) if last_read else None,
            "bad_tags": described.get("bad_tags") or [],
            "last_error": described.get("last_error") or "",
            "signals_seen": sorted(norm.freshness(now).keys()) if norm else [],
            "counter_notes": list(norm.counter_notes)[-5:] if norm else [],
        }

    queued = 0
    oldest_age = None
    dropped = 0
    for name, buf in (buffers or {}).items():
        stats = buf.stats(now=now)
        queued += stats["queued"]
        dropped += stats["dropped"]
        if stats["oldest_age_s"] is not None:
            oldest_age = max(oldest_age or 0, stats["oldest_age_s"])
        machines.setdefault(name, {})["queued"] = stats["queued"]

    cloud = publisher.describe() if publisher is not None else {
        "state": base.DISCONNECTED, "broker": "", "topic": "", "signed": False,
        "published": 0, "last_publish_at": None, "last_error": "never started"}

    out = {
        "edge": {
            "state": "RUNNING",
            "started_at": started_at,
            "uptime_s": round(now - started_at, 1) if started_at else None,
        },
        "machines": machines,
        "cloud": cloud,
        "buffer": {
            "queued": queued,
            "oldest_age_s": round(oldest_age, 1) if oldest_age is not None else None,
            "dropped": dropped,
        },
    }
    out["verdict"], out["say"] = _verdict(out, now)
    return out


def _verdict(state, now):
    """The first broken thing, and a sentence a person can act on."""
    machines = state["machines"]
    cloud = state["cloud"]

    unreachable = [n for n, m in machines.items()
                   if m.get("plc") in (base.DISCONNECTED, base.ERROR, base.CONNECTING)]
    if unreachable:
        first = machines[unreachable[0]]
        return NO_PLC, (
            f"{_list(unreachable)} — no connection to the controller"
            + (f" at {first['endpoint']}" if first.get("endpoint") else "")
            + (f": {first['last_error']}" if first.get("last_error") else "")
            + ". Check the IP, the port, and whether this PC can reach that subnet at all.")

    failing = {n: m["bad_tags"] for n, m in machines.items() if m.get("bad_tags")}
    if failing:
        name = next(iter(failing))
        tags = failing[name]
        return BAD_TAGS, (
            f"connected to the controller, but {len(tags)} tag(s) on {name} cannot be read "
            f"({', '.join(tags[:3])}{'...' if len(tags) > 3 else ''}). The network is fine; the "
            f"addresses are wrong. Browse the server and correct them in the config.")

    silent = [n for n, m in machines.items()
              if m.get("since_last_read_s") is None or m["since_last_read_s"] > READ_SILENCE_S]
    if silent and (state["edge"].get("uptime_s") or 0) > READ_SILENCE_S:
        return NO_PLC, (f"{_list(silent)} — connected, but nothing has been read for over "
                        f"{int(READ_SILENCE_S)}s. The session is up and the values are not coming.")

    if cloud.get("state") != base.CONNECTED:
        return NO_CLOUD, (
            f"the controllers are fine and AMP is not reachable at {cloud.get('broker') or 'the broker'}"
            + (f": {cloud['last_error']}" if cloud.get("last_error") else "")
            + f". {state['buffer']['queued']} reading(s) are held on disk and will be sent when "
              f"the connection comes back — nothing is being lost.")

    if (state["buffer"]["oldest_age_s"] or 0) > BACKLOG_AGE_S:
        return BACKLOG, (
            f"connected to everything, but {state['buffer']['queued']} reading(s) are still "
            f"waiting, the oldest for {int(state['buffer']['oldest_age_s'] / 60)} minutes. "
            f"Publishing is slower than reading.")

    if not machines or all(m.get("last_read_at") is None for m in machines.values()):
        # NOT "healthy". Nothing has been read yet, and saying GOOD here is the
        # exact dishonesty AMP's read-models refuse one layer up.
        return STARTING, "the gateway is up and has not read anything yet."

    return GOOD, (f"streaming: {len(machines)} machine(s) reading, "
                  f"{cloud.get('published', 0)} message(s) delivered to AMP"
                  + (f", {state['buffer']['queued']} queued" if state["buffer"]["queued"] else "")
                  + ".")


def _list(names):
    names = sorted(names)
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])} and {names[-1]}"


def lines(state) -> list:
    """The report as flat text, for a console and for the diagnostic export."""
    out = [f"AMP EDGE  {state['verdict']}", f"  {state['say']}", ""]
    uptime = state["edge"].get("uptime_s")
    out.append(f"  EDGE     RUNNING" + (f" ({int(uptime)}s)" if uptime else ""))
    for name, m in sorted(state["machines"].items()):
        since = m.get("since_last_read_s")
        out.append(f"  PLC      {name}: {m.get('plc')} via {m.get('protocol')} "
                   f"{m.get('endpoint') or ''}".rstrip())
        out.append(f"           last read: "
                   + (f"{since:.0f}s ago" if since is not None else "never"))
        if m.get("bad_tags"):
            out.append(f"           BAD TAGS: {', '.join(m['bad_tags'])}")
    cloud = state["cloud"]
    out.append(f"  AMP      {cloud.get('state')} -> {cloud.get('broker')} "
               f"topic {cloud.get('topic')}")
    out.append(f"           signed: {'yes' if cloud.get('signed') else 'NO'}   "
               f"delivered: {cloud.get('published', 0)}")
    buf = state["buffer"]
    out.append(f"  BUFFER   {buf['queued']} queued"
               + (f", oldest {int(buf['oldest_age_s'])}s" if buf.get("oldest_age_s") else "")
               + (f", {buf['dropped']} DROPPED" if buf.get("dropped") else ""))
    return out
