# AMP's production MQTT broker

The broker AMP ingests telemetry through, and the ACL that makes
`backend/mqtt_identity.py`'s tenant isolation real rather than aspirational.

It is deployed on Railway as its own service, built from this directory. It
contains no AMP code and imports nothing from `backend/` or `edge/` — it is
infrastructure AMP talks to, in the same category as Postgres.

---

## Support status — read this before promising anything

| Path | Status |
|---|---|
| AMP backend → broker, over Railway's private network | **PROVISIONED, NOT YET EXERCISED BY A GATEWAY** |
| Edge gateway → broker, from outside Railway | **NOT AVAILABLE.** No TLS listener and no TCP proxy — see [The external path is not open yet](#the-external-path-is-not-open-yet) |
| ACL: one gateway writes exactly one tenant/site topic | **VERIFIED BY TEST** (`test_mosquitto_config.py`, 11/11 mutants caught) — against the rendered file, *not* against a running broker |
| mosquitto enforces that file as written | **UNVERIFIED HERE.** No broker and no Docker in CI. Verify it during commissioning, step 6 below |
| Messages queued while AMP restarts | **AMP ASKS FOR IT CORRECTLY** (persistent session + QoS 1); that mosquitto honours it is unverified until step 6 |

Nothing on this page has been tested against a physical PLC.

---

## What the pieces are

| File | What it does |
|---|---|
| `Dockerfile` | `eclipse-mosquitto:2.0.22`, pinned. Adds our entrypoint. |
| `mosquitto.conf` | Static settings. **No credentials, no topic grants.** |
| `entrypoint.sh` | Renders `passwd` and `acl` from the environment at boot, then `exec mosquitto`. The security boundary. |
| `test_mosquitto_config.py` | Drives `entrypoint.sh --render-only` and asserts the ACL cannot be widened. |

The ACL is rendered rather than committed because a password file in the
repository is a password in the repository. Railway holds the values; the
container is rebuilt on every deploy and the files exist only inside it.

---

## Variables

Set on the **mosquitto** service:

| Variable | Value | Notes |
|---|---|---|
| `AMP_BACKEND_PASSWORD` | `${{ secret(32) }}` | Railway generates it. Nobody needs to see it. |
| `AMP_BACKEND_USER` | *(optional)* | Defaults to `amp-backend`. |
| `MQTT_TOPIC_PREFIX` | *(optional)* | Defaults to `flowmes`. **If you set it, set it on the AMP backend too** — see below. |
| `GATEWAY_1_USER` | e.g. `gw-acme-plant1` | One numbered slot per gateway. |
| `GATEWAY_1_PASSWORD` | `${{ secret(32) }}` | Read it out of Railway when commissioning that gateway. |
| `GATEWAY_1_TENANT` | e.g. `ACME` | Must be the tenant code AMP knows. |
| `GATEWAY_1_SITE` | e.g. `PLANT1`, or `-` | `-` is the wire spelling of "no site" (`mqtt_identity.NO_SITE_TOKEN`). |

Set on the **FlowMES** service:

| Variable | Value |
|---|---|
| `MQTT_BROKER` | `mosquitto.railway.internal` |
| `MQTT_PORT` | `1883` |
| `MQTT_USERNAME` | `amp-backend` |
| `MQTT_PASSWORD` | `${{ mosquitto.AMP_BACKEND_PASSWORD }}` — a *reference*, so the value is never copied |
| `MQTT_TLS` | **unset.** The private network never leaves Railway; see below |

### The numbering must be contiguous

`entrypoint.sh` stops scanning at the first missing `GATEWAY_n_USER`. Deleting
slot 2 of 3 silently strands slot 3 — it will authenticate and then be unable to
publish anything, which reads exactly like a PLC fault. Renumber instead.
Pinned by a test so this note and the code cannot drift apart.

### The prefix must agree in two places

`MQTT_TOPIC_PREFIX` defaults to `flowmes` here and in
`backend/mqtt_service.py`. Override one without the other and the broker grants
topics nobody is subscribed to: the gateway reports healthy publishes and AMP
receives nothing. The test asserts the two defaults match; it cannot assert your
overrides do.

---

## Provisioning on Railway

1. **New** → **GitHub Repo** → this repository.
2. Service **Settings** → **Root Directory**: `infra/mosquitto`.
3. Same page → **Watch Paths**: `infra/mosquitto/**`. Without this, every commit
   to AMP redeploys the broker and drops every connected gateway.
4. Rename the service to **`mosquitto`** (Settings → Service Name). The name is
   load-bearing: it is the `mosquitto` in `mosquitto.railway.internal` and in the
   `${{ mosquitto.AMP_BACKEND_PASSWORD }}` reference.
5. **Variables** → add the table above.
6. **Volume** → mount at `/mosquitto/data`. Without it `persistence true` writes
   to a container filesystem that is discarded on every deploy.
7. Do **not** enable a TCP proxy. See below.

### Why no healthcheck

Railway's healthcheck is HTTP. MQTT is not HTTP, so there is nothing to point it
at; the service is healthy when the log says `mosquitto version 2.0.22 running`.

---

## The external path is not open yet

A gateway at a customer site cannot reach this broker, and that is deliberate.

Reaching it from outside Railway needs a **TCP proxy**, which is a raw
passthrough on a random high port. Enabling it as things stand would publish a
**plaintext** MQTT listener on the public internet, and every gateway credential
would cross it in the clear. `edge/ampedge/publisher.py` already defaults
`tls=True` for exactly this reason.

Both ways to close the gap are real work, not configuration:

- **A domain we own.** CNAME `mqtt.<domain>` at the proxy host, issue a
  DNS-01 certificate for it, serve it from an 8883 listener here. The gateway
  needs no change — `tls_set()` with the system CA store already trusts it.
  Costs: certificate renewal has to run somewhere.
- **Our own CA.** Ship a CA certificate with each gateway and pin it. More
  robust for industrial sites, and standard practice there — but
  `publisher.py` calls `tls_set()` with no arguments and has no `ca_cert`
  setting, so it is an edge change plus a key-distribution story.

Until one of them is done, the honest description of this broker is: **AMP can
ingest over MQTT within Railway; nothing outside Railway can publish to it.**

## The queue, and the one thing that would silently disarm it

`mosquitto.conf` sets `persistence`, `max_queued_messages` and
`persistent_client_expiration` so the broker holds messages for a subscriber
that is temporarily gone. AMP asks it to: `_build_client` connects with a
stable client id (`MQTT_CLIENT_ID`, default `amp-ingest`) and
`clean_session=False`, and `on_connect` subscribes at **QoS 1**.

**Both halves are load-bearing.** A broker queues nothing for an offline
subscriber whose subscription is QoS 0 — `queue_qos0_messages` is false above,
and false by default — so a persistent session that subscribed at paho's
default of 0 would queue exactly nothing while looking entirely correct. If you
ever change one, change the other.

**One ingest process per broker.** Two subscribers sharing a client id take the
session from each other on every connect and flap forever, losing more than the
session saves. Railway runs one replica by default; a second environment
pointed at this broker must set `MQTT_CLIENT_ID` to something else. (Giving
them different ids means each gets its OWN copy of every message — correct for
a staging environment reading production traffic, wrong as a way to scale
ingest. MQTT v5 shared subscriptions are the answer to that, and AMP does not
use them.)

**How to tell whether it worked.** AMP logs the CONNACK's session-present flag
on every connect. `resumed its existing broker session` means the broker held
the backlog. A `FRESH broker session` warning is expected exactly once — on the
first connect after configuring MQTT — and at any other time means the session
was gone and whatever was published while AMP was away was never queued.

---

## Commissioning: proving it actually works

Nothing below has been run yet. Step 6 is the one that matters — it is the only
step that tests mosquitto rather than our rendering of its config.

1. Deploy. The log should end with `mosquitto version 2.0.22 running` and, above
   it, our own two lines: `subscriber 'amp-backend' reads flowmes/+/+/machines`
   and `N gateway credential(s) rendered`.
2. Set the FlowMES variables. It redeploys.
3. `GET /system-health` as an Admin. The `mqtt` block must read
   `"state": "listener_running"`. `"not_configured"` means `MQTT_BROKER` did not
   take; `"listener_stopped"` means it could not connect or was refused.
4. Publish one message as the gateway user to its own topic. It should appear in
   AMP as a machine event.
5. Publish as the gateway user to a **different tenant's** topic. The broker must
   refuse it. If it arrives, stop and do not commission anything.
6. Publish as the gateway user to `flowmes/+/+/machines`, to `$SYS/#`, and
   subscribe to another tenant's topic. All three must be refused. This is the
   step that verifies mosquitto enforces the ACL as written, which no test in
   this repository can.
7. Only then, work through `docs/` for the gateway's own commissioning steps.

---

## Running it locally

```
docker build -t amp-mosquitto infra/mosquitto
docker run --rm -p 1883:1883 -e AMP_BACKEND_PASSWORD=local-only amp-mosquitto
```

`docker-compose.yml` deliberately does **not** use this image: the local stack
runs an anonymous broker so a developer can `mosquitto_pub` at it without a
credential. This one has no anonymous path at all.

Run the tests with `python infra/mosquitto/test_mosquitto_config.py`. They need
a POSIX shell (Git for Windows provides one) and no broker.
