"""The terms of a downtime attribution contract: parse, canonicalise, hash (ADR-0021).

WHAT THE TERMS SAY
------------------
A service contract an SME machine maker already signs (an annual maintenance
contract, a warranty, an uptime clause) gets a machine-readable annex: which
installations are covered, when, what the SLA target and credit schedule are,
which status sources both parties trust, and how the factory's OWN downtime
reasons are attributed. Attribution from the factory's own MES reasons is the
differentiator; metering and shared usage ledgers already exist (SteamChain,
PayperChain, Linxfour, Rockwell US10747201B2) and nothing here claims novelty
for them. A freedom-to-operate review is needed before commercial launch.

Schema 1, every field required, nothing defaulted:

    {"schema":1, "currency":"INR", "period_months":1|3, "period_fee":"40000.00",
     "timezone":"Asia/Kolkata", "term_months":12,
     "coverage": {"mode":"24x7"}
               | {"mode":"weekly",
                  "windows":[{"days":[0..6], "start":"08:00", "end":"20:00"}],
                  "excluded_dates":["2026-10-20"]},
     "sla_target_pct":"97.00",
     "credit_tiers":[{"below_pct":"97.00","credit_pct":"5.00"}, ...],
     "min_measured_pct":"90.00",
     "trusted_sources":["mqtt"],
     "status_defaults":{"Breakdown":"OEM","Maintenance":"FACTORY","Offline":"DISPUTED"},
     "reason_map":{"no material":"FACTORY", ...},
     "generic_reasons":["breakdown","unknown"],
     "reason_lead_seconds":600, "termination_notice_days":30,
     "covered_installations":[{"installation_id":12,"serial_number":"AER-0042"}]}

FAIL CLOSED, BY NAME
--------------------
`parse` refuses a missing field, an unknown field, a wrong type or a value out
of range with TermsError(field, msg), where `field` is the path
("coverage.windows[0].end"). A term the OEM did not write is never supplied by
a default: a default is a term the factory never saw.

Validation beyond types:
  * decimals are D() text; percentages <= 100.00;
  * term_months is a multiple of period_months, so every period is whole;
  * weekly windows have end > start within one local day ("24:00" is the end of
    the day). An overnight shift is two windows. Days are 0=Monday .. 6=Sunday;
  * credit tiers: below_pct strictly descending, credit_pct strictly
    ascending, 0 < credit_pct <= 100, and 0 < below_pct <= sla_target_pct (a
    tier above the target would credit a period that MET the SLA);
  * status_defaults has exactly the down statuses: machine_status's vocabulary
    minus the AVAILABLE ones (Running, Idle), derived, never re-listed. Values
    are OEM, FACTORY or DISPUTED;
  * reason_map values are OEM or FACTORY; keys go through reason_key, must not
    collide after it and must not be generic reasons;
  * generic_reasons must include the reasons AMP writes itself: MQTT's
    automatic "Breakdown" log and the normaliser's "Unknown";
  * trusted_sources is non-empty. "manual" is refused: the manual status PATCH
    writes no telemetry span, so trusting it would make every period no-data.

ONE MEANING, ONE HASH
---------------------
`terms_canonical` renders a parsed Terms as a canonical-ready document: decimals
through contract_money.decimal_text, reason keys normalised, unordered lists
sorted. `terms_hash` is canonical.content_hash of it, and
parse(terms_canonical(t)) == t. Key order, whitespace, "040000.00" and
"No Material" therefore cannot give one set of terms two hashes.

Run the tests: DATABASE_URL="sqlite:///./ci.db" python backend/test_contract_terms.py
"""
import functools
import json
import re
import unicodedata
import zoneinfo
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Tuple

import canonical
import machine_status
from analytics_engine import normalize_downtime_reason
from contract_money import D, MoneyError, decimal_text

SCHEMA = 1
CURRENCIES = ("INR",)
PERIOD_MONTHS = (1, 3)
MAX_TERM_MONTHS = 120
MAX_REASON_LEAD_SECONDS = 86400
MAX_NOTICE_DAYS = 366

# ── Buckets and parties: the one vocabulary ─────────────────────────────────
AVAILABLE = "AVAILABLE"
OEM = "OEM"
FACTORY = "FACTORY"
DISPUTED = "DISPUTED"
UNMEASURED = "UNMEASURED"
BUCKETS = (AVAILABLE, OEM, FACTORY, DISPUTED, UNMEASURED)
PARTIES = (OEM, FACTORY)
STATUS_DEFAULT_BUCKETS = (OEM, FACTORY, DISPUTED)
# A dispute must settle its window, so it can never resolve to DISPUTED.
RESOLUTION_BUCKETS = (AVAILABLE, OEM, FACTORY, UNMEASURED)

# ── Statuses ────────────────────────────────────────────────────────────────
# Idle is AVAILABLE: ready, not scheduled. That is a scheduling matter for the
# factory, not machine downtime the OEM owes a credit for.
AVAILABLE_STATUSES = (machine_status.RUNNING,
                      machine_status.normalize_machine_status("idle"))
if None in AVAILABLE_STATUSES or not set(AVAILABLE_STATUSES) <= set(
        machine_status.VALID_MACHINE_STATUSES):
    raise ImportError("contract_terms: AVAILABLE_STATUSES left the status vocabulary")


def down_status_keys():
    """The statuses a down episode can have: the vocabulary minus AVAILABLE ones.

    Derived, so a status added to machine_status is a status the terms must
    attribute (parse then refuses every existing document until it does)."""
    return tuple(s for s in machine_status.VALID_MACHINE_STATUSES
                 if s not in AVAILABLE_STATUSES)


# ── Reasons ─────────────────────────────────────────────────────────────────
# The reason mqtt_service writes on its automatic DowntimeLog when a machine
# transitions into Breakdown (test_contract_terms pins it against that source).
MQTT_AUTO_REASON = machine_status.normalize_machine_status("breakdown")

# A span source name, as telemetry spans record it.
_SOURCE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
# The manual status PATCH writes no span; trusting it would be trusting nothing.
UNTRUSTABLE_SOURCES = ("manual",)

_HHMM = re.compile(r"\A([01][0-9]|2[0-3]):([0-5][0-9])\Z")
_ISO_DATE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_ZONE_KEY = re.compile(r"\A[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*\Z")
_SURROGATE = re.compile("[\ud800-\udfff]")

MINUTES_PER_DAY = 1440
_HUNDRED = D("100.00")
_ZERO = D("0.00")


class TermsError(ValueError):
    """A terms document is invalid. `field` is the path of the offending value."""

    def __init__(self, field, msg):
        self.field = field
        self.msg = msg
        super().__init__(f"{field}: {msg}")


def reason_key(reason):
    """The one normal form of a downtime reason, for mapping and comparison.

    normalize_downtime_reason (None/blank -> "Unknown", stripped), then NFC,
    casefold, NFC again: casefold can decompose (U+01F0 -> j + caron), and the
    key must itself be NFC so it survives canonical_bytes unchanged and
    reason_key(reason_key(x)) == reason_key(x)."""
    if reason is not None and type(reason) is not str:
        raise TermsError("reason", f"a reason is text, not {type(reason).__name__}")
    text = unicodedata.normalize("NFC", normalize_downtime_reason(reason)).strip()
    return unicodedata.normalize("NFC", text.casefold())


REQUIRED_GENERIC_REASONS = tuple(sorted({reason_key(None), reason_key(MQTT_AUTO_REASON)}))


# ── The parsed terms ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CreditTier:
    below_pct: object      # Decimal
    credit_pct: object     # Decimal


@dataclass(frozen=True)
class CoverageWindow:
    days: Tuple[int, ...]  # sorted, 0=Monday .. 6=Sunday
    start_minute: int      # minutes after local midnight, 0..1439
    end_minute: int        # start_minute < end_minute <= 1440


@dataclass(frozen=True)
class Coverage:
    mode: str                              # "24x7" | "weekly"
    windows: Tuple[CoverageWindow, ...]    # sorted; empty for 24x7
    excluded_dates: Tuple[date, ...]       # sorted; empty for 24x7


@dataclass(frozen=True)
class CoveredInstallation:
    installation_id: int
    serial_number: str


@dataclass(frozen=True)
class Terms:
    schema: int
    currency: str
    period_months: int
    period_fee: object                     # Decimal
    timezone: str
    term_months: int
    coverage: Coverage
    sla_target_pct: object                 # Decimal
    credit_tiers: Tuple[CreditTier, ...]
    min_measured_pct: object               # Decimal
    trusted_sources: Tuple[str, ...]       # sorted
    status_defaults: MappingProxyType      # down status -> OEM|FACTORY|DISPUTED
    reason_map: MappingProxyType           # reason_key -> OEM|FACTORY
    generic_reasons: Tuple[str, ...]       # sorted reason keys
    reason_lead_seconds: int
    termination_notice_days: int
    covered_installations: Tuple[CoveredInstallation, ...]   # sorted by id

    @property
    def zone(self):
        return zoneinfo.ZoneInfo(self.timezone)


FIELDS = ("schema", "currency", "period_months", "period_fee", "timezone",
          "term_months", "coverage", "sla_target_pct", "credit_tiers",
          "min_measured_pct", "trusted_sources", "status_defaults", "reason_map",
          "generic_reasons", "reason_lead_seconds", "termination_notice_days",
          "covered_installations")


# ── Field validators ────────────────────────────────────────────────────────

def _object(value, field, required, optional=()):
    if type(value) is not dict:
        raise TermsError(field, "must be an object")
    for key in value:
        if type(key) is not str or (key not in required and key not in optional):
            raise TermsError(f"{field}.{key}" if field != "$" else str(key),
                             "unknown field")
    for key in required:
        if key not in value:
            raise TermsError(f"{field}.{key}" if field != "$" else key, "required")
    return value


def _list(value, field):
    if type(value) is not list:
        raise TermsError(field, "must be a list")
    return value


def _int(value, field, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise TermsError(field, f"must be an integer from {lo} to {hi}")
    return value


def _text(value, field):
    if type(value) is not str or not value.strip():
        raise TermsError(field, "must be non-blank text")
    if _SURROGATE.search(value):
        raise TermsError(field, "contains a lone surrogate")
    return unicodedata.normalize("NFC", value)


def _decimal(value, field):
    try:
        return D(value)
    except MoneyError:
        raise TermsError(field, "must be decimal text with exactly two places, "
                                "e.g. \"40000.00\"") from None


def _pct(value, field):
    d = _decimal(value, field)
    if d > _HUNDRED:
        raise TermsError(field, "a percentage cannot exceed 100.00")
    return d


def _minute(value, field, allow_end_of_day):
    if allow_end_of_day and value == "24:00":
        return MINUTES_PER_DAY
    m = _HHMM.match(value) if type(value) is str else None
    if m is None:
        raise TermsError(field, "must be HH:MM (00:00-23:59"
                         + (", or 24:00)" if allow_end_of_day else ")"))
    return int(m.group(1)) * 60 + int(m.group(2))


@functools.lru_cache(maxsize=1)
def _zone_keys():
    return frozenset(zoneinfo.available_timezones())


def _timezone(value, field):
    if type(value) is not str or _ZONE_KEY.match(value) is None \
            or value not in _zone_keys():
        raise TermsError(field, "must be an IANA timezone name, e.g. Asia/Kolkata")
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise TermsError(field, "timezone cannot be loaded") from None
    return value


def _coverage(value, field):
    mode = value.get("mode") if type(value) is dict else None
    if mode == "24x7":
        _object(value, field, ("mode",))
        return Coverage("24x7", (), ())
    if mode != "weekly":
        _object(value, field, ("mode",), ("windows", "excluded_dates"))
        raise TermsError(f"{field}.mode", "must be \"24x7\" or \"weekly\"")
    _object(value, field, ("mode", "windows"), ("excluded_dates",))
    windows_field = f"{field}.windows"
    raw_windows = _list(value["windows"], windows_field)
    if not raw_windows:
        raise TermsError(windows_field, "a weekly coverage needs at least one window")
    windows = []
    for i, raw in enumerate(raw_windows):
        wf = f"{windows_field}[{i}]"
        _object(raw, wf, ("days", "start", "end"))
        days = _list(raw["days"], f"{wf}.days")
        if not days or any(type(d) is not int or not 0 <= d <= 6 for d in days) \
                or len(set(days)) != len(days):
            raise TermsError(f"{wf}.days", "must be distinct integers 0 (Monday) to "
                                           "6 (Sunday), at least one")
        start = _minute(raw["start"], f"{wf}.start", allow_end_of_day=False)
        end = _minute(raw["end"], f"{wf}.end", allow_end_of_day=True)
        if end <= start:
            raise TermsError(f"{wf}.end", "must be after start on the same day; "
                                          "write an overnight shift as two windows")
        windows.append(CoverageWindow(tuple(sorted(days)), start, end))
    if len(set(windows)) != len(windows):
        raise TermsError(windows_field, "the same window is listed twice")
    dates_field = f"{field}.excluded_dates"
    raw_dates = _list(value.get("excluded_dates", []), dates_field)
    dates = []
    for i, raw in enumerate(raw_dates):
        df = f"{dates_field}[{i}]"
        if type(raw) is not str or _ISO_DATE.match(raw) is None:
            raise TermsError(df, "must be a date YYYY-MM-DD")
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            raise TermsError(df, "is not a real date") from None
    if len(set(dates)) != len(dates):
        raise TermsError(dates_field, "a date is listed twice")
    return Coverage("weekly",
                    tuple(sorted(windows, key=lambda w: (w.days, w.start_minute,
                                                         w.end_minute))),
                    tuple(sorted(dates)))


def _credit_tiers(value, field, target):
    tiers = []
    for i, raw in enumerate(_list(value, field)):
        tf = f"{field}[{i}]"
        _object(raw, tf, ("below_pct", "credit_pct"))
        below = _pct(raw["below_pct"], f"{tf}.below_pct")
        credit = _pct(raw["credit_pct"], f"{tf}.credit_pct")
        if below <= _ZERO or below > target:
            raise TermsError(f"{tf}.below_pct", "must be above 0.00 and at most "
                                                "sla_target_pct")
        if credit <= _ZERO:
            raise TermsError(f"{tf}.credit_pct", "must be above 0.00")
        if tiers and below >= tiers[-1].below_pct:
            raise TermsError(f"{tf}.below_pct", "tiers must be in strictly "
                                                "descending below_pct order")
        if tiers and credit <= tiers[-1].credit_pct:
            raise TermsError(f"{tf}.credit_pct", "a deeper tier must give a strictly "
                                                 "larger credit")
        tiers.append(CreditTier(below, credit))
    return tuple(tiers)


def _trusted_sources(value, field):
    sources = _list(value, field)
    if not sources:
        raise TermsError(field, "at least one trusted status source is required")
    for i, s in enumerate(sources):
        if type(s) is not str or _SOURCE.match(s) is None:
            raise TermsError(f"{field}[{i}]", "must be a lower-case source name")
        if s in UNTRUSTABLE_SOURCES:
            raise TermsError(f"{field}[{i}]", f"{s!r} writes no telemetry span and "
                                              "cannot be a trusted source")
    if len(set(sources)) != len(sources):
        raise TermsError(field, "a source is listed twice")
    return tuple(sorted(sources))


def _status_defaults(value, field):
    _object(value, field, (), down_status_keys())
    if set(value) != set(down_status_keys()):
        raise TermsError(field, "must give exactly "
                                + ", ".join(down_status_keys()))
    for status in down_status_keys():
        if value[status] not in STATUS_DEFAULT_BUCKETS:
            raise TermsError(f"{field}.{status}", "must be OEM, FACTORY or DISPUTED")
    return MappingProxyType({s: value[s] for s in down_status_keys()})


def _generic_reasons(value, field):
    raw = _list(value, field)
    keys = []
    for i, r in enumerate(raw):
        keys.append(reason_key(_text(r, f"{field}[{i}]")))
    if len(set(keys)) != len(keys):
        raise TermsError(field, "a reason is listed twice (after normalisation)")
    missing = [k for k in REQUIRED_GENERIC_REASONS if k not in keys]
    if missing:
        raise TermsError(field, "must include the reasons AMP writes itself: "
                                + ", ".join(missing))
    return tuple(sorted(keys))


def _reason_map(value, field, generic):
    if type(value) is not dict:
        raise TermsError(field, "must be an object")
    mapped = {}
    for raw_key, party in value.items():
        kf = f"{field}.{raw_key}"
        if type(raw_key) is not str or _SURROGATE.search(raw_key):
            raise TermsError(kf, "a reason must be text")
        if party not in PARTIES:
            raise TermsError(kf, "must be OEM or FACTORY")
        key = reason_key(raw_key)
        if key in generic:
            raise TermsError(kf, "is a generic reason; generic reasons explain nothing")
        if key in mapped:
            raise TermsError(field, f"{raw_key!r} and another reason are the same "
                                    "after normalisation")
        mapped[key] = party
    return MappingProxyType(dict(sorted(mapped.items())))


def _covered_installations(value, field):
    raw = _list(value, field)
    if not raw:
        raise TermsError(field, "at least one installation must be covered")
    out = []
    for i, item in enumerate(raw):
        f = f"{field}[{i}]"
        _object(item, f, ("installation_id", "serial_number"))
        iid = _int(item["installation_id"], f"{f}.installation_id", 1,
                   canonical.MAX_SAFE_INTEGER)
        serial = _text(item["serial_number"], f"{f}.serial_number")
        out.append(CoveredInstallation(iid, serial))
    if len({c.installation_id for c in out}) != len(out):
        raise TermsError(field, "an installation is listed twice")
    if len({c.serial_number for c in out}) != len(out):
        raise TermsError(field, "a serial number is listed twice")
    return tuple(sorted(out, key=lambda c: c.installation_id))


def _refuse_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise TermsError("$", f"key {key!r} appears twice")
        out[key] = value
    return out


def _refuse_constant(name):
    raise TermsError("$", f"{name} is not a number")


def _load(raw):
    if type(raw) is dict:
        return raw
    if type(raw) in (bytes, bytearray):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise TermsError("$", "terms are not UTF-8") from None
    if type(raw) is not str:
        raise TermsError("$", "terms must be a JSON object")
    try:
        doc = json.loads(raw, object_pairs_hook=_refuse_duplicate_keys,
                         parse_constant=_refuse_constant)
    except ValueError as e:
        if isinstance(e, TermsError):
            raise
        raise TermsError("$", f"not valid JSON ({e.msg})") from None
    return doc


def parse(raw):
    """Parse a terms document (dict, JSON text or UTF-8 bytes) into Terms.

    Raises TermsError(field, msg) naming the first invalid field."""
    doc = _object(_load(raw), "$", FIELDS)
    schema = doc["schema"]
    if type(schema) is not int or schema != SCHEMA:
        raise TermsError("schema", f"must be {SCHEMA}")
    if doc["currency"] not in CURRENCIES:
        raise TermsError("currency", "must be one of " + ", ".join(CURRENCIES))
    period_months = doc["period_months"]
    if type(period_months) is not int or period_months not in PERIOD_MONTHS:
        raise TermsError("period_months", "must be 1 (monthly) or 3 (quarterly)")
    period_fee = _decimal(doc["period_fee"], "period_fee")
    timezone = _timezone(doc["timezone"], "timezone")
    term_months = _int(doc["term_months"], "term_months", 1, MAX_TERM_MONTHS)
    if term_months % period_months:
        raise TermsError("term_months", "must be a whole number of periods")
    coverage = _coverage(doc["coverage"], "coverage")
    target = _pct(doc["sla_target_pct"], "sla_target_pct")
    tiers = _credit_tiers(doc["credit_tiers"], "credit_tiers", target)
    min_measured = _pct(doc["min_measured_pct"], "min_measured_pct")
    sources = _trusted_sources(doc["trusted_sources"], "trusted_sources")
    defaults = _status_defaults(doc["status_defaults"], "status_defaults")
    generic = _generic_reasons(doc["generic_reasons"], "generic_reasons")
    reasons = _reason_map(doc["reason_map"], "reason_map", generic)
    lead = _int(doc["reason_lead_seconds"], "reason_lead_seconds", 0,
                MAX_REASON_LEAD_SECONDS)
    notice = _int(doc["termination_notice_days"], "termination_notice_days", 0,
                  MAX_NOTICE_DAYS)
    installations = _covered_installations(doc["covered_installations"],
                                           "covered_installations")
    return Terms(
        schema=schema, currency=doc["currency"], period_months=period_months,
        period_fee=period_fee, timezone=timezone, term_months=term_months,
        coverage=coverage, sla_target_pct=target, credit_tiers=tiers,
        min_measured_pct=min_measured, trusted_sources=sources,
        status_defaults=defaults, reason_map=reasons, generic_reasons=generic,
        reason_lead_seconds=lead, termination_notice_days=notice,
        covered_installations=installations)


# ── Canonical document and hash ─────────────────────────────────────────────

def _hhmm(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def terms_canonical(t):
    """The canonical-ready document for parsed terms. parse() of it returns `t`."""
    if t.coverage.mode == "24x7":
        coverage = {"mode": "24x7"}
    else:
        coverage = {
            "mode": "weekly",
            "windows": [{"days": list(w.days), "start": _hhmm(w.start_minute),
                         "end": _hhmm(w.end_minute)} for w in t.coverage.windows],
            "excluded_dates": [d.isoformat() for d in t.coverage.excluded_dates],
        }
    return {
        "schema": t.schema,
        "currency": t.currency,
        "period_months": t.period_months,
        "period_fee": decimal_text(t.period_fee),
        "timezone": t.timezone,
        "term_months": t.term_months,
        "coverage": coverage,
        "sla_target_pct": decimal_text(t.sla_target_pct),
        "credit_tiers": [{"below_pct": decimal_text(x.below_pct),
                          "credit_pct": decimal_text(x.credit_pct)}
                         for x in t.credit_tiers],
        "min_measured_pct": decimal_text(t.min_measured_pct),
        "trusted_sources": list(t.trusted_sources),
        "status_defaults": dict(t.status_defaults),
        "reason_map": dict(t.reason_map),
        "generic_reasons": list(t.generic_reasons),
        "reason_lead_seconds": t.reason_lead_seconds,
        "termination_notice_days": t.termination_notice_days,
        "covered_installations": [{"installation_id": c.installation_id,
                                   "serial_number": c.serial_number}
                                  for c in t.covered_installations],
    }


def terms_json_text(t):
    """The exact text to store as terms_json: the canonical bytes, decoded."""
    return canonical.canonical_bytes(terms_canonical(t)).decode("utf-8")


def terms_hash(t):
    """SHA-256 of the canonical terms document. What both parties accept."""
    return canonical.content_hash(terms_canonical(t))
