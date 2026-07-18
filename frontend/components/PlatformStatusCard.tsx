"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend platform self-report (ai/platform_status.py).
type PlatformStatus = {
  read_models: string[];
  read_model_count: number;
  agents: string[];
  agent_count: number;
  copilot: { rule_based: boolean; llm_enabled: boolean };
  agent_actions_logged: number;
};

// The AI platform's self-report: what's wired up — every registered read-model,
// the agent workforce, and copilot connectivity. Self-contained; renders nothing
// until the status loads.
export default function PlatformStatusCard() {
  const [s, setS] = useState<PlatformStatus | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<PlatformStatus>("/platform/status"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (!s) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">AI platform</h3>
          <p className="text-slate-400 text-sm mt-1">
            {s.read_model_count} read-models · {s.agent_count} agents · {s.agent_actions_logged.toLocaleString()} agent actions logged
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-xs text-emerald-300">
            Copilot: rule-based on
          </span>
          <span className={`rounded-md border px-2.5 py-1 text-xs ${s.copilot.llm_enabled ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300" : "border-slate-700 bg-slate-800 text-slate-400"}`}>
            LLM: {s.copilot.llm_enabled ? "connected" : "optional, off"}
          </span>
        </div>
      </div>

      <div className="mt-4">
        <p className="text-xs text-slate-500 mb-2">Agents</p>
        <div className="flex flex-wrap gap-2">
          {s.agents.map((a) => (
            <span key={a} className="rounded-md border border-indigo-500/30 bg-indigo-500/10 px-2.5 py-1 text-xs text-indigo-300 capitalize">
              {a}
            </span>
          ))}
        </div>
      </div>

      <div className="mt-4">
        <p className="text-xs text-slate-500 mb-2">Read-models</p>
        <div className="flex flex-wrap gap-1.5">
          {s.read_models.map((r) => (
            <span key={r} className="rounded border border-slate-700 bg-slate-800/70 px-2 py-0.5 text-[11px] text-slate-300">
              {r}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
