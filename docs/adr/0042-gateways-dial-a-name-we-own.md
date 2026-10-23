# ADR-0042: Gateways dial a hostname MARX8 owns, never the host's

**Status:** Accepted
**Date:** 2026-10-24

## Context

Every AMP Edge gateway in the field opens one outbound connection, and until now
it opened it to `sakura.proxy.rlwy.net:33862` — a hostname **Railway generates
and Railway owns**.

That is fine while AMP is hosted on Railway and catastrophic the day it is not.
A gateway is a small computer bolted inside a plant, on a network AMP cannot
reach, often behind a customer's firewall, with no remote access. Changing the
address in its config means **a person physically driving to that plant**. With
one pilot that is an afternoon. With thirty machines across a dozen customers it
is the reason the migration never happens.

The same argument has already been made once in this codebase and won: the
broker's CA is generated once and never reissued, because
"a fleet that has to be re-visited to trust a new CA is a fleet that stops
reporting until somebody drives to it." The hostname is the same problem and had
the opposite answer.

## Decision

**A gateway's `amp.host` is always a DNS name under a domain MARX8 controls.**
Today that is `mqtt.marx8.com`, a CNAME pointing at whatever host actually runs
the broker. Moving AMP to its own server becomes a DNS change that no plant
notices.

Three constraints make that true, and all three are load-bearing:

1. **The certificate must name every hostname gateways may dial.** `MQTT_TLS_SAN`
   already takes a comma-separated list and reissues the SERVER certificate when
   that list changes, under the SAME CA. So a new name is ADDED to the list, never
   substituted for the old one. Both names keep working, and no gateway needs a
   new `ca.crt`.

2. **The CA's private key moves with the service.** A new server that generates a
   fresh CA presents a certificate every commissioned gateway rejects, which is
   exactly the field visit this ADR exists to prevent. The CA lives on the
   broker's volume; it must be backed up and restored, not regenerated.

3. **The port moves with the name.** DNS carries a hostname, not a port. A gateway
   dialling `mqtt.marx8.com:33862` still has `33862` written in its config, so a
   future server must listen on **33862** — however arbitrary that number looks
   on hardware we own. Choosing the "tidier" 8883 later would require editing
   every gateway, which is the whole cost this decision avoids.

## Consequences

Moving hosts becomes: stand the new broker up, restore the CA, listen on 33862,
repoint the CNAME. Gateways reconnect on their own backoff and nobody drives
anywhere.

The transition for existing gateways is additive and has no downtime window:
add the new name to the SAN, create the DNS record, verify a TLS handshake
against the new name, then change configs at leisure. The old name keeps
working the entire time and should be left in the SAN list indefinitely — it
costs nothing and it is the fallback if DNS is ever misconfigured.

`33862` is now a number AMP is married to. That is the price of never visiting a
plant to change an address, and it is worth paying.

## Alternatives considered

**Leave it as the Railway hostname and fix it at migration time.** This is the
status quo and it is a debt that grows by one site visit per commissioned
gateway. The fix costs half a day today and roughly a day per plant later.

**Put the hostname in a remotely-updatable config.** A gateway that can be told
where to connect from outside is a gateway that can be told to connect
*somewhere else*, which is a far larger security surface than this problem
deserves. ADR-0040 keeps the edge deliberately dumb; a DNS record is the
smallest mechanism that solves it.

**Use a standard port (8883) via a second proxy.** Another hop to run, pay for
and monitor, to avoid writing one odd number in an ADR.
