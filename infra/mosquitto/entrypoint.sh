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
