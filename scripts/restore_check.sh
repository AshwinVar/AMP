#!/usr/bin/env bash
#
# restore_check.sh — prove the newest database dump can actually be restored.
#
# WHY THIS EXISTS. AMP shipped for two months with no database backups at all.
# The obvious fix is a nightly pg_dump, and the obvious fix is also a trap: a
# dump is a file, and a file is not a recovery. docs/Production-Setup.md §2 ends
# with the sentence this script implements — "a backup you've never restored is
# not a backup". So the deliverable of the backup work is not the dump, it is
# this: an unattended check that takes the most recent dump, restores it into a
# database nobody cares about, and refuses to say PASS unless the result looks
# like AMP's production database rather than an empty shell.
#
# WHAT IT PROVES, AND WHAT IT DOES NOT. It proves the file decompresses, that
# psql can replay it end to end with ON_ERROR_STOP, that the schema arrived
# whole, that the rows came with it, and that credentials survived (a restore
# that loses the users table locks everyone out of their own factory). It does
# not prove the dump is *recent* — that is a property of the schedule, not of
# the file — so data age is reported and warned on, never failed on. A check
# that cries wolf gets muted, and a muted restore check is worse than none.
#
# WHY IT IS PARANOID ABOUT WHERE IT POINTS. It issues CREATE DATABASE and
# DROP DATABASE. Run against the wrong server that is a very bad afternoon, so
# it refuses hosted-looking hosts outright and will only ever drop a database
# whose name it generated itself.
#
# Pure POSIX tooling plus bash arrays: psql and gzip, both of which any machine
# doing a restore already has by definition.
#
# USAGE
#   bash scripts/restore_check.sh [path/to/dump.sql.gz]
#
#   With no argument it picks the newest *.sql.gz in $BACKUP_DIR (default
#   ./backups), which is where the CI drill and the documented manual download
#   both put it.
#
# ENVIRONMENT
#   BACKUP_DIR                 where to look for dumps            (default: backups)
#   RESTORE_CHECK_ADMIN_URL    a throwaway Postgres to restore into
#                              (default: postgres://postgres:postgres@localhost:5432/postgres)
#   EXPECTED_MIN_TABLES        floor on restored table count      (default: 40)
#   REQUIRED_TABLES            space-separated tables that must hold rows
#                              (default: "users machines work_orders")
#   WARN_DATA_AGE_DAYS         warn if the newest row is older    (default: 30)
#   RESTORE_CHECK_KEEP=1       keep the throwaway database for inspection
#   RESTORE_CHECK_ALLOW_REMOTE=1  override the hosted-host guard  (think first)
#
# EXIT CODES
#   0  PASS — every check passed; the dump is a restorable backup
#   1  FAIL — at least one check failed, or the script could not run at all

# -E so the ERR trap below is inherited by functions: an unexpected psql failure
# must announce itself rather than exiting 1 in silence, which would read
# identically to a clean FAIL and send someone hunting the wrong problem.
set -Eeuo pipefail
trap 'printf "\nRESTORE CHECK ABORTED — unexpected error at line %s.\n" "$LINENO" >&2' ERR

BACKUP_DIR="${BACKUP_DIR:-backups}"
ADMIN_URL="${RESTORE_CHECK_ADMIN_URL:-postgres://postgres:postgres@localhost:5432/postgres}"
EXPECTED_MIN_TABLES="${EXPECTED_MIN_TABLES:-40}"
REQUIRED_TABLES="${REQUIRED_TABLES:-users machines work_orders}"
WARN_DATA_AGE_DAYS="${WARN_DATA_AGE_DAYS:-30}"
KEEP="${RESTORE_CHECK_KEEP:-0}"
ALLOW_REMOTE="${RESTORE_CHECK_ALLOW_REMOTE:-0}"

# Every database this script creates carries this prefix, and it will drop
# nothing that does not. The guard is cheap; the failure it prevents is not.
SCRATCH_PREFIX="amp_restore_check_"

FAILURES=0
CHECKS=0

info() { printf '      %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; }
pass() { CHECKS=$((CHECKS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { CHECKS=$((CHECKS + 1)); FAILURES=$((FAILURES + 1)); printf 'FAIL  %s\n' "$*"; }

# Refusal to start at all. Distinct from a failed check: nothing was verified,
# so the caller must not read the exit code as "the backup is bad" — only as
# "the backup is unverified", which for a backup amounts to the same decision.
die() {
  printf '\nRESTORE CHECK ABORTED — %s\n' "$*" >&2
  exit 1
}

# Print the header block above as the help text — one source of truth, so the
# usage can never drift from the documentation the next reader actually sees.
# Stops at the first non-comment line rather than a hardcoded range, which would
# rot the moment anyone adds a paragraph.
usage() {
  awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"
  exit 0
}

case "${1:-}" in
  -h|--help) usage ;;
esac

# ── Preconditions ─────────────────────────────────────────────────────────────

missing=""
for tool in psql gzip; do
  command -v "$tool" > /dev/null 2>&1 || missing="$missing $tool"
done
[ -z "$missing" ] || die "missing required tool(s):$missing. Install the PostgreSQL client (psql) and gzip, then re-run."

# The admin URL must name a database, because the scratch URL is derived from it
# by swapping that last path segment. Catching the malformed case here gives a
# sentence you can act on instead of a psql connection error three steps later.
case "$ADMIN_URL" in
  *://*/*) : ;;
  *) die "RESTORE_CHECK_ADMIN_URL must include a database name, e.g. postgres://postgres:postgres@localhost:5432/postgres" ;;
esac

# Hosted-host guard. This script drops databases; the one place it must never be
# aimed is the estate that holds the only copy of the data. Substring matching is
# deliberately blunt — a false positive costs one environment variable, a false
# negative costs the company.
case "$ADMIN_URL" in
  *rlwy.net*|*railway.app*|*railway.internal*|*rds.amazonaws.com*|*supabase.co*|*neon.tech*|*render.com*)
    [ "$ALLOW_REMOTE" = "1" ] || die "RESTORE_CHECK_ADMIN_URL looks like a hosted database server. This script CREATEs and DROPs databases and belongs on a local throwaway Postgres. If you are certain, set RESTORE_CHECK_ALLOW_REMOTE=1."
    ;;
esac

# ── Pick the dump ─────────────────────────────────────────────────────────────

if [ -n "${1:-}" ]; then
  DUMP="$1"
  [ -f "$DUMP" ] || die "no such dump file: $DUMP"
else
  [ -d "$BACKUP_DIR" ] || die "no dump given and BACKUP_DIR '$BACKUP_DIR' does not exist. Download an artifact from the 'Database backup' workflow into it, or pass a path."
  shopt -s nullglob
  candidates=("$BACKUP_DIR"/*.sql.gz)
  shopt -u nullglob
  [ "${#candidates[@]}" -gt 0 ] || die "no *.sql.gz found in '$BACKUP_DIR'. Download an artifact from the 'Database backup' workflow into it, or pass a path."
  # Newest by name, not by mtime: the workflow stamps every filename with a UTC
  # timestamp (amp-YYYYMMDDTHHMMSSZ.sql.gz), so lexicographic order is
  # chronological order — and unlike mtime it survives being copied about.
  DUMP="$(printf '%s\n' "${candidates[@]}" | sort | tail -n 1)"
fi

DUMP_BYTES="$(wc -c < "$DUMP" | tr -d '[:space:]')"

echo "Dump:    $DUMP (${DUMP_BYTES} bytes)"
echo "Target:  a throwaway database on the server in RESTORE_CHECK_ADMIN_URL"
echo

# ── Throwaway database ────────────────────────────────────────────────────────

SCRATCH_DB="${SCRATCH_PREFIX}$(date -u +%Y%m%d%H%M%S)_$$"
SCRATCH_CREATED=0

# Rebuild a connection URL against a different database, preserving any query
# string (Railway and most hosts append ?sslmode=require, and dropping it turns
# a working connection into a refused one).
url_with_db() {
  local url="$1" db="$2" base query
  case "$url" in
    *\?*) query="?${url#*\?}"; base="${url%%\?*}" ;;
    *)    query=""; base="$url" ;;
  esac
  base="${base%/}"    # tolerate a trailing slash
  base="${base%/*}"   # drop the database name
  printf '%s/%s%s' "$base" "$db" "$query"
}

SCRATCH_URL="$(url_with_db "$ADMIN_URL" "$SCRATCH_DB")"

# -X ignores ~/.psqlrc: an operator's personal settings must not change what a
# verification script sees. ON_ERROR_STOP turns a stream of red text into a
# non-zero exit, which is the entire point.
psql_admin() { PGCONNECT_TIMEOUT=10 psql -X -q -v ON_ERROR_STOP=1 -d "$ADMIN_URL" "$@"; }
scalar() { PGCONNECT_TIMEOUT=10 psql -X -A -t -q -v ON_ERROR_STOP=1 -d "$SCRATCH_URL" -c "$1"; }

cleanup() {
  local status=$?
  if [ "$SCRATCH_CREATED" = "1" ]; then
    if [ "$KEEP" = "1" ]; then
      warn "RESTORE_CHECK_KEEP=1 — leaving '$SCRATCH_DB' in place. Drop it when you are done."
    else
      case "$SCRATCH_DB" in
        "${SCRATCH_PREFIX}"*)
          # A cleanup failure must not overwrite the verdict the run earned, so
          # it degrades to a visible warning rather than an exit code. This is
          # the one tolerated non-fatal command in the script.
          psql_admin -c "DROP DATABASE IF EXISTS \"$SCRATCH_DB\"" > /dev/null 2>&1 ||
            warn "could not drop '$SCRATCH_DB' — drop it by hand."
          ;;
        *) warn "refusing to drop '$SCRATCH_DB': not a throwaway name." ;;
      esac
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

if ! psql_admin -c "CREATE DATABASE \"$SCRATCH_DB\"" > /dev/null 2>&1; then
  die "could not create the throwaway database on RESTORE_CHECK_ADMIN_URL. Is a Postgres server running and reachable, and does the role have CREATEDB?"
fi
SCRATCH_CREATED=1
info "created throwaway database $SCRATCH_DB"
echo

# ── Check 1: the file is an intact gzip stream ────────────────────────────────
# Runs before the restore so a corrupted artifact is named as corruption rather
# than surfacing as an inscrutable syntax error 40,000 lines into a replay.

if gzip -t "$DUMP" 2> /dev/null; then
  pass "dump is an intact gzip stream"
else
  fail "dump is corrupt — gzip cannot read it. This file cannot restore anything."
  echo
  echo "RESTORE CHECK FAILED — 1 of $CHECKS check(s) failed."
  exit 1
fi

# ── Check 2: psql replays the whole dump without a single error ───────────────
# --single-transaction so a failure leaves nothing half-applied and the timing
# reflects a real restore. ON_ERROR_STOP so the first error is fatal: a restore
# that "mostly worked" is the exact thing this script exists to catch.

RESTORE_LOG="$(mktemp)"
restore_start="$(date -u +%s)"
if gzip -dc "$DUMP" | psql -X -q -v ON_ERROR_STOP=1 --single-transaction -d "$SCRATCH_URL" > "$RESTORE_LOG" 2>&1; then
  restore_secs=$(( $(date -u +%s) - restore_start ))
  pass "restore completed with no errors (${restore_secs}s)"
else
  fail "restore ABORTED — psql could not replay the dump. Last 40 lines:"
  tail -n 40 "$RESTORE_LOG" >&2
  echo
  echo "RESTORE CHECK FAILED — $FAILURES of $CHECKS check(s) failed."
  exit 1
fi

# ── Check 3: the schema arrived whole ─────────────────────────────────────────
# A floor, not an equality: models.py declares 48 tables today and grows most
# months. Equality would fail on every legitimate migration and get muted; the
# failure actually worth catching is an order-of-magnitude shortfall, which
# means the dump came from the wrong database or stopped early.

table_count="$(scalar "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")"
if [ "$table_count" -ge "$EXPECTED_MIN_TABLES" ]; then
  pass "schema restored: ${table_count} tables (floor ${EXPECTED_MIN_TABLES})"
else
  fail "only ${table_count} tables restored, expected at least ${EXPECTED_MIN_TABLES}. The dump is incomplete or came from the wrong database."
fi

# ── Check 4: the rows came with the schema ────────────────────────────────────
# The failure this pins is the one that looks like success: a dump taken with
# --schema-only, or against an empty staging database, restores perfectly and
# gives you a factory with no machines in it.

for table in $REQUIRED_TABLES; do
  exists="$(scalar "SELECT to_regclass('public.\"$table\"') IS NOT NULL")"
  if [ "$exists" != "t" ]; then
    fail "required table '$table' is missing from the restored database"
    continue
  fi
  rows="$(scalar "SELECT count(*) FROM public.\"$table\"")"
  if [ "$rows" -gt 0 ]; then
    pass "$table holds ${rows} rows"
  else
    fail "$table restored empty — a backup with no $table is not a recovery"
  fi
done

# ── Check 5: credentials survived ─────────────────────────────────────────────
# Row counts alone would pass a users table whose password column came back
# NULL, and a restored AMP that nobody can log in to is not a restored AMP.
# Length rather than shape: security.py stores bcrypt (60 chars) but still
# accepts legacy SHA-256 hashes (64) pending their next login, so asserting a
# '$2' prefix would fail an entirely healthy restore.

users_exists="$(scalar "SELECT to_regclass('public.users') IS NOT NULL")"
if [ "$users_exists" != "t" ]; then
  fail "no users table in the restored database — nobody could log in to this recovery"
else
  broken="$(scalar "SELECT count(*) FROM public.users WHERE password IS NULL OR length(password) < 20")"
  if [ "$broken" -eq 0 ]; then
    pass "every user row restored with an intact password hash"
  else
    fail "${broken} user row(s) restored with a missing or truncated password hash"
  fi
fi

# ── Reported, never failed: how old the data is ───────────────────────────────
# Staleness is a property of the schedule, not of this file, so it must not turn
# a good restore red. It is still the number an operator wants first, because a
# flawless restore of a six-month-old dump is a very different morning.

if [ "$(scalar "SELECT to_regclass('public.work_orders') IS NOT NULL")" = "t" ]; then
  # work_orders.created_at is a naive UTC timestamp (models.py uses
  # datetime.utcnow), so compare it against UTC explicitly rather than letting
  # the session timezone decide.
  age_days="$(scalar "SELECT COALESCE(floor(EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'UTC') - max(created_at))) / 86400)::int, -1) FROM public.work_orders")"
  if [ "$age_days" -lt 0 ]; then
    info "data age: no work orders carry a created_at, cannot judge freshness"
  elif [ "$age_days" -gt "$WARN_DATA_AGE_DAYS" ]; then
    warn "newest work order is ${age_days} days old (threshold ${WARN_DATA_AGE_DAYS}). The dump restores, but check the backup schedule is still running."
  else
    info "data age: newest work order is ${age_days} day(s) old"
  fi
fi

# ── Verdict ───────────────────────────────────────────────────────────────────

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "RESTORE CHECK PASSED — $CHECKS checks, 0 failures. $DUMP is a restorable backup."
  exit 0
fi
echo "RESTORE CHECK FAILED — $FAILURES of $CHECKS check(s) failed. Do not rely on $DUMP."
exit 1
