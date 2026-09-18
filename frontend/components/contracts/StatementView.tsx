"use client";

import React from "react";

import {
  BUCKET_TONE,
  bucketLabel,
  bucketShares,
  describeCause,
  evidenceLines,
  formatDuration,
  sharesTileCovered,
  shortHash,
  slaStateLabel,
  type Bucket,
  type Credit,
  type Sla,
  type StatementContent,
  type Totals,
} from "../../lib/contracts";
import { formatDecimalMoney } from "../../lib/money";

/**
 * One downtime attribution statement, as both parties read it (ADR-0021).
 *
 * Every covered second of every covered machine sits in exactly one bucket, and
 * the bar and the legend are drawn from the same totals so they cannot disagree.
 * UNMEASURED is "No data": it is never drawn as uptime or downtime, and a period
 * that could not be evaluated shows NO credit rather than a credit of zero.
 * Money is the server's decimal text, formatted without becoming a float.
 */

function pct(value: string | null) {
  return value === null ? "—" : `${value}%`;
}

export function BucketBar({ totals, label }: { totals: Totals; label: string }) {
  const shares = bucketShares(totals);
  return (
    <div>
      <div
        className="flex h-3 w-full overflow-hidden rounded bg-slate-800"
        role="img"
        aria-label={label}
      >
        {shares.map((s) =>
          s.seconds > 0 ? (
            <div
              key={s.bucket}
              className={BUCKET_TONE[s.bucket]}
              style={{ width: `${s.share * 100}%` }}
            />
          ) : null,
        )}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
        {shares.map((s) => (
          <li key={s.bucket} data-testid={`tile-${s.bucket}`} data-seconds={s.seconds}>
            <span className={`mr-1 inline-block h-2 w-2 rounded-sm ${BUCKET_TONE[s.bucket as Bucket]}`} />
            {bucketLabel(s.bucket)}: {formatDuration(s.seconds)}
          </li>
        ))}
      </ul>
      {!sharesTileCovered(totals) && (
        <p role="alert" className="mt-1 text-xs text-red-400">
          These buckets do not add up to the covered time. Do not accept this statement;
          check its consistency.
        </p>
      )}
    </div>
  );
}

function SlaBlock({ sla }: { sla: Sla }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4 text-sm">
      <p className="text-white font-medium" data-testid="sla-state">
        {slaStateLabel(sla.state)}
      </p>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-slate-400">
        <dt>Target availability</dt>
        <dd>{pct(sla.target_pct)}</dd>
        <dt>Measured (not No data)</dt>
        <dd>{pct(sla.measured_pct)} of covered time, minimum {pct(sla.min_measured_pct)}</dd>
        <dt>Availability</dt>
        <dd data-testid="availability">{pct(sla.availability_pct)}</dd>
      </dl>
      {sla.state === "not_evaluable" && (
        <p className="mt-2 text-xs text-slate-400">
          Not evaluable: {sla.not_evaluable_reason}. No availability and no credit are computed.
        </p>
      )}
      {sla.state === "pending_disputes" && (
        <p className="mt-2 text-xs text-amber-300">
          Disputed time is unresolved. If all of it is the manufacturer&apos;s:{" "}
          {pct(sla.availability_if_disputes_oem)}; if all of it is the factory&apos;s:{" "}
          {pct(sla.availability_if_disputes_factory)}; if all of it was available:{" "}
          {pct(sla.availability_if_disputes_available)}.
        </p>
      )}
    </div>
  );
}

function CreditBlock({ credit, state }: { credit: Credit; state: string | null }) {
  const money = (amount: string | null) => formatDecimalMoney(amount, credit.currency);
  let line: React.ReactNode;
  if (state === "pending_disputes") {
    line = (
      <>Credit depends on the disputes: between {money(credit.range_min)} and{" "}
        {money(credit.range_max)}. No amount until they are resolved.</>
    );
  } else if (credit.amount === null) {
    line = <>No credit is computed for this period.</>;
  } else {
    line = (
      <>Credit: <span className="text-white font-semibold" data-testid="credit-amount">
        {money(credit.amount)}</span> ({pct(credit.credit_pct)} of the period fee
        {credit.tier_below_pct ? `, tier below ${credit.tier_below_pct}%` : ""})</>
    );
  }
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4 text-sm">
      <p className="text-xs text-slate-500">
        Period fee {money(credit.period_fee)} ({credit.currency})
      </p>
      <p className="mt-1 text-slate-300" data-testid="credit">{line}</p>
      <p className="mt-2 text-[11px] text-slate-500">
        AMP computes this figure. It does not invoice, charge or move money.
      </p>
    </div>
  );
}

export default function StatementView({
  content,
  preview = false,
}: {
  content: StatementContent;
  preview?: boolean;
}) {
  return (
    <div className="space-y-4" data-testid="statement-view">
      {preview && (
        <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-200">
          Preview of the running period so far. This is not a statement: it has no hash,
          is not stored, and cannot be accepted.
        </p>
      )}
      <div className="text-xs text-slate-500">
        {content.period.start} to {content.period.end} (UTC; periods follow{" "}
        {content.period.timezone}) · terms version {content.contract.terms_version} ·{" "}
        <span title={content.contract.terms_hash}>terms {shortHash(content.contract.terms_hash)}</span>
      </div>

      <BucketBar totals={content.totals} label="Attribution of all covered time" />

      <div className="grid gap-3 md:grid-cols-2">
        <SlaBlock sla={content.sla} />
        <CreditBlock credit={content.credit} state={content.sla.state} />
      </div>

      {content.machines.map((m) => (
        <details key={m.installation_id} className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
          <summary className="cursor-pointer text-sm text-white">
            <span className="font-mono">{m.serial_number}</span>{" "}
            <span className="text-slate-500 text-xs">
              {formatDuration(m.totals.covered_seconds)} covered
              {m.coverage_end ? ` · coverage ended ${m.coverage_end}` : ""}
            </span>
          </summary>
          <div className="mt-3">
            <BucketBar totals={m.totals} label={`Attribution for ${m.serial_number}`} />
          </div>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-slate-500 uppercase tracking-wide">
                <tr>
                  <th className="p-2 text-left">From</th>
                  <th className="p-2 text-left">To</th>
                  <th className="p-2 text-left">Duration</th>
                  <th className="p-2 text-left">Bucket</th>
                  <th className="p-2 text-left">Why</th>
                  <th className="p-2 text-left">Evidence</th>
                </tr>
              </thead>
              <tbody>
                {m.intervals.map((iv) => (
                  <tr key={`${iv.start}-${iv.bucket}`} className="border-t border-slate-800 align-top">
                    <td className="p-2 font-mono">{iv.start}</td>
                    <td className="p-2 font-mono">{iv.end}</td>
                    <td className="p-2">{formatDuration(iv.seconds)}</td>
                    <td className="p-2">{bucketLabel(iv.bucket)}</td>
                    <td className="p-2 text-slate-300">{describeCause(iv.cause)}</td>
                    <td className="p-2 text-slate-500">
                      {evidenceLines(iv.evidence).map((line) => (
                        <div key={line}>{line}</div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ))}
    </div>
  );
}
