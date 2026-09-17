"""Mutation harness for the statement-integrity primitives and the 0009 schema (ADR-0020).

Each mutation is a plausible edit that would let an acceptance count for content
nobody accepted, let one statement content hash two ways, or let the schema stop
enforcing what the acceptance design depends on. Every one must turn a suite red.

WHAT IS MUTATED
---------------
  canonical.py            the canonical form (ordering, separators, encoding,
                          NFC, refusals), sha256_hex, ts / utc_seconds, and
                          acceptance_is_valid — the only copy of the rule
  models.py / 0009 / tenancy.py
                          the unique keys, the tenant scoping, types and
                          nullability the plan fixes

RUN IT ALONE. It EDITS THE WORKING TREE and restores each file byte for byte
(CRLF files stay CRLF), so anything else reading those files meanwhile sees a
deliberately broken codebase.

POSTGRESQL. Some schema properties are invisible to SQLite. Set PG_DATABASE_URL
to a PostgreSQL URL whose credentials may create scratch databases and the
harness also runs verify_pg_outcome_contracts.py for every mutation.

Run: python backend/mutate_canonical.py
     PG_DATABASE_URL=postgresql://... python backend/mutate_canonical.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATION = os.path.join("alembic", "versions", "0009_outcome_contracts.py")

SQLITE_SUITES = ["test_canonical.py", "test_migration_0009_outcome_contracts.py",
                 "test_tenancy.py"]
PG_SUITE = "verify_pg_outcome_contracts.py"


def _one(path, old, new):
    return [(path, old, new)]


MUTATIONS = [
    # --- one content, one byte string ------------------------------------------
    ("keys are not sorted", _one("canonical.py",
        "json.dumps(walked, sort_keys=True,", "json.dumps(walked, sort_keys=False,")),
    # Anchored on the CODE lines: the module docstring quotes the same call, and
    # the unanchored patterns matched twice and were skipped.
    ("whitespace creeps into the separators", _one("canonical.py",
        '    return json.dumps(walked, sort_keys=True, separators=(",", ":"),',
        '    return json.dumps(walked, sort_keys=True, separators=(", ", ": "),')),
    ("non-ASCII is escaped instead of raw UTF-8", _one("canonical.py",
        '                      ensure_ascii=False, allow_nan=False).encode("utf-8")',
        '                      ensure_ascii=True, allow_nan=False).encode("utf-8")')),
    ("strings are not NFC-normalised", _one("canonical.py",
        '    return unicodedata.normalize("NFC", value)', "    return value")),
    ("lone surrogates are not refused", _one("canonical.py",
        "    if _SURROGATE.search(value):\n        raise CanonicalError(",
        "    if False:\n        raise CanonicalError(")),
    ("keys colliding under NFC are silently merged", _one("canonical.py",
        "                if norm in out:", "                if False:")),
    ("bool is accepted as an int", _one("canonical.py",
        "    if kind is int:", "    if kind is int or kind is bool:")),
    ("a float is accepted as-is", _one("canonical.py",
        "    if kind is str:\n        return _text(value, path)\n",
        "    if kind is str:\n        return _text(value, path)\n"
        '    if kind.__name__ == "float":\n        return value\n')),
    ("a Decimal is accepted as its str()", _one("canonical.py",
        "    if kind is str:\n        return _text(value, path)\n",
        "    if kind is str:\n        return _text(value, path)\n"
        '    if kind.__name__ == "Decimal":\n        return str(value)\n')),
    ("integers beyond 2**53 are accepted", _one("canonical.py",
        "        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:",
        "        if False:")),
    ("non-str keys are stringified, as json.dumps would", [
        ("canonical.py", "                if type(key) is not str:", "                if False:"),
        ("canonical.py", '                norm = _text(key, f"{path}.<key>")',
         '                norm = _text(str(key), f"{path}.<key>")')]),
    ("cycles are not detected", _one("canonical.py",
        "        if marker in open_containers:", "        if False:")),

    # --- the hash --------------------------------------------------------------
    ("sha256_hex quietly encodes a str", _one("canonical.py",
        "    if not isinstance(data, (bytes, bytearray)):\n"
        "        raise CanonicalError(\n"
        '            f"sha256_hex hashes bytes, not {type(data).__name__}")\n',
        "    if isinstance(data, str):\n"
        '        data = data.encode("utf-8")\n')),
    ("an uppercase hex digest counts as a content hash", _one("canonical.py",
        '_SHA256_HEX = re.compile(r"\\A[0-9a-f]{64}\\Z")',
        '_SHA256_HEX = re.compile(r"\\A[0-9a-fA-F]{64}\\Z")')),

    # --- instants --------------------------------------------------------------
    ("ts silently drops a fractional second", _one("canonical.py",
        "    if dt.microsecond:\n        raise CanonicalError(",
        "    if False:\n        raise CanonicalError(")),
    ("ts renders an aware time's wall clock, not UTC", _one("canonical.py",
        '        raise CanonicalError(f"ts needs a datetime, not {type(dt).__name__}")\n'
        "    if dt.tzinfo is not None and dt.utcoffset() is not None:\n"
        "        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)",
        '        raise CanonicalError(f"ts needs a datetime, not {type(dt).__name__}")\n'
        "    if dt.tzinfo is not None and dt.utcoffset() is not None:\n"
        "        dt = dt.replace(tzinfo=None)")),
    ("utc_seconds does not truncate", _one("canonical.py",
        "    return dt.replace(microsecond=0, tzinfo=None)",
        "    return dt.replace(tzinfo=None)")),

    # --- the acceptance rule ---------------------------------------------------
    ("an acceptance is valid on hash alone (C1: old acceptance revives)",
     _one("canonical.py", "    return a_hash == s_hash and a_rev == s_rev",
          "    return a_hash == s_hash")),
    ("an acceptance is valid on revision alone", _one("canonical.py",
        "    return a_hash == s_hash and a_rev == s_rev", "    return a_rev == s_rev")),
    ("an acceptance counts for any statement with the same hash and revision",
     _one("canonical.py",
          "    if type(a_statement) is not int or type(s_id) is not int or a_statement != s_id:\n"
          "        return False",
          "    if False:\n        return False")),
    ("two malformed hashes that are equal count as a match", _one("canonical.py",
        "    if not (is_sha256_hex(a_hash) and is_sha256_hex(s_hash)):\n        return False",
        "    if False:\n        return False")),
    ("two invalid revisions that are equal count as a match", _one("canonical.py",
        "    if not (_revision(a_rev) and _revision(s_rev)):\n        return False",
        "    if False:\n        return False")),
    ("True stands in for revision 1", _one("canonical.py",
        "    return type(value) is int and value >= 1",
        "    return isinstance(value, int) and value >= 1")),
    ("revision 0 is a valid revision", _one("canonical.py",
        "    return type(value) is int and value >= 1",
        "    return type(value) is int and value >= 0")),
    ("the explicit None guard is removed", _one("canonical.py",
        "    if acceptance is None or statement is None:\n        return False",
        "    if False:\n        return False")),

    # --- the schema ------------------------------------------------------------
    ("the acceptance key loses revision (model and migration)", [
        ("models.py",
         '        UniqueConstraint("statement_id", "party", "revision",\n'
         '                         name="uq_statement_acceptance"),',
         '        UniqueConstraint("statement_id", "party",\n'
         '                         name="uq_statement_acceptance"),'),
        (MIGRATION,
         '            sa.UniqueConstraint("statement_id", "party", "revision",\n'
         '                                name="uq_statement_acceptance"),',
         '            sa.UniqueConstraint("statement_id", "party",\n'
         '                                name="uq_statement_acceptance"),')]),
    ("two statements may exist for one period (model and migration)", [
        ("models.py",
         '        UniqueConstraint("contract_id", "period_start",\n'
         '                         name="uq_contract_statement_period"),\n',
         ""),
        (MIGRATION,
         '            sa.UniqueConstraint("contract_id", "period_start",\n'
         '                                name="uq_contract_statement_period"),\n',
         "")]),
    ("the migration forgets the acceptance key (model keeps it)", _one(MIGRATION,
        '            sa.UniqueConstraint("statement_id", "party", "revision",\n'
        '                                name="uq_statement_acceptance"),\n', "")),
    ("the migration forgets the span lookup index", _one(MIGRATION,
        '        ("ix_machine_telemetry_spans_lookup",\n'
        '         ["tenant_code", "machine_id", "source", "span_start"]),\n', "")),
    ("the migration stores terms_hash without a length", _one(MIGRATION,
        'sa.Column("terms_hash", sa.String(64), nullable=False),',
        'sa.Column("terms_hash", sa.String(), nullable=False),')),
    ("seconds become a 32-bit INTEGER (model and migration)", [
        ("models.py", "    seconds = Column(BigInteger, nullable=False)",
         "    seconds = Column(Integer, nullable=False)"),
        (MIGRATION, 'sa.Column("seconds", sa.BigInteger(), nullable=False),',
         'sa.Column("seconds", sa.Integer(), nullable=False),')]),
    ("a statement's canonical_json becomes NOT NULL", _one("models.py",
        "    canonical_json = Column(Text, nullable=True)",
        "    canonical_json = Column(Text, nullable=False)")),
    ("a span without a tenant lands in DEFAULT", _one("models.py",
        "    tenant_code = Column(String, index=True, nullable=False)\n"
        '    machine_id = Column(Integer, ForeignKey("machines.id"), index=True, nullable=False)',
        '    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")\n'
        '    machine_id = Column(Integer, ForeignKey("machines.id"), index=True, nullable=False)')),
    ("spans are not tenant-scoped", _one("tenancy.py",
        "    models.MachineTelemetrySpan,\n)", ")")),
    ("a contract gets a tenant_code the offboarding sweep would delete",
     _one("models.py",
          "    factory_tenant_code = Column(String, index=True, nullable=False)\n"
          "    contract_ref = Column(String, nullable=False)",
          "    tenant_code = Column(String, index=True, nullable=False)\n"
          "    contract_ref = Column(String, nullable=False)")),
    ("downgrade leaves the span table behind", _one(MIGRATION,
        "    for name in reversed(_TABLES):", "    for name in reversed(_TABLES[:-1]):")),
    ("upgrade no longer skips tables boot already created", _one(MIGRATION,
        "        if name in present:\n            continue\n        _create(name)",
        "        _create(name)")),
]

# A mutation another guard already covers. Each needs a reason that survives
# reading.
EXPECTED_SURVIVORS = {
    "the explicit None guard is removed":
        "SHADOWED BY THE STATEMENT BINDING. getattr(None, 'statement_id', None) is "
        "None, which fails the `type(...) is not int` check on the next line, so a "
        "None acceptance or statement is still invalid. Kept as the explicit, "
        "readable refusal; removing BOTH is the 'counts for any statement' "
        "mutation's territory and the 'no acceptance' case still pins the result.",
}


def _read(path):
    with open(os.path.join(HERE, path), "rb") as fh:
        return fh.read()


def _write(path, data):
    with open(os.path.join(HERE, path), "wb") as fh:
        fh.write(data)


def _apply(source_bytes, old, new):
    """Replace once, speaking the file's own newline convention."""
    text = source_bytes.decode("utf-8")
    if "\r\n" in text:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    count = text.count(old)
    if count != 1:
        return None, count
    return text.replace(old, new, 1).encode("utf-8"), 1


def run_suites():
    failed = []
    for suite in SQLITE_SUITES:
        env = dict(os.environ, DATABASE_URL="sqlite:///./ci.db", PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, suite], capture_output=True, text=True,
                              errors="replace", cwd=HERE, env=env)
        if proc.returncode != 0:
            failed.append(suite)
    pg = os.environ.get("PG_DATABASE_URL")
    if pg:
        env = dict(os.environ, DATABASE_URL=pg, PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, PG_SUITE], capture_output=True, text=True,
                              errors="replace", cwd=HERE, env=env)
        if proc.returncode != 0:
            failed.append(PG_SUITE)
    return failed


def main():
    paths = sorted({p for _, edits in MUTATIONS for p, _, _ in edits})
    originals = {p: _read(p) for p in paths}

    suites = SQLITE_SUITES + ([PG_SUITE] if os.environ.get("PG_DATABASE_URL") else [])
    baseline = run_suites()
    if baseline:
        print(f"ABORT: suites already failing before any mutation: {baseline}")
        return 2
    print(f"baseline: all {len(suites)} suites green ({', '.join(suites)})\n")
    print(f"{'mutation':<72} {'verdict':<9} caught by")
    print("-" * 118)

    survived = []
    for label, edits in MUTATIONS:
        mutated = dict(originals)
        problem = None
        for path, old, new in edits:
            result, count = _apply(mutated[path], old, new)
            if result is None:
                problem = f"pattern hits {count} in {path}"
                break
            mutated[path] = result
        if problem:
            print(f"{label:<72} {'SKIP':<9} {problem}")
            survived.append(f"{label} (pattern did not apply)")
            continue
        touched = {p for p, _, _ in edits}
        try:
            for p in touched:
                _write(p, mutated[p])
            failing = run_suites()
        finally:
            for p in touched:
                _write(p, originals[p])
        if failing:
            verdict, note = "caught", ", ".join(
                s.replace("test_", "").replace(".py", "")[:28] for s in failing)
        elif label in EXPECTED_SURVIVORS:
            verdict, note = "shadowed", EXPECTED_SURVIVORS[label][:60] + "..."
        else:
            verdict, note = "SURVIVED", "-- nothing --"
        print(f"{label:<72} {verdict:<9} {note}")
        if verdict == "SURVIVED":
            survived.append(label)

    dirty = [p for p in paths if _read(p) != originals[p]]
    if dirty:
        print(f"\nERROR: files not restored byte for byte: {dirty}")
        return 3
    print()
    if survived:
        print(f"{len(survived)} mutation(s) not caught:")
        for s in survived:
            print("   *", s)
        return 1
    print(f"ALL {len(MUTATIONS)} MUTATIONS CAUGHT OR SHADOWED WITH A STATED REASON; "
          "every file restored byte for byte")
    return 0


if __name__ == "__main__":
    sys.exit(main())
