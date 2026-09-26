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
| Messages queued while AMP restarts | **NOT WORKING.** See [The queue is not armed yet](#the-queue-is-not-armed-yet) |

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

## The queue is not armed yet

`mosquitto.conf` sets `persistence`, `max_queued_messages` and
`persistent_client_expiration` so the broker holds messages for a subscriber
that is temporarily gone. None of it does anything yet, because
`backend/mqtt_service.py:_build_client` constructs a bare `mqtt.Client()` —
paho's defaults are a **random client id** and **`clean_session=True`**. The
broker therefore has no session to queue against, and anything published while
AMP is restarting is dropped.

The gateway will have been PUBACKed for those messages and will have deleted
them from its local buffer, so this is silent data loss on every deploy, not a
visible outage. The edge buffer does not cover it: it protects the
gateway→broker hop, and this is the broker→AMP hop.

The fix is a fixed `client_id` plus `clean_session=False`, and it needs care
about what happens if the service ever runs more than one replica (two
subscribers sharing a client id disconnect each other in a loop; MQTT v5 shared
subscriptions are the real answer). It is deliberately not in this change: it is
untestable without a broker, and now there is one.

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
