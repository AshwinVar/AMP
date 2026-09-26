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
| AMP backend → broker, over Railway's private network | **LIVE.** Verified in production: `FastAPI MQTT connected with code: 0`, subscribed to `flowmes/+/+/machines` |
| Edge gateway → broker, from outside Railway | **CONFIGURED, NEVER CONNECTED TO.** TLS listener on 8883 behind a Railway TCP proxy. No gateway has yet completed a TLS handshake against it — see [TLS](#tls-how-a-gateway-outside-railway-reaches-us) |
| ACL: one gateway writes exactly one tenant/site topic | **VERIFIED BY TEST** (`test_mosquitto_config.py`) — against the rendered file, *not* against a running broker |
| Certificate cannot be forged by a bad `MQTT_TLS_SAN` | **VERIFIED BY TEST**, including newline and second-name injection |
| mosquitto enforces the ACL as written | **UNVERIFIED HERE.** No broker and no Docker in CI. Verify it during commissioning, step 6 below |
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
7. **Networking → TCP Proxy → port 8883.** Never 1883: the plaintext listener
   must stay on the private network. Railway assigns a `*.proxy.rlwy.net`
   hostname and a port once you deploy.
8. Set `MQTT_TLS_SAN` to that hostname and deploy again. The broker generates
   its CA on the first run and prints `ca.crt` to the log.

### Why no healthcheck

Railway's healthcheck is HTTP. MQTT is not HTTP, so there is nothing to point it
at; the service is healthy when the log says `mosquitto version 2.0.22 running`.

---

## TLS: how a gateway outside Railway reaches us

Railway's TCP proxy is a **raw passthrough** on a generated
`*.proxy.rlwy.net` hostname, so mosquitto terminates TLS itself. No public CA
will issue a certificate for a domain we do not own, so the broker runs **its
own CA**.

**The private key never leaves the container.** `entrypoint.sh` generates the
CA and the server certificate on the volume at `/mosquitto/data/tls` and reuses
them across deploys. Nothing is pasted into Railway, nothing is committed, and
the only thing anyone copies out is `ca.crt` — a public certificate, printed to
the startup log because a container with no shell has no other way out.

| Variable | Value |
|---|---|
| `MQTT_TLS_SAN` | the hostname gateways dial, e.g. `xxx.proxy.rlwy.net`. Comma-separated for several. **Unset ⇒ no TLS listener at all**, which is the safe default |

The proxy points at **8883**, never 1883. The plaintext listener stays on the
private network where only the AMP backend can reach it; exposing it would put
every gateway credential on the public internet in clear text.

### What a gateway needs

```yaml
amp:
  host: xxx.proxy.rlwy.net
  port: <the port Railway assigned>
  tls: true
  ca_cert: /etc/amp/ca.crt      # copied from the broker's startup log
  username_env: AMP_MQTT_USERNAME
  password_env: AMP_MQTT_PASSWORD
```

`publisher.py` calls `tls_set(ca_certs=...)` when `ca_cert` is set, and falls
back to the system trust store when it is not. There is deliberately **no
`tls_insecure` option**: the first thing anyone reaches for when a certificate
will not verify is the switch that stops it verifying, and a gateway that skips
verification hands its credentials to anything in the path. If the name does
not match, fix the name.

### The CA is generated once, on purpose

Every commissioned gateway pins it. A redeploy that minted a new CA would
disconnect the whole fleet at once and need a site visit each to install the
new `ca.crt`, so the CA is created only when absent and never rotated
automatically. The **server** certificate is reissued whenever `MQTT_TLS_SAN`
changes — which is what happens when the TCP proxy is recreated and Railway
hands out a new hostname — and that reissue keeps the same CA, so no gateway is
touched.

### Rotating the CA, when you eventually must

Delete `/mosquitto/data/tls/ca.*` and redeploy. **Every gateway stops
connecting until it is given the new `ca.crt`**, so this is a planned
maintenance window, not a fix to try while something is broken.

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

Steps 1–2 are **done** (verified in production). Steps 3 onward are not. Step 6
is the one that matters most — it is the only step that tests mosquitto rather
than our rendering of its config.

1. ~~Deploy.~~ **Done.** The log carries `mosquitto version 2.0.22 running`,
   both an `ipv4` and an `ipv6` listen socket on 1883, and our own lines:
   `subscriber 'amp-backend' reads flowmes/+/+/machines` and
   `N gateway credential(s) rendered`.
2. ~~Set the FlowMES variables.~~ **Done.** AMP logs
   `FastAPI MQTT connected with code: 0` and
   `subscribed to flowmes/+/+/machines`. Code 0 means the broker accepted the
   credential *and* the ACL allowed the subscribe.
3. `GET /system-health` as an Admin. The `mqtt` block must read
   `"state": "listener_running"`. `"not_configured"` means `MQTT_BROKER` did not
   take; `"listener_stopped"` means it could not connect or was refused.
   (Note: `MQTT_BROKER` was previously set to `127.0.0.1`, so this reported
   `listener_stopped`, not `not_configured` — "no broker" and "not configured"
   are different states and only one of them is quiet.)
4. **From outside Railway**, with `ca.crt` from the log, complete a TLS
   handshake against `MQTT_TLS_SAN:port` as a gateway user and publish one
   message to its own topic. It should appear in AMP as a machine event. This
   is the first time anything has connected over the TLS listener.
   Then repeat with a deliberately **wrong** `ca.crt` and confirm it is
   refused — a TLS listener that accepts an unverified client is not a TLS
   listener.
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
