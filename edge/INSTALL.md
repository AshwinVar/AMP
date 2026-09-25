# Installing AMP Edge on a plant PC

Written for the person standing in front of the machine, not for a developer.
Every step says what "worked" looks like, so you can stop as soon as something
does not.

**Before you start**, the customer's
[intake form](../docs/integration/PLC-PILOT-CLIENT-INTAKE.md) should be filled
in. The two answers that decide whether this works at all are *which protocol
the controller actually has switched on* and *what each counter does when it
resets*. Everything else can be found on the day; those two cannot.

---

## What the PC needs

| | |
|---|---|
| **OS** | Windows 10/11 or any current Linux. Nothing exotic. |
| **Python** | 3.9 or newer. `python --version` to check. |
| **Network to the PLC** | Same subnet, or a route to it. See "Can it see the PLC?" below. |
| **Network to AMP** | Outbound TCP **8883** only. No inbound ports, no static IP, no port forwarding. |
| **Disk** | ~200 MB, plus room for the queue (a busy machine buffering for a week is well under 100 MB). |
| **Privileges** | None special. Do not run it as Administrator or root. |

It does **not** need: a public IP, a VPN, a firewall exception for inbound
traffic, or a connection to the corporate domain.

---

## 1. Install

```bash
python -m pip install -r requirements.txt
```

Behind a corporate proxy, pip needs telling:

```bash
python -m pip install --proxy http://proxy.example.com:8080 -r requirements.txt
```

**Worked when:** `python -c "import asyncua, pymodbus, paho.mqtt.client"` prints
nothing.

## 2. Write the config

Copy the example nearest your protocol and edit it:

- [`examples/opcua-gateway.yaml`](examples/opcua-gateway.yaml)
- [`examples/modbus-gateway.yaml`](examples/modbus-gateway.yaml)

**The machine name must match the machine already in AMP, exactly.** If it does
not, AMP registers a new one and the customer's history splits in two. Check the
spelling against the machine list in AMP before you start, not after.

## 3. Put the secrets in the environment

Copy [`examples/.env.example`](examples/.env.example) to `.env` and fill it in.
The config file names the variables; it never holds the values, and the gateway
refuses to start if it finds a password written into it.

- `AMP_GATEWAY_KEY`, `AMP_MQTT_USERNAME`, `AMP_MQTT_PASSWORD` — issued by AMP
- `PLC_PASSWORD` — the controller's own, from the customer's controls engineer

Keep these separate. The plant's controls password should never be the thing
that also opens their AMP workspace.

## 4. Check the config before touching the network

```bash
python -m ampedge validate gateway.yaml
```

This connects to nothing. It tells you every problem at once rather than one per
restart.

**Worked when:** it prints `OK`, the workspace and site you expect, and your tag
list.

## 5. Find out what the PLC actually has (OPC UA only)

```bash
python -m ampedge browse gateway.yaml
```

Tag lists are wrong the first three times — `Machine.PartCount` turns out to be
`Machine.Counters.Parts`. Browsing turns that from an afternoon into a minute.

Modbus cannot do this. A register is a number with no name, so you need the
register map from the machine builder; there is no way around it.

## 6. **The important step.** Read the live values

```bash
python -m ampedge preview gateway.yaml
```

This prints, for every mapped tag, the RAW value beside the CANONICAL value.

**Stand in front of the machine and compare.** Start the machine — does
`running` turn true? Make one part — does `part_count` move by one? Is the
temperature 48.5 or 485?

This is the step that decides whether the pilot works. A wrong register does not
produce an error; it produces a **plausible number**. Nothing downstream can
catch that, and by the time somebody notices, a month of history is wrong.

> A counter's first reading shows `(baseline)`, not a value. That is correct —
> the first reading establishes where the counter started. Run `preview` again
> after a part is made and you will see the increment.

## 7. Stream

```bash
python -m ampedge run gateway.yaml
```

It prints a health summary every minute. Watch for `STREAMING`, then check the
machine in AMP.

**Worked when:** the machine's state in AMP changes when you start and stop the
machine.

---

## Running it as a service

The gateway is one ordinary foreground process. Use whatever the PC already has.

**Linux (systemd)** — `/etc/systemd/system/amp-edge.service`:

```ini
[Unit]
Description=AMP Edge Gateway
After=network-online.target

[Service]
Type=simple
User=ampedge
WorkingDirectory=/opt/amp-edge
EnvironmentFile=/opt/amp-edge/.env
ExecStart=/usr/bin/python3 -m ampedge run /opt/amp-edge/gateway.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Windows** — NSSM (`nssm install AMPEdge`) or Task Scheduler with "run whether
the user is logged on or not". Set the working directory to the folder holding
`gateway.yaml`.

**Docker** — see the header of [`Dockerfile`](Dockerfile). Mount the queue as a
volume; without it a restart discards anything not yet delivered.

Whatever you choose: **restart it automatically, and keep the queue file on
local disk.** Not a network share — a queue on a share that goes away is a
gateway that stops.

---

## When it is not working

Run this first. It tells you *which side* is broken, which is the question:

```bash
python -m ampedge run gateway.yaml --health-every 10
```

| It says | It means | Do this |
|---|---|---|
| `PLC_UNREACHABLE` | The controller is not answering. | `ping` the PLC IP. Check the port (4840 OPC UA, 502 Modbus). Check the protocol is **enabled in the controller**, not just listed on the datasheet. |
| `TAGS_FAILING` | The network is fine; the addresses are wrong. | `browse` (OPC UA) or re-check the register map. `preview` names each failing tag. |
| `AMP_UNREACHABLE` | The controller is fine; AMP is not reachable. | Check outbound 8883 is allowed. **Nothing is being lost** — readings are queueing on disk and will be sent when the link returns. |
| `BACKLOG_GROWING` | Everything is connected but publishing is slower than reading. | Usually a very short poll interval. Check the oldest-record age in the health output. |
| `STARTING` | Up, nothing read yet. | Normal for the first few seconds. If it persists, it is really `PLC_UNREACHABLE`. |

### Things that look like bugs and are not

**"The machine shows Idle in AMP but it is running."** Check `preview`: if
`running` reads `false`, the PLC is saying so — usually the wrong tag, or a run
bit that means "in auto mode" rather than "producing".

**"AMP shows no production."** If only `part_count` is mapped and there is no
reject counter, AMP records **no production rather than claiming 100% quality**.
Map `reject_count`, or set `quality_unknown_is_good: true` on that machine if
the line genuinely has no reject counter. The gateway says which in its notes.

**"Parts are missing after a restart."** Expected. A restarted gateway
re-baselines its counters rather than subtracting across a gap that might
contain a shift change or a reset. It under-reports; it never invents.

**"A counter jumped to a huge number."** Almost always Modbus `word_order`. Set
the other value and compare against the machine in `preview`.

### Sending a diagnostic

```bash
python -m ampedge diagnose gateway.yaml
```

Writes `amp-edge-diagnostics.json`: the configuration with every secret replaced
by `<redacted>`, and only the **names** of the environment variables in use —
never their values. Open it before sending if your site requires you to.

---

## Removing it

Stop the service, delete the folder. That is all of it: no registry entries, no
system-wide packages, no scheduled tasks beyond the service you created, and
nothing left on the PLC — the gateway only ever **read** from it.

Machines already in AMP stay in AMP, with the history they have collected.
