# ADR-0040: The edge gateway is not AMP — it runs in the factory, dials out, and never substitutes a value

**Status:** accepted · **Date:** 2026-09-25 · **Builds on** [ADR-0027](0027-machine-health-explained.md) (a machine with nothing recorded is not a machine scoring zero) · **Implements** the edge-to-cloud wire contract (`docs/integration/EDGE-CLOUD-WIRE-CONTRACT.md`)

---

## Context

AMP has read `{prefix}/{tenant}/{site}/machines` for a long time, and it works.
Nothing existed in front of that topic. Connecting a customer's machine meant
somebody writing a script, and that script was different every time.

Two shapes were available, and the choice is not reversible cheaply.

**Cloud reads the PLC.** AMP holds a connection into the customer's plant
network. This is what every "we integrate with your machines" demo does,
because it needs no software on site. It also needs an inbound hole in the
plant firewall, a route to the PLC subnet, and an IT department willing to grant
both — which is a six-week conversation before a pilot can start, and a
permanent liability afterwards. A cloud service with a live socket into a
factory's control network is a thing that gets AMP removed from a site after one
security review.

**The factory sends to the cloud.** A small process on a plant PC reads the PLC
and publishes outward. It costs an install. It buys: no inbound ports, a
connection that looks like everything else on that network already reaching the
internet, and a customer who can revoke it by unplugging one machine.

There is also a question this project has already answered once, in a different
place. ADR-0027 established that a machine with nothing recorded is not a
machine scoring zero. The gateway is where that question is *asked first*: a
PLC that does not answer, a tag that does not exist, a register that returns an
exception code. Each of those has an obvious wrong answer sitting right there —
`0`, `false`, the previous value — and the wire will hand it over without
complaint.

## Decision

**1. The gateway is a separate program that imports nothing from AMP.** `edge/`
has its own dependencies and its own tests. CI greps it for imports of
`models`, `main`, `database`, `schemas`, `tenancy`, `mqtt_service` and `crud`
and fails if it finds one. A test may cross the boundary — the end-to-end
suites drive AMP's real `on_message`, because proving the gateway builds a
payload is worthless if that payload lands on the wrong machine. The shipped
gateway may not.

**2. Communication is outbound only.** There is no listener in the package.
Nothing in the factory accepts a connection from AMP.

**3. Absence is never substituted.** A `Reading` carries a quality, and a value
only when that quality is GOOD. `UNCERTAIN` — an OPC UA server saying "here is a
number but I doubt it" — is not usable. A PLC that has gone away produces no
sample, so AMP shows the last known state with its age rather than a machine
that appears to have stopped.

**4. A counter's meaning is declared, never inferred.** Every counter mapping
states `cumulative`, `resets` or `per_cycle`; a mapping without one does not
load. A backwards step the declared mode cannot explain counts **zero** and
records why. Under-reporting is visible to an operator who knows what they made;
over-reporting is not.

**5. Quality is refused rather than assumed.** A machine that counts parts but
not scrap produces no production record at all, unless its configuration states
in writing that untracked parts may be counted as good.

**6. Configuration is data.** Addresses, datatypes, scale, offsets, boolean
maps, enums, units and counter behaviour are all config. No Python is edited to
map a pilot's tags, and validation refuses a bad mapping before a socket opens.
Secrets are *not* configuration: they are named in the file and read from the
environment, and a literal password stops the gateway starting.

**7. Readings reach disk before they reach AMP.** A bounded SQLite queue, and a
record is deleted only once the broker has acknowledged it. Delivery is
therefore at-least-once and every record carries a `record_id`.

## Consequences

**Positive.** A pilot needs a firewall rule the customer already has. An
internet outage becomes a delay rather than a hole in the plant's history. One
canonical signal set means no read-model, OEE calculation or Copilot answer ever
learns which protocol a customer happens to run. The gateway can be given to a
customer as a package without giving them AMP.

**Negative.** Somebody has to install and run a process on a plant PC, and when
it stops, telemetry stops — so the health report has to be good enough that a
commissioning engineer can tell which of five things is broken without calling
us. At-least-once delivery means AMP can see a record twice; until AMP
deduplicates on `record_id`, a retried publish can double-count a production
window, and the wire contract calls that release-blocking. A gateway restart
re-baselines counters and loses the parts made while it was down: chosen
deliberately, because subtracting two numbers across a gap that may contain a
shift change, a reset or a rollover is how phantom production gets invented.

**Also negative, and accepted:** two protocols only. OPC UA and Modbus TCP. A
PLC datasheet listing PROFINET or EtherNet/IP does not mean AMP can read it, and
`docs/integration/PLC-PILOT-READINESS.md` says so per capability rather than in
a footnote.

## Alternatives

**A vendor OPC UA aggregator (KEPServerEX and similar).** Mature, speaks
everything, and would have removed most of this work. Rejected for a first
pilot: it is a per-site licence cost before AMP has proved anything, and it puts
a third party between AMP and the customer's data at exactly the moment the
customer is deciding whether to trust AMP with it. Worth revisiting when a
pilot needs a protocol this gateway does not speak.

**Publishing raw PLC values and normalising in the cloud.** Simpler gateway,
and it moves the counter arithmetic somewhere it can be fixed without a site
visit. Rejected because it moves the *guessing* there too: the cloud would have
to decide what a backwards counter meant, with none of the configuration that
says. It also means AMP's tables would hold protocol-shaped rows, and the
protocol would leak upward one read-model at a time.

**Substituting a safe default for a bad reading.** Rejected — it is the failure
this entire document is arranged around. A stopped machine and an unreachable
one look identical once a `false` has been written for both.

## Rollout

Shipped in one vertical slice (`edge/`, plus the machine-identity fix that had
to come first — a gateway is not safe to point at a customer while its first
packet would register a duplicate of every machine they typed in). Both
protocols are proven end to end against live software servers; neither has met a
physical PLC. The support matrix records that distinction per capability and
must keep doing so.

**Not yet done, and named here so it is not forgotten:** AMP does not verify
gateway signatures. The gateway signs every message; until the cloud checks
them, the MQTT topic is an assertion the broker's ACLs alone are enforcing.
