#!/bin/sh
# Render this broker's identity (passwd) and authority (acl) from the
# environment, then hand over to mosquitto.
#
# WHY RENDER RATHER THAN SHIP FILES
# A password file in the repository is a password in the repository. Railway
# holds the values; this script turns them into the two files mosquitto wants,
# at boot, in a container that is thrown away on every deploy.
#
# WHY THIS SCRIPT IS THE SECURITY BOUNDARY
# backend/mqtt_identity.py trusts the topic BECAUSE the broker checked it. That
# trust is only as good as the ACL rendered here. One unvalidated '+' in a
# tenant name turns `topic write flowmes/ACME/PLANT1/machines` into
# `topic write flowmes/+/PLANT1/machines`, and that gateway can then publish
# into every customer AMP has. So every value that reaches a topic line is
# validated against the same charset backend/mqtt_identity.py enforces, and
# anything that does not match stops the container instead of narrowing to
# something that looks close enough.
#
# FAIL CLOSED, LOUDLY. Every refusal below exits non-zero with a reason. A
# broker that starts with a half-rendered ACL is worse than one that does not
# start: the first is a silent hole, the second is an obvious outage.
#
# Test:  python infra/mosquitto/test_mosquitto_config.py
set -eu

# Character ranges below are matched against the C collating sequence. Without
# this a builder's locale decides whether [A-Za-z] includes, say, 'é', and the
# ACL's safety would vary by build host.
LC_ALL=C
export LC_ALL

# Everything this script creates is readable only by its owner. Set as a UMASK
# rather than chmod-after-the-fact because the gap matters: mosquitto_passwd
# creates the password file itself, and between its creation and our chmod the
# credentials sat world-readable. The first deploy proved it — mosquitto logged
# "File /mosquitto/config/passwd.tmp has world readable permissions" before we
# ever got to fix it.
umask 077

CONFIG_DIR="${AMP_MQTT_CONFIG_DIR:-/mosquitto/config}"
PASSWD_FILE="$CONFIG_DIR/passwd"
ACL_FILE="$CONFIG_DIR/acl"
PASSWD_BIN="${MOSQUITTO_PASSWD_BIN:-mosquitto_passwd}"

# Must agree with backend/mqtt_service.py's DEFAULT_TOPIC_PREFIX. If a
# deployment overrides MQTT_TOPIC_PREFIX it must be overridden in BOTH places:
# the broker would otherwise grant topics nobody subscribes to, which presents
# as "the gateway says it is publishing and AMP sees nothing".
PREFIX="${MQTT_TOPIC_PREFIX:-flowmes}"

die() {
    echo "amp-mosquitto: REFUSING TO START: $*" >&2
    exit 1
}

# The charset from backend/mqtt_identity.py:_IDENTIFIER — `^[A-Za-z0-9]` then up
# to 63 more of `[A-Za-z0-9_.-]` — deliberately re-implemented rather than
# imported: this container has no Python and no AMP code in it, which is the
# separation ADR-0040 requires. test_mosquitto_config.py drives BOTH
# implementations over the same corpus and fails if this one ever accepts
# something the backend would reject.
#
# NOT `grep -E`, WHICH IS WHY THIS IS SIX LINES INSTEAD OF ONE. grep matches
# line by line, so it reports success when ANY line matches: `printf '%s'
# "gw\ntopic write flowmes/+/+/machines" | grep -Eq '^[A-Za-z0-9]...$'` passes,
# because "gw" passes. A value that reaches an ACL line must be validated whole
# or the validation is decorative — and the injected line in that example is a
# grant to every tenant. `case` globs the entire string, newlines included.
# A DNS name, for the TLS certificate's subjectAltName. Looser than an
# identifier (dots are the point) and validated for the same reason: the value
# is written into an OpenSSL config file, so an unvalidated one could add
# `subjectAltName=DNS:anything-it-likes` or a whole new section, and the
# resulting certificate would be trusted by every gateway holding our CA.
valid_hostname() {
    _h="${1:-}"
    [ -n "$_h" ] || return 1
    [ "${#_h}" -le 253 ] || return 1
    case "$_h" in
        [A-Za-z0-9]*) ;;
        *) return 1 ;;
    esac
    case "$_h" in
        *[!A-Za-z0-9.-]*) return 1 ;;
    esac
    # No empty labels: "a..b" and a trailing dot are both rejected rather than
    # normalised, because a certificate is not the place to guess what was meant.
    case "$_h" in
        *..*|*.) return 1 ;;
    esac
    return 0
}

valid_identifier() {
    _v="${1:-}"
    [ -n "$_v" ] || return 1
    [ "${#_v}" -le 64 ] || return 1
    case "$_v" in
        [A-Za-z0-9]*) ;;
        *) return 1 ;;
    esac
    case "$_v" in
        *[!A-Za-z0-9_.-]*) return 1 ;;
    esac
    return 0
}

# Exposed so test_mosquitto_config.py can ask this exact shell function about
# this exact value, instead of asserting against a copy of the rule.
#
# The value arrives in the ENVIRONMENT, not in argv, and that is not fussiness:
# a Windows process boundary re-parses the command line, so an argument
# containing a newline or a quote is not the argument the shell receives. A
# suite whose job is to probe newline handling cannot use a channel that eats
# newlines -- it reported four false accepts before this moved to the
# environment, which is also how the real values (GATEWAY_n_TENANT and friends)
# arrive in production.
if [ "${1:-}" = "--check-identifier" ]; then
    if valid_identifier "${AMP_CHECK_VALUE:-}"; then exit 0; else exit 1; fi
fi
if [ "${1:-}" = "--check-hostname" ]; then
    if valid_hostname "${AMP_CHECK_VALUE:-}"; then exit 0; else exit 1; fi
fi

# Read an environment variable whose NAME is computed. `eval` is the usual way
# to do this in POSIX sh and it is a command-injection hole: for
# GATEWAY_1_USER='x; rm -rf /', `eval "u=${GATEWAY_1_USER}"` runs the rm.
# printenv cannot do that, because the value never reaches the shell parser.
getenv() {
    printenv "$1" 2>/dev/null || true
}

render_only=0
if [ "${1:-}" = "--render-only" ]; then
    render_only=1
fi

mkdir -p "$CONFIG_DIR"

# ── AMP's own subscriber ───────────────────────────────────────────────
# Reads every tenant, writes nothing. It is the one client that legitimately
# sees all traffic, and it has no business publishing: AMP ingests telemetry,
# it does not originate it. A write grant here would let a compromised backend
# forge production counts for any customer.
BACKEND_USER="${AMP_BACKEND_USER:-amp-backend}"
BACKEND_PASSWORD="${AMP_BACKEND_PASSWORD:-}"

valid_identifier "$BACKEND_USER" \
    || die "AMP_BACKEND_USER=$(printf '%s' "$BACKEND_USER" | head -c 40) is not a valid identifier"
[ -n "$BACKEND_PASSWORD" ] \
    || die "AMP_BACKEND_PASSWORD is not set. This broker has no anonymous path by design; set it on this service and point the AMP backend's MQTT_PASSWORD at it."

: > "$ACL_FILE"
{
    echo "# GENERATED AT BOOT by entrypoint.sh. Editing this file achieves"
    echo "# nothing: the container is replaced on every deploy. Change the"
    echo "# service's environment variables instead."
    echo "#"
    echo "# Anything not granted below is DENIED — there are deliberately no"
    echo "# topic lines outside a user block, which is how mosquitto spells"
    echo "# 'and this applies to everyone, including anonymous'."
    echo
    echo "# AMP's ingest listener: every tenant, read-only."
    echo "user $BACKEND_USER"
    echo "topic read $PREFIX/+/+/machines"
} >> "$ACL_FILE"

# The pre-multi-tenant topic carries no tenant segment, so it is only readable
# when a deployment has named the tenant that owns it. Granting it
# unconditionally would be harmless today and confusing forever.
LEGACY_TENANT="$(getenv MQTT_LEGACY_TENANT)"
if [ -n "$LEGACY_TENANT" ]; then
    valid_identifier "$LEGACY_TENANT" \
        || die "MQTT_LEGACY_TENANT is not a valid identifier"
    echo "topic read $PREFIX/machines" >> "$ACL_FILE"
fi

# ── the edge gateways ──────────────────────────────────────────────────
# One numbered slot per gateway: GATEWAY_1_USER / _PASSWORD / _TENANT / _SITE.
# Numbered rather than one packed variable so each password can be an
# independent Railway `${{ secret(32) }}` and can be rotated for one customer
# without touching another's.
: > "$PASSWD_FILE.tmp"
gateway_count=0
i=1
while :; do
    user="$(getenv "GATEWAY_${i}_USER")"
    [ -n "$user" ] || break

    password="$(getenv "GATEWAY_${i}_PASSWORD")"
    tenant="$(getenv "GATEWAY_${i}_TENANT")"
    site="$(getenv "GATEWAY_${i}_SITE")"
    [ -n "$site" ] || site="-"

    valid_identifier "$user" \
        || die "GATEWAY_${i}_USER is not a valid identifier"
    [ -n "$password" ] \
        || die "GATEWAY_${i}_USER is set but GATEWAY_${i}_PASSWORD is not"
    valid_identifier "$tenant" \
        || die "GATEWAY_${i}_TENANT is not a valid identifier. A '+' or '#' here would grant this gateway every tenant's topic, so it is refused rather than escaped."
    # "-" is mqtt_identity.NO_SITE_TOKEN — the wire spelling of "this customer
    # has one plant and nothing meaningful to put here". Every other value is an
    # ordinary identifier.
    if [ "$site" != "-" ]; then
        valid_identifier "$site" \
            || die "GATEWAY_${i}_SITE is neither '-' nor a valid identifier"
    fi

    {
        echo
        echo "# Gateway ${i}: publishes for ${tenant}/${site} and nowhere else."
        echo "user $user"
        echo "topic write $PREFIX/$tenant/$site/machines"
    } >> "$ACL_FILE"

    # argv, not a shell string: a password containing $ ` " or a space reaches
    # mosquitto_passwd exactly as typed. -b is batch mode (no prompt).
    if [ "$gateway_count" -eq 0 ]; then
        "$PASSWD_BIN" -c -b "$PASSWD_FILE.tmp" "$user" "$password"
    else
        "$PASSWD_BIN" -b "$PASSWD_FILE.tmp" "$user" "$password"
    fi

    gateway_count=$((gateway_count + 1))
    i=$((i + 1))
done

# The backend's own credential goes in last so the file is never valid-looking
# but incomplete if a gateway above refused.
if [ "$gateway_count" -eq 0 ]; then
    "$PASSWD_BIN" -c -b "$PASSWD_FILE.tmp" "$BACKEND_USER" "$BACKEND_PASSWORD"
else
    "$PASSWD_BIN" -b "$PASSWD_FILE.tmp" "$BACKEND_USER" "$BACKEND_PASSWORD"
fi

mv "$PASSWD_FILE.tmp" "$PASSWD_FILE"
chmod 0600 "$PASSWD_FILE"
# 0600, NOT 0644. mosquitto 2.0.22 logs "File /mosquitto/config/acl has world
# readable permissions. Future versions will refuse to load this file" — so the
# permissive mode is not a style question, it is a broker that stops starting
# on some later image bump. The ACL is also not public information: it lists
# every tenant and site this broker carries.
chmod 0600 "$ACL_FILE"

# ── TLS, so a gateway outside Railway can reach us at all ──────────────
# WHY OUR OWN CA AND NOT A PUBLIC ONE. Railway's TCP proxy is a raw
# passthrough on a generated `*.proxy.rlwy.net` hostname, and no public CA will
# issue for a domain we do not own. The alternatives were a certificate for a
# domain we DO own (a DNS record plus a renewal every 90 days, forever) or
# this: one CA, generated here, trusted by the gateways we hand it to. Pinning
# a private CA is normal practice for industrial fleets and it has no expiry
# treadmill.
#
# THE KEY NEVER LEAVES THIS CONTAINER. It is generated on the volume at
# /mosquitto/data/tls and re-used across deploys; nothing is pasted into
# Railway, nothing is committed, and the only thing anybody copies out is
# ca.crt, which is a public certificate.
TLS_DIR="${AMP_MQTT_TLS_DIR:-/mosquitto/data/tls}"
TLS_CONF_DIR="$CONFIG_DIR/conf.d"
TLS_SAN="$(getenv MQTT_TLS_SAN)"
OPENSSL_BIN="${OPENSSL_BIN:-openssl}"

mkdir -p "$TLS_CONF_DIR"
rm -f "$TLS_CONF_DIR/tls.conf"

if [ -n "$TLS_SAN" ]; then
    mkdir -p "$TLS_DIR"

    # Validate BEFORE anything reaches the OpenSSL config, and validate every
    # entry: one bad name in a list of three is still a bad certificate.
    san_line=""
    remaining="$TLS_SAN"
    while [ -n "$remaining" ]; do
        case "$remaining" in
            *,*) one="${remaining%%,*}"; remaining="${remaining#*,}" ;;
            *)   one="$remaining";        remaining="" ;;
        esac
        # Trim the ends only, so "a.net, b.net" works. NOT `tr -d ' '`, which
        # deletes spaces in the MIDDLE too and quietly turned "a b.net" into
        # the perfectly valid "ab.net" -- a certificate for a hostname nobody
        # asked for, issued without a word. A space inside a name is a typo,
        # and a typo in a certificate should stop the broker, not be corrected.
        while :; do case "$one" in " "*) one="${one# }" ;; *) break ;; esac; done
        while :; do case "$one" in *" ") one="${one% }" ;; *) break ;; esac; done
        [ -n "$one" ] || continue
        valid_hostname "$one" \
            || die "MQTT_TLS_SAN contains $(printf '%s' "$one" | head -c 60), which is not a DNS name. It would be written into the certificate this broker presents to every gateway, so it is refused rather than escaped."
        if [ -z "$san_line" ]; then
            san_line="DNS:$one"
        else
            san_line="$san_line,DNS:$one"
        fi
    done
    [ -n "$san_line" ] || die "MQTT_TLS_SAN is set but contains no usable hostname"

    command -v "$OPENSSL_BIN" >/dev/null 2>&1 \
        || die "MQTT_TLS_SAN is set but $OPENSSL_BIN is not installed in this image"

    # The CA is generated ONCE and then left alone. Regenerating it would
    # invalidate the ca.crt every already-commissioned gateway is pinning, and
    # a fleet that has to be re-visited to trust a new CA is a fleet that stops
    # reporting until somebody drives to it.
    # EVERY openssl call below passes -config and pins OPENSSL_CONF to the file
    # it was given. Neither the subject nor the extensions may come from
    # whatever openssl.cnf the base image happens to ship, because that file is
    # not ours, can change with an image bump, and decides whether the CA we
    # mint is actually a CA. (It also makes this runnable anywhere: the machine
    # this was written on has OPENSSL_CONF pointing at a path that does not
    # exist, which failed every generation until it was pinned.)
    cat > "$TLS_DIR/ca.cnf" <<'EOF'
[req]
distinguished_name = dn
x509_extensions    = ca_ext
prompt             = no
[dn]
CN = AMP Edge CA
O  = AMP
[ca_ext]
basicConstraints     = critical,CA:TRUE
keyUsage             = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
EOF

    if [ ! -f "$TLS_DIR/ca.key" ] || [ ! -f "$TLS_DIR/ca.crt" ]; then
        echo "amp-mosquitto: generating a new private CA (first run)"
        OPENSSL_CONF="$TLS_DIR/ca.cnf" "$OPENSSL_BIN" req -x509 \
            -newkey rsa:4096 -sha256 -nodes -days 3650 \
            -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" \
            -config "$TLS_DIR/ca.cnf" -extensions ca_ext >/dev/null 2>&1 \
            || die "could not generate the CA"
    fi

    # The SERVER certificate is reissued whenever the SAN changes, which is
    # what happens when the TCP proxy is recreated and Railway hands out a new
    # hostname. Same CA, so no gateway needs touching.
    want="$san_line"
    have=""
    [ -f "$TLS_DIR/server.san" ] && have="$(cat "$TLS_DIR/server.san")"
    if [ "$want" != "$have" ] || [ ! -f "$TLS_DIR/server.crt" ]; then
        echo "amp-mosquitto: issuing a server certificate for $san_line"
        cat > "$TLS_DIR/server.cnf" <<EOF
[req]
distinguished_name = dn
req_extensions     = ext
prompt             = no
[dn]
CN = AMP MQTT broker
[ext]
subjectAltName   = $san_line
basicConstraints = critical,CA:FALSE
keyUsage         = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
EOF
        OPENSSL_CONF="$TLS_DIR/server.cnf" "$OPENSSL_BIN" req -new \
            -newkey rsa:2048 -sha256 -nodes \
            -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.csr" \
            -config "$TLS_DIR/server.cnf" >/dev/null 2>&1 \
            || die "could not generate the server key"
        OPENSSL_CONF="$TLS_DIR/server.cnf" "$OPENSSL_BIN" x509 -req \
            -in "$TLS_DIR/server.csr" -sha256 -days 1825 \
            -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
            -out "$TLS_DIR/server.crt" \
            -extfile "$TLS_DIR/server.cnf" -extensions ext >/dev/null 2>&1 \
            || die "could not sign the server certificate"
        printf '%s' "$want" > "$TLS_DIR/server.san"
        rm -f "$TLS_DIR/server.csr"
    fi

    chmod 0600 "$TLS_DIR/ca.key" "$TLS_DIR/server.key"
    chmod 0644 "$TLS_DIR/ca.crt" "$TLS_DIR/server.crt"

    # No `cafile` and no `require_certificate`: the gateway proves itself with a
    # username and password that the ACL is keyed to, and separately with the
    # HMAC signature AMP checks (ADR-0041). TLS here is about the gateway
    # verifying US, and about nobody on the path reading the credential.
    cat > "$TLS_CONF_DIR/tls.conf" <<EOF
# GENERATED AT BOOT by entrypoint.sh. Edit MQTT_TLS_SAN, not this file.
listener 8883
protocol mqtt
certfile $TLS_DIR/server.crt
keyfile $TLS_DIR/server.key
require_certificate false
EOF

    echo "amp-mosquitto: TLS listener on 8883 for $san_line"
    echo "amp-mosquitto: CA fingerprint $(OPENSSL_CONF="$TLS_DIR/ca.cnf" "$OPENSSL_BIN" x509 -in "$TLS_DIR/ca.crt" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
    # The CA CERTIFICATE is public — it is the thing every gateway must carry,
    # and the log is the only way out of a container with no shell access. The
    # CA KEY is not printed here and must never be.
    echo "amp-mosquitto: ---- copy the block below to each gateway as ca.crt ----"
    cat "$TLS_DIR/ca.crt"
    echo "amp-mosquitto: ---- end ca.crt ----"
else
    echo "amp-mosquitto: MQTT_TLS_SAN is not set, so there is no TLS listener and no gateway outside Railway can connect. This is the safe default: exposing 1883 through a TCP proxy would put every gateway credential on the public internet in clear text."
fi

# Counts and usernames only. A password has never been printed by this script
# and must not start being: these lines go to Railway's log viewer, which is
# shoulder-surfable and retained.
echo "amp-mosquitto: subscriber '$BACKEND_USER' reads $PREFIX/+/+/machines"
echo "amp-mosquitto: $gateway_count gateway credential(s) rendered"
if [ "$gateway_count" -eq 0 ]; then
    echo "amp-mosquitto: no GATEWAY_1_USER set — no gateway can publish to this broker yet."
fi

if [ "$render_only" -eq 1 ]; then
    exit 0
fi

# THE BASE IMAGE'S ENTRYPOINT DOES THIS AND WE HAVE JUST REPLACED IT.
# eclipse-mosquitto starts as root and mosquitto drops to the `mosquitto` user
# itself, so every file it opens after that point must be readable by that user
# — including the 0600 password file we just wrote as root, and the persistence
# directory it writes the session database into. Miss this and the broker either
# exits with "Error: Unable to open pwfile" or starts fine and loses every
# queued message on restart, which is the worse of the two because it looks
# like success.
if [ "$(id -u)" = "0" ]; then
    chown mosquitto:mosquitto "$PASSWD_FILE" "$ACL_FILE" 2>/dev/null || true
    chown -R mosquitto:mosquitto /mosquitto/data /mosquitto/log 2>/dev/null || true
fi

# Absolute path, copied from the base image's own CMD rather than assumed.
# /usr/sbin is on PATH in that image today, so a bare `mosquitto` resolves --
# but this script's whole job is to be the thing that does not depend on an
# environment nobody here can run.
exec /usr/sbin/mosquitto -c "$CONFIG_DIR/mosquitto.conf"
