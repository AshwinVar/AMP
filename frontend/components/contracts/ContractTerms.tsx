"use client";

import React from "react";

import { bucketLabel, formatDuration, shortHash, type TermVersion } from "../../lib/contracts";
import { formatDecimalMoney } from "../../lib/money";

/**
 * The terms of one version of a contract, as either party reads them (ADR-0020).
 *
 * Shown in full to BOTH parties before anything binds them, including the one
 * limitation that matters most: the status sources a contract trusts are
 * provisioned or posted by the FACTORY, and AMP does not authenticate them as
 * coming from the machine's maker. Disputes are the remedy, and the terms say so
 * where they are read, not only in a document nobody opens.
 */

const DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-slate-500">{label}</dt>
      <dd className="text-slate-200">{children}</dd>
    </>
  );
}

export default function ContractTerms({ version }: { version: TermVersion }) {
  const t = version.terms;
  const every = t.period_months === 1 ? "month" : `${t.period_months} months`;
  return (
    <div className="space-y-4 text-xs" data-testid={`terms-v${version.version}`}>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
        <Row label="Version">
          {version.version} · {version.status}
          {version.effective_from ? ` · from ${version.effective_from}` : ""} ·{" "}
          <span className="font-mono" title={version.terms_hash}>{shortHash(version.terms_hash)}</span>
        </Row>
        <Row label="Fee">
          {formatDecimalMoney(t.period_fee, t.currency)} per {every}, for {t.term_months} months
        </Row>
        <Row label="Periods">whole calendar periods in {t.timezone}</Row>
        <Row label="Covered hours">
          {t.coverage.mode === "24x7" ? "24 hours a day, 7 days a week" : (
            <>
              {t.coverage.windows.map((w) => (
                <div key={`${w.days.join("")}-${w.start}`}>
                  {w.days.map((d) => DAY[d]).join(", ")} {w.start} to {w.end}
                </div>
              ))}
              {t.coverage.excluded_dates && t.coverage.excluded_dates.length > 0 && (
                <div>except {t.coverage.excluded_dates.join(", ")}</div>
              )}
            </>
          )}
        </Row>
        <Row label="SLA">
          availability of at least {t.sla_target_pct}%, evaluated only when at least{" "}
          {t.min_measured_pct}% of covered time has data
        </Row>
        <Row label="Credits">
          {t.credit_tiers.map((tier) => (
            <div key={tier.below_pct}>below {tier.below_pct}%: {tier.credit_pct}% of the fee</div>
          ))}
        </Row>
        <Row label="Trusted status sources">
          {t.trusted_sources.join(", ")}
          <p className="mt-1 text-amber-200/90" data-testid="trust-note">
            Statuses from these sources are provisioned or posted by the factory. They are not
            authenticated as coming from the manufacturer. If either party believes a status is
            wrong, the remedy is a dispute.
          </p>
        </Row>
        <Row label="Down with no reason">
          {Object.entries(t.status_defaults).map(([status, bucket]) => (
            <div key={status}>{status}: {bucketLabel(bucket)}</div>
          ))}
        </Row>
        <Row label="Reasons">
          {Object.entries(t.reason_map).map(([reason, bucket]) => (
            <div key={reason}>&ldquo;{reason}&rdquo;: {bucketLabel(bucket)}</div>
          ))}
          <div className="text-slate-500">
            Any other reason is Disputed. Treated as no reason: {t.generic_reasons.join(", ")}.
            A reason counts if logged up to {formatDuration(t.reason_lead_seconds)} before a stop.
          </div>
        </Row>
        <Row label="Termination notice">{t.termination_notice_days} days, at a period boundary</Row>
        <Row label="Covered machines">
          {t.covered_installations.map((c) => (
            <div key={c.installation_id} className="font-mono">{c.serial_number}</div>
          ))}
        </Row>
      </dl>
    </div>
  );
}
