"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";

import {
  compareToBaseline,
  dataLabel,
  describeAnomaly,
  describeAnomalyError,
  describeSweepRow,
  describeFailureRisk,
  formatDifference,
  formatInterval,
  formatMetricValue,
  modelIdentity,
  verdictBadge,
  type AnomalyResult,
  type AnomalySweep,
  type AnomalyView,
  type FailureRiskResponse,
  type HeadlineRow,
  type ModelCard,
  type ModelCardsResponse,
} from "../lib/aiModels";
import { apiGet, getUserRole } from "../lib/api";
import { LoadError, useLoadError } from "../lib/useLoadError";

/**
 * AMP-native model cards (ADR-0020).
 *
 * A card shows what the evaluation showed and nothing more: the verdict
 * (Adopted / Experimental / Unavailable), that the numbers come from SYNTHETIC
 * data, and every headline metric next to the baseline it was measured against
 * on the same held-out data, with the 95% interval where there is one. The
 * reasons a model was not adopted are shown, not hidden, and so are its known
 * limitations and its stress-test rows: a card that showed only the rows a model
 * does well on would say more than its evaluation did. The wording is decided in
 * lib/aiModels.ts and backend/amp_ai/registry.py, where it is tested.
 */

const BADGE_STYLE = {
  adopted: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  experimental: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  unavailable: "border-slate-600/40 bg-slate-500/10 text-slate-400",
} as const;

const VERDICT_STYLE = {
  better: "text-emerald-300",
  worse: "text-red-300",
  unclear: "text-slate-400",
  equal: "text-slate-400",
} as const;

function MetricTable({ rows, label }: { rows: HeadlineRow[]; label: string }) {
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-xs" aria-label={label}>
        <thead>
          <tr className="text-slate-500 text-left">
            <th className="font-normal py-1 pr-3">Metric</th>
            <th className="font-normal py-1 pr-3">Model</th>
            <th className="font-normal py-1 pr-3">Baseline</th>
            <th className="font-normal py-1 pr-3">Difference</th>
            <th className="font-normal py-1">Reading</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => {
            const unit = row.unit ?? "score";
            const cmp = compareToBaseline(row);
            return (
              <tr key={`${row.metric}-${row.dataset}-${i}`} className="border-t border-slate-800 align-top">
                <td className="py-1.5 pr-3 text-slate-300">
                  {row.metric}
                  <span className="block text-[10px] text-slate-500">{row.dataset}</span>
                </td>
                <td className="py-1.5 pr-3 text-slate-200 whitespace-nowrap">
                  {formatMetricValue(row.model, unit)}
                  <span className="block text-[10px] text-slate-500">{formatInterval(row.model_lo, row.model_hi, unit)}</span>
                </td>
                <td className="py-1.5 pr-3 text-slate-300 whitespace-nowrap">
                  {formatMetricValue(row.baseline, unit)}
                  <span className="block text-[10px] text-slate-500">{row.baseline_name}</span>
                </td>
                <td className="py-1.5 pr-3 text-slate-300 whitespace-nowrap">
                  {formatDifference(row.difference, unit)}
                  <span className="block text-[10px] text-slate-500">
                    {formatInterval(row.difference_lo, row.difference_hi, unit, true)}
                  </span>
                </td>
                <td className={`py-1.5 ${VERDICT_STYLE[cmp.verdict]}`}>{cmp.text}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function AIModelCard({ card, children }: { card: ModelCard; children?: ReactNode }) {
  const badge = verdictBadge(card);
  const limitations = card.limitations ?? [];
  const stress = card.misspecification_rows ?? [];
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900 p-4">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-slate-100">{card.title}</p>
          <p className="text-[11px] text-slate-500 mt-0.5">{modelIdentity(card)}</p>
        </div>
        <span className={`rounded-full px-2.5 py-0.5 text-[11px] border ${BADGE_STYLE[badge.tone]}`}>{badge.label}</span>
      </div>
      <p className="text-[11px] uppercase tracking-wide text-amber-300/80 mt-2">{dataLabel(card)}</p>
      <p className="text-xs text-slate-400 mt-2">{card.purpose}</p>
      <p className="text-xs text-slate-500 mt-1">{card.default_behaviour}</p>

      {!card.available && (
        <p role="alert" className="text-xs text-red-300 mt-3">
          This model&apos;s file failed its integrity check, so no numbers are shown: {card.reason}
        </p>
      )}

      {card.available && card.headline.length > 0 && <MetricTable rows={card.headline} label="Held-out results" />}

      {card.available && card.adopted !== true && card.reasons.length > 0 && (
        <div className="mt-3">
          <p className="text-[11px] text-slate-500">Why it is not adopted</p>
          <ul className="list-disc pl-4 text-xs text-slate-400 mt-1 space-y-0.5">
            {card.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {card.available && limitations.length > 0 && (
        <div className="mt-3">
          <p className="text-[11px] text-slate-500">Known limitations</p>
          <ul className="list-disc pl-4 text-xs text-slate-400 mt-1 space-y-0.5">
            {limitations.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        </div>
      )}

      {card.available && stress.length > 0 && (
        <div className="mt-3">
          <p className="text-[11px] text-slate-500">Stress tests: the same comparison on deliberately misspecified synthetic data</p>
          <MetricTable rows={stress} label="Stress tests" />
        </div>
      )}

      <details className="mt-3 text-[11px] text-slate-500">
        <summary className="cursor-pointer">Provenance</summary>
        <dl className="mt-2 space-y-1">
          <div>
            <dt className="inline text-slate-500">Training data: </dt>
            <dd className="inline text-slate-400">{card.training_data_source ?? "—"}</dd>
          </div>
          <div>
            <dt className="inline text-slate-500">Consent basis: </dt>
            <dd className="inline text-slate-400">{card.consent_basis ?? "—"}</dd>
          </div>
          <div>
            <dt className="inline text-slate-500">Built: </dt>
            <dd className="inline text-slate-400">{card.created_at ?? "—"}</dd>
          </div>
          <div>
            <dt className="inline text-slate-500">Caveat: </dt>
            <dd className="inline text-slate-400">{card.caveat}</dd>
          </div>
        </dl>
      </details>
      {children}
    </div>
  );
}

type Machine = { id: number; name: string };

/** Run the (consent-gated, experimental) anomaly check for one machine. Never styled as an alert. */
function AnomalyCheck() {
  const [machines, setMachines] = useState<Machine[]>([]);
  const [machineId, setMachineId] = useState<string>("");
  const [view, setView] = useState<AnomalyView | null>(null);
  const [busy, setBusy] = useState(false);
  const [fleet, setFleet] = useState<AnomalySweep | null>(null);
  const [sweeping, setSweeping] = useState(false);
  const [sweepError, setSweepError] = useState<string | null>(null);
  const { error, track } = useLoadError();

  useEffect(() => {
    track(apiGet<Machine[]>("/machines"), setMachines, "machines");
  }, [track]);

  const run = useCallback(async () => {
    if (!machineId) return;
    setBusy(true);
    try {
      setView(describeAnomaly(await apiGet<AnomalyResult>(`/ai/native/anomaly/machines/${encodeURIComponent(machineId)}`)));
    } catch (e) {
      setView(describeAnomalyError(e));
    } finally {
      setBusy(false);
    }
  }, [machineId]);

  const sweep = useCallback(async () => {
    setSweeping(true);
    try {
      setFleet(await apiGet<AnomalySweep>("/ai/native/anomaly/sweep"));
      setSweepError(null);
    } catch (e) {
      // A refusal (no consent, or a preview) is an ANSWER, not a blank: the
      // wording helper already knows how to say each one.
      setFleet(null);
      setSweepError(describeAnomalyError(e).text);
    } finally {
      setSweeping(false);
    }
  }, []);

  return (
    <div className="mt-3 border-t border-slate-800 pt-3">
      <LoadError message={error} />
      <div className="flex gap-2 flex-wrap items-center">
        <select
          aria-label="Machine to check"
          value={machineId}
          onChange={(e) => setMachineId(e.target.value)}
          className="bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 text-xs text-slate-200"
        >
          <option value="">Choose a machine…</option>
          {machines.map((m) => (
            <option key={m.id} value={String(m.id)}>
              {m.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={run}
          disabled={!machineId || busy}
          className="rounded-lg border border-slate-600 text-slate-300 text-xs px-3 py-1 disabled:opacity-50"
        >
          {busy ? "Checking…" : "Check the last hour"}
        </button>
        {/* ADR-0032: the same check over every machine, so a plant is not asked
            to work through a dropdown to know it has looked at them all. */}
        <button
          type="button"
          onClick={sweep}
          disabled={sweeping}
          className="rounded-lg border border-slate-600 text-slate-300 text-xs px-3 py-1 disabled:opacity-50"
        >
          {sweeping ? "Checking the fleet…" : "Check every machine"}
        </button>
      </div>
      {sweepError && <p className="text-xs text-slate-300 mt-2">{sweepError}</p>}
      {fleet && (
        <div className="mt-2 rounded-lg border border-slate-700 bg-slate-950/60 p-2">
          <p className="text-xs text-slate-300">{fleet.headline}</p>
          {fleet.machines.length > 0 && (
            <ul className="mt-2 space-y-1">
              {/* Every machine, scored or not. A row with no score shows its
                  REASON: dropping it would read as a machine that was fine. */}
              {fleet.machines.map(describeSweepRow).map((row) => (
                <li key={row.id} className="text-xs flex flex-wrap items-baseline gap-x-2">
                  <span className="text-slate-300">{row.name}</span>
                  <span className={row.muted ? "text-slate-500" : "text-slate-200 tabular-nums"}>
                    {row.value}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p className="text-[11px] text-slate-500 mt-2">{fleet.note}</p>
        </div>
      )}
      {view && (
        <div className="mt-2 rounded-lg border border-slate-700 bg-slate-950/60 p-2">
          <p className="text-xs text-slate-300">{view.text}</p>
          {view.detail && <p className="text-[11px] text-slate-500 mt-1">{view.detail}</p>}
        </div>
      )}
    </div>
  );
}

/**
 * The failure-risk model's per-machine estimates (ADR-0027).
 *
 * The model has had a card here since ADR-0020, but its actual output had no
 * screen at all — it existed only at /ai/native/failure-risk. Showing it matters
 * as much as HOW it is shown: it is loaded on request rather than on every
 * dashboard, each estimate sits beside the rule score for the same machine, and
 * nothing is coloured as an alarm. A number styled red would say "act on this",
 * which an evaluation on synthetic machines cannot support.
 */
function FailureRiskScores() {
  const [view, setView] = useState<ReturnType<typeof describeFailureRisk> | null>(null);
  const [busy, setBusy] = useState(false);
  const { error, track } = useLoadError();

  const run = useCallback(() => {
    setBusy(true);
    track(apiGet<FailureRiskResponse>("/ai/native/failure-risk"), (r) => setView(describeFailureRisk(r)),
      "failure risk").finally(() => setBusy(false));
  }, [track]);

  return (
    <div className="mt-3 border-t border-slate-800 pt-3">
      <LoadError message={error} />
      <button
        type="button"
        onClick={run}
        disabled={busy}
        className="rounded-lg border border-slate-600 text-slate-300 text-xs px-3 py-1 disabled:opacity-50"
      >
        {busy ? "Scoring…" : "Show this model's estimates"}
      </button>
      {view && (
        <div className="mt-2">
          <p className="text-[11px] text-slate-500">{view.headline}</p>
          {view.unavailable && <p className="text-xs text-slate-300 mt-2">{view.unavailable}</p>}
          {view.rows.length > 0 && (
            <table className="w-full text-xs mt-2" aria-label="Failure-risk model estimates">
              <thead>
                <tr className="text-slate-500 text-left">
                  <th className="font-normal py-1 pr-3">Machine</th>
                  <th className="font-normal py-1 pr-3">Model estimate</th>
                  <th className="font-normal py-1 pr-3">Band</th>
                  <th className="font-normal py-1">Rule score</th>
                </tr>
              </thead>
              <tbody>
                {view.rows.map((r) => (
                  <tr key={r.id} className="border-t border-slate-800">
                    <td className="py-1 pr-3 text-slate-300">{r.name}</td>
                    {/* Deliberately not colour-coded: see the doc comment. */}
                    <td className="py-1 pr-3 text-slate-300 tabular-nums">{r.estimate}</td>
                    <td className="py-1 pr-3 text-slate-400">{r.band}</td>
                    <td className="py-1 text-slate-400 tabular-nums">{r.rule}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {view.excluded && <p className="text-[11px] text-slate-500 mt-2">{view.excluded}</p>}
        </div>
      )}
    </div>
  );
}

/** Every registered AMP-native model's card. Hidden (not an error) when the plan does not include it. */
export default function AINativeModelsPanel() {
  const [data, setData] = useState<ModelCardsResponse | null>(null);
  const role = getUserRole();
  const canCheck = role === "Admin" || role === "Supervisor";

  useEffect(() => {
    // Feature detection, like the copilot's /ai/status: a plan without the
    // Intelligence Pack answers 403, and then there is simply no panel.
    apiGet<ModelCardsResponse>("/ai/models").then(setData).catch(() => {});
  }, []);

  if (!data || data.models.length === 0) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">AMP-native models</h3>
      <p className="text-slate-400 text-sm mt-1">
        Built into AMP, no external AI service. Each is shown against its baseline on the same held-out data, with its
        known limitations. {data.caveat}
      </p>
      <div className="mt-4 space-y-3">
        {data.models.map((card) => (
          <AIModelCard key={card.name} card={card}>
            {card.name === "telemetry_anomaly" && card.available && canCheck && <AnomalyCheck />}
            {card.name === "failure_risk" && card.available && canCheck && <FailureRiskScores />}
          </AIModelCard>
        ))}
      </div>
    </div>
  );
}
