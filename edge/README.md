# AMP Edge Gateway

Reads a PLC on the factory network and sends what it reads to AMP.

It runs **inside the plant**, on a small PC beside the line. AMP never connects
into the factory: the gateway dials out, and can be revoked by unplugging one
machine.

```
PLC ──► adapter ──► tag mapper ──► normalizer ──► local queue ──► MQTT ──► AMP
       (OPC UA /   (your config,   (counters      (disk, so an    (outbound
        Modbus)     not code)       become         outage is a     only)
                                    production)    delay, not
                                                   a hole)
```

## What it speaks

Two protocols, both real clients, both proven against a live server. See
[`docs/integration/PLC-PILOT-READINESS.md`](../docs/integration/PLC-PILOT-READINESS.md)
for the support matrix — it says exactly what has been verified against what,
and it does not round up.

## Install

```bash
pip install -r edge/requirements.txt
```

Python 3.9 or newer. No database, no server, no inbound ports.

## Commissioning, in the order you actually do it

```bash
# 1. Is the config coherent? Connects to nothing.
python -m ampedge validate gateway.yaml

# 2. What does this PLC actually have? (OPC UA only — Modbus cannot say.)
python -m ampedge browse gateway.yaml

# 3. THE IMPORTANT ONE. Read every mapped tag once and print the raw value
#    beside the canonical value. Check it against the machine in front of you.
python -m ampedge preview gateway.yaml

# 4. Stream.
python -m ampedge run gateway.yaml

# 5. It is not working and you need help. Writes a REDACTED bundle.
python -m ampedge diagnose gateway.yaml
```

Step 3 is where pilots are won or lost. A wrong register does not error — it
returns a plausible number. Reading `preview` next to the machine is the only
way to catch that, and it takes ten seconds.

## Configuration is data

Everything a commissioning engineer changes lives in a YAML or JSON file:
addresses, datatypes, scaling, units, counter behaviour, poll rate. **No Python
is edited to map a tag.** See [`examples/`](examples/).

Secrets are *not* configuration. Passwords and keys are named in the file and
read from the environment — the loader refuses to start if it finds a literal
one, because config files get emailed and pasted into tickets. See
[`examples/.env.example`](examples/.env.example).

## The rules it will not break

- **No data is not zero.** A PLC that cannot be reached produces no reading, not
  a `0` and not a `false`. A stopped machine and an unreachable one look nothing
  alike in AMP.
- **A counter is never guessed.** Every counter declares whether it is
  cumulative, resets, or holds one cycle. A backwards step it cannot explain
  counts **zero** and says so, rather than inventing a shift's production.
- **Quality is not assumed.** A machine that counts parts but not scrap gets no
  production record at all, unless its config says in writing that there is no
  reject counter. Reporting 100% quality by default is worse than reporting
  nothing.
- **Nothing is lost to an outage.** Readings go to disk first and are deleted
  only once the broker has acknowledged them. A buffered reading keeps its own
  timestamp and is marked as buffered, so late data becomes history rather than
  a false "now".
- **A gateway can only publish as itself.** Messages are signed with a key AMP
  issued for one workspace and one site. Editing the topic breaks the signature.

## Running it as a service

The gateway is a single foreground process; use whatever the plant PC already
has — `systemd` on Linux, NSSM or Task Scheduler on Windows. It needs:

- outbound TCP to the AMP broker (8883 with TLS)
- access to the PLC's subnet
- a writable directory for the queue file

Nothing else, and nothing inbound.
