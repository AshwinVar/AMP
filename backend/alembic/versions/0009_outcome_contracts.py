"""agreed downtime attribution: service contracts, statements, acceptances, spans (ADR-0020)

Revision ID: 0009_outcome_contracts
Revises: 0008_machine_claim
Create Date: 2026-09-17

WHY
---
SME machine makers already sign annual maintenance contracts, warranties and
uptime clauses with their factory customers, and at month end both sides argue
about whose fault the downtime was. These tables hold such a contract, a periodic
statement that attributes every covered downtime minute to AVAILABLE, OEM,
FACTORY, DISPUTED or UNMEASURED from records AMP actually receives, and each
party's acceptance of an exact statement revision. Attribution from the
factory's own MES reasons is the differentiator.

Metering, pay-per-use and shared usage ledgers already exist
(SteamChain, PayperChain, Linxfour; Rockwell US10747201B2) and this migration
builds none of them: no ledger, no chain, no usage billing. Integrity is one
SHA-256 per statement revision plus the audit log. A freedom-to-operate review
is needed before commercial launch.

WHAT THE CONSTRAINTS ARE FOR
----------------------------
  uq_contract_statement_period   (contract_id, period_start)
      ONE statement per contract period, revised in place. Two rows for one
      period would let each party accept a different one.

  uq_statement_acceptance        (statement_id, party, revision)
      REVISION IS IN THE KEY. A party accepts revision 1; a dispute raised and
      withdrawn produces revision 3 with identical bytes; the party must be able
      to accept again, and the old row must stay as history rather than be
      overwritten. Validity itself is canonical.acceptance_is_valid.

  uq_contract_term_version       (contract_id, version)
  uq_service_contract_ref        (oem_code, contract_ref)
  uq_contract_machine_coverage   (term_version_id, installation_id)
  uq_attribution_record_seq      (statement_id, seq)

  hash columns VARCHAR(64)       lowercase hex SHA-256; PostgreSQL enforces the
                                 length, SQLite does not (so does the code).

  NO Numeric / Float column      money and percentages live as decimal TEXT in
                                 the JSON documents. SQLite stores NUMERIC as
                                 REAL, which is exactly the float money avoids.

  NO `tenant_code` on the seven contract tables
      offboard_tenant.purge_tenant_data hard-deletes every mapper carrying that
      attribute. A contract is the OEM's record as much as the factory's; the
      tables carry oem_code and factory_tenant_code instead (the
      machine_installations precedent).

  machine_telemetry_spans.tenant_code NOT NULL, no default
      the one tenant-owned table here (tenancy.SCOPED_MODELS). It is swept by
      the ordinary offboarding purge; its machine_id FK is why the purge's
      FK-ordered passes see it.

  ix_machine_telemetry_spans_lookup (tenant_code, machine_id, source, span_start)
      the engine's only access path; ix on span_end serves retention.

Every table also gets `ix_<table>_id`: every model declares
`id = Column(Integer, primary_key=True, index=True)`, and the CI drift gate
demands the migrated schema match models.py (see 0008's note).

Additive and reversible. Creates eight tables and alters nothing that exists, so
a database with live factory and OEM data upgrades without rewriting a row. Each
table is guarded: boot's create_all may already have created it.
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_outcome_contracts"
down_revision = "0008_machine_claim"
branch_labels = None
depends_on = None


# Creation order follows the foreign keys; downgrade walks it backwards.
_INDEXES = {
    "service_contracts": [
        ("ix_service_contracts_id", ["id"]),
        ("ix_service_contracts_oem_code", ["oem_code"]),
        ("ix_service_contracts_factory_tenant_code", ["factory_tenant_code"]),
    ],
    "service_contract_term_versions": [
        ("ix_service_contract_term_versions_id", ["id"]),
        ("ix_service_contract_term_versions_contract_id", ["contract_id"]),
    ],
    "service_contract_machines": [
        ("ix_service_contract_machines_id", ["id"]),
        ("ix_service_contract_machines_term_version_id", ["term_version_id"]),
        ("ix_service_contract_machines_installation_id", ["installation_id"]),
    ],
    "contract_statements": [
        ("ix_contract_statements_id", ["id"]),
        ("ix_contract_statements_contract_id", ["contract_id"]),
        ("ix_contract_statements_term_version_id", ["term_version_id"]),
        ("ix_contract_statements_content_hash", ["content_hash"]),
    ],
    "contract_attribution_records": [
        ("ix_contract_attribution_records_id", ["id"]),
        ("ix_contract_attribution_records_statement_id", ["statement_id"]),
    ],
    "contract_statement_acceptances": [
        ("ix_contract_statement_acceptances_id", ["id"]),
        ("ix_contract_statement_acceptances_statement_id", ["statement_id"]),
    ],
    "contract_disputes": [
        ("ix_contract_disputes_id", ["id"]),
        ("ix_contract_disputes_contract_id", ["contract_id"]),
        ("ix_contract_disputes_statement_id", ["statement_id"]),
    ],
    "machine_telemetry_spans": [
        ("ix_machine_telemetry_spans_id", ["id"]),
        ("ix_machine_telemetry_spans_tenant_code", ["tenant_code"]),
        ("ix_machine_telemetry_spans_machine_id", ["machine_id"]),
        ("ix_machine_telemetry_spans_span_end", ["span_end"]),
        ("ix_machine_telemetry_spans_lookup",
         ["tenant_code", "machine_id", "source", "span_start"]),
    ],
}
_TABLES = list(_INDEXES)


def _tables(bind):
    return set(sa.inspect(bind).get_table_names())


def _create(name):
    if name == "service_contracts":
        op.create_table(
            "service_contracts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("oem_code", sa.String(), nullable=False),
            sa.Column("factory_tenant_code", sa.String(), nullable=False),
            sa.Column("contract_ref", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("contract_type", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="draft"),
            sa.Column("starts_at", sa.DateTime(), nullable=False),
            sa.Column("ends_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("proposed_at", sa.DateTime(), nullable=True),
            sa.Column("factory_accepted_by", sa.String(), nullable=True),
            sa.Column("factory_accepted_at", sa.DateTime(), nullable=True),
            sa.Column("termination_effective_at", sa.DateTime(), nullable=True),
            sa.Column("terminated_by_party", sa.String(), nullable=True),
            sa.Column("terminated_by", sa.String(), nullable=True),
            sa.Column("termination_reason", sa.String(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("oem_code", "contract_ref",
                                name="uq_service_contract_ref"),
        )
    elif name == "service_contract_term_versions":
        op.create_table(
            "service_contract_term_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("contract_id", sa.Integer(),
                      sa.ForeignKey("service_contracts.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("terms_json", sa.Text(), nullable=False),
            sa.Column("terms_hash", sa.String(64), nullable=False),
            sa.Column("effective_from", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="draft"),
            sa.Column("proposed_by_party", sa.String(), nullable=True),
            sa.Column("proposed_by", sa.String(), nullable=True),
            sa.Column("proposed_at", sa.DateTime(), nullable=True),
            sa.Column("oem_accepted_by", sa.String(), nullable=True),
            sa.Column("oem_accepted_at", sa.DateTime(), nullable=True),
            sa.Column("oem_accepted_hash", sa.String(64), nullable=True),
            sa.Column("factory_accepted_by", sa.String(), nullable=True),
            sa.Column("factory_accepted_at", sa.DateTime(), nullable=True),
            sa.Column("factory_accepted_hash", sa.String(64), nullable=True),
            sa.Column("decision_note", sa.String(), nullable=True),
            sa.UniqueConstraint("contract_id", "version",
                                name="uq_contract_term_version"),
        )
    elif name == "service_contract_machines":
        op.create_table(
            "service_contract_machines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("term_version_id", sa.Integer(),
                      sa.ForeignKey("service_contract_term_versions.id"),
                      nullable=False),
            sa.Column("installation_id", sa.Integer(),
                      sa.ForeignKey("machine_installations.id"), nullable=False),
            # Snapshots, deliberately not FKs: offboarding deletes the machine.
            sa.Column("machine_id_at_acceptance", sa.Integer(), nullable=False),
            sa.Column("factory_tenant_at_acceptance", sa.String(), nullable=False),
            sa.Column("serial_number", sa.String(), nullable=False),
            sa.Column("coverage_ended_at", sa.DateTime(), nullable=True),
            sa.Column("coverage_end_reason", sa.String(), nullable=True),
            sa.UniqueConstraint("term_version_id", "installation_id",
                                name="uq_contract_machine_coverage"),
        )
    elif name == "contract_statements":
        op.create_table(
            "contract_statements",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("contract_id", sa.Integer(),
                      sa.ForeignKey("service_contracts.id"), nullable=False),
            sa.Column("term_version_id", sa.Integer(),
                      sa.ForeignKey("service_contract_term_versions.id"),
                      nullable=False),
            sa.Column("period_start", sa.DateTime(), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("content_hash", sa.String(64), nullable=False),
            # NULL only after offboarding; the hash and acceptances are kept.
            sa.Column("canonical_json", sa.Text(), nullable=True),
            sa.Column("computed_at", sa.DateTime(), nullable=False),
            sa.Column("computed_by_party", sa.String(), nullable=False),
            sa.Column("computed_by", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("contract_id", "period_start",
                                name="uq_contract_statement_period"),
        )
    elif name == "contract_attribution_records":
        op.create_table(
            "contract_attribution_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("statement_id", sa.Integer(),
                      sa.ForeignKey("contract_statements.id"), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("installation_id", sa.Integer(), nullable=False),
            sa.Column("start_at", sa.DateTime(), nullable=False),
            sa.Column("end_at", sa.DateTime(), nullable=False),
            sa.Column("seconds", sa.BigInteger(), nullable=False),
            sa.Column("bucket", sa.String(), nullable=False),
            sa.Column("cause", sa.String(), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=False),
            sa.UniqueConstraint("statement_id", "seq",
                                name="uq_attribution_record_seq"),
        )
    elif name == "contract_statement_acceptances":
        op.create_table(
            "contract_statement_acceptances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("statement_id", sa.Integer(),
                      sa.ForeignKey("contract_statements.id"), nullable=False),
            sa.Column("party", sa.String(), nullable=False),
            sa.Column("actor", sa.String(), nullable=False),
            sa.Column("accepted_at", sa.DateTime(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.UniqueConstraint("statement_id", "party", "revision",
                                name="uq_statement_acceptance"),
        )
    elif name == "contract_disputes":
        op.create_table(
            "contract_disputes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("contract_id", sa.Integer(),
                      sa.ForeignKey("service_contracts.id"), nullable=False),
            sa.Column("statement_id", sa.Integer(),
                      sa.ForeignKey("contract_statements.id"), nullable=False),
            sa.Column("installation_id", sa.Integer(), nullable=False),
            sa.Column("window_start", sa.DateTime(), nullable=False),
            sa.Column("window_end", sa.DateTime(), nullable=False),
            sa.Column("raised_by_party", sa.String(), nullable=False),
            sa.Column("raised_by", sa.String(), nullable=False),
            sa.Column("raised_at", sa.DateTime(), nullable=False),
            sa.Column("reason", sa.String(1000), nullable=False),
            sa.Column("proposed_bucket", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="open"),
            sa.Column("resolution_bucket", sa.String(), nullable=True),
            sa.Column("resolution_note", sa.String(1000), nullable=True),
            sa.Column("resolution_proposed_by_party", sa.String(), nullable=True),
            sa.Column("resolution_proposed_by", sa.String(), nullable=True),
            sa.Column("resolution_proposed_at", sa.DateTime(), nullable=True),
            sa.Column("resolution_accepted_by", sa.String(), nullable=True),
            sa.Column("resolution_accepted_at", sa.DateTime(), nullable=True),
            sa.Column("closed_at", sa.DateTime(), nullable=True),
        )
    elif name == "machine_telemetry_spans":
        op.create_table(
            "machine_telemetry_spans",
            sa.Column("id", sa.Integer(), primary_key=True),
            # No server default: a span without its tenant must fail, not
            # land in the founder workspace.
            sa.Column("tenant_code", sa.String(), nullable=False),
            sa.Column("machine_id", sa.Integer(),
                      sa.ForeignKey("machines.id"), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("span_start", sa.DateTime(), nullable=False),
            sa.Column("span_end", sa.DateTime(), nullable=False),
            sa.Column("message_count", sa.Integer(), nullable=False,
                      server_default="1"),
        )
    else:  # pragma: no cover - a table listed in _INDEXES with no definition
        raise RuntimeError(f"0009 has no definition for {name}")


def upgrade() -> None:
    bind = op.get_bind()
    present = _tables(bind)
    for name in _TABLES:
        if name in present:
            continue
        _create(name)
        for index_name, columns in _INDEXES[name]:
            op.create_index(index_name, name, columns)


def downgrade() -> None:
    """Drops the eight tables, children first.

    This DISCARDS every contract, statement, acceptance, dispute and span: the
    record of what both parties agreed. Nothing that existed before 0009 is
    touched, so the factory and OEM schema returns exactly to 0008's shape. Take
    an export before downgrading a database where contracts were signed.

    Indexes are dropped only if present, found by inspection rather than by
    catching the error: on PostgreSQL a failed DROP INDEX aborts the whole
    migration transaction, so try/except would turn a missing index into a
    failed downgrade.
    """
    bind = op.get_bind()
    present = _tables(bind)
    for name in reversed(_TABLES):
        if name not in present:
            continue
        existing = {ix["name"] for ix in sa.inspect(bind).get_indexes(name)}
        for index_name, _ in reversed(_INDEXES[name]):
            if index_name in existing:
                op.drop_index(index_name, table_name=name)
        op.drop_table(name)
