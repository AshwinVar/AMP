"use client";
import { useState } from "react";
import { apiPost } from "../lib/api";

type Turn = { q: string; a: string; view?: string };

const SUGGESTIONS = [
  "Why is my OEE low?",
  "Which machines are in breakdown?",
  "What should I reorder first?",
  "Are any orders late?",
  "How much are losses costing us?",
  "How are we doing vs last week?",
  "Summarise today's production.",
];

const VIEW_LABEL: Record<string, string> = {
  executive: "Executive OEE", machines: "Machines", inventory: "Inventory",
  orders: "Orders & Dispatch", costing: "Costing", quality: "Quality",
  cmms: "CMMS", downtime: "Downtime", analytics: "Analytics", overview: "Overview",
};

// The AI Factory Copilot: ask a plain-language question about the plant and get an
// answer straight from the live read-models (rule-first, no API key needed), with
// a one-tap drill-in to the view that owns the detail.
export default function AICopilot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [question, setQuestion] = useState("");
  const [thread, setThread] = useState<Turn[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  async function ask(q?: string) {
    const query = (q ?? question).trim();
    if (!query || loading) return;
    setLoading(true); setErr("");
    try {
      const res = await apiPost<{ answer: string; view?: string }>("/copilot/ask", { question: query });
      setThread((t) => [{ q: query, a: res.answer, view: res.view }, ...t]);
      setQuestion("");
    } catch {
      setErr("Couldn't answer that — try rephrasing.");
    }
    setLoading(false);
  }

  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">AI Factory Copilot</h2>
        <p className="text-slate-400 mt-2 text-sm">
          Ask about your live plant — answered instantly from your OEE, cost, delivery, downtime,
          quality, maintenance and stock. No setup needed.
        </p>
      </div>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <div className="flex gap-2">
          <input
            className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-sm flex-1"
            placeholder="e.g. How much are losses costing us this week?"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ask()}
          />
          <button
            onClick={() => ask()}
            disabled={loading}
            className="rounded-xl bg-white text-slate-950 font-semibold px-5 py-3 text-sm disabled:opacity-50"
          >
            {loading ? "Thinking…" : "Ask"}
          </button>
        </div>
        <div className="flex flex-wrap gap-2 mt-3">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              onClick={() => ask(s)}
              disabled={loading}
              className="text-xs text-slate-400 border border-slate-700 rounded-full px-3 py-1.5 hover:border-slate-500 hover:text-white"
            >
              {s}
            </button>
          ))}
        </div>
        {err && <p className="text-red-400 text-sm mt-3">{err}</p>}
      </div>

      <div className="space-y-3">
        {thread.map((t, i) => (
          <div key={i} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
            <p className="text-indigo-300 text-sm font-semibold mb-2">{t.q}</p>
            <p className="text-slate-200 text-sm whitespace-pre-wrap leading-relaxed">{t.a}</p>
            {t.view && onOpen && VIEW_LABEL[t.view] && (
              <button
                onClick={() => onOpen(t.view!)}
                className="mt-3 text-xs text-indigo-300 border border-indigo-500/40 rounded-lg px-3 py-1 hover:bg-indigo-500/10"
              >
                Open {VIEW_LABEL[t.view]} →
              </button>
            )}
          </div>
        ))}
        {thread.length === 0 && !loading && (
          <p className="text-slate-500 text-sm">
            Ask a question above, or tap a suggestion — the copilot reads your live plant and points you at the fix.
          </p>
        )}
      </div>

      <p className="text-slate-600 text-xs">
        Rule-based answers over your live data · set <code className="bg-slate-950 border border-slate-800 rounded px-1.5 py-0.5">ANTHROPIC_API_KEY</code> for free-form conversational answers.
      </p>
    </section>
  );
}
