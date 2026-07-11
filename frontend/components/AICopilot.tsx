"use client";
import React, { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

interface Status { enabled: boolean; model: string | null; }
interface Turn { q: string; a: string; }

const SUGGESTIONS = [
  "Why is my OEE low?",
  "Which machines are in breakdown?",
  "What should I reorder first?",
  "Summarise today's production.",
];

export default function AICopilot() {
  const [status, setStatus] = useState<Status | null>(null);
  const [question, setQuestion] = useState("");
  const [thread, setThread] = useState<Turn[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    apiGet<Status>("/ai/status").then(setStatus).catch(() => setStatus({ enabled: false, model: null }));
  }, []);

  async function ask(q?: string) {
    const query = (q ?? question).trim();
    if (!query || loading) return;
    setLoading(true); setErr("");
    try {
      const res = await apiPost<{ answer: string }>("/ai/ask", { question: query });
      setThread((t) => [{ q: query, a: res.answer }, ...t]);
      setQuestion("");
    } catch (e: any) {
      setErr(e?.message?.replace(/^POST .* failed: \d+ /, "") || "AI request failed");
    }
    setLoading(false);
  }

  async function report() {
    if (loading) return;
    setLoading(true); setErr("");
    try {
      const res = await apiPost<{ report: string }>("/ai/report", {});
      setThread((t) => [{ q: "Generate today's management report", a: res.report }, ...t]);
    } catch (e: any) {
      setErr(e?.message?.replace(/^POST .* failed: \d+ /, "") || "AI request failed");
    }
    setLoading(false);
  }

  // ── Not connected: show the (very simple) connect instructions ──
  if (status && !status.enabled) {
    return (
      <section className="mt-8 space-y-6">
        <div>
          <h2 className="text-3xl font-bold flex items-center gap-3">
            AI Factory Copilot
            <span className="rounded-lg bg-yellow-500/15 border border-yellow-500/40 px-3 py-1 text-xs text-yellow-300 font-semibold tracking-wider">NOT CONNECTED</span>
          </h2>
          <p className="text-slate-400 mt-2 text-sm">A natural-language assistant over your live factory data. Built and ready — it just needs an API key.</p>
        </div>

        <div className="rounded-2xl bg-slate-900 border border-slate-800 p-6">
          <h3 className="text-lg font-semibold mb-3">Connect in 3 steps (~2 minutes)</h3>
          <ol className="space-y-3 text-sm text-slate-300 list-decimal ml-5">
            <li>Create an API key at <span className="text-indigo-300">console.anthropic.com</span> (free trial credits to start).</li>
            <li>In Railway → your <strong>AMP</strong> service → <strong>Variables</strong>, add: <code className="bg-slate-950 border border-slate-700 rounded px-2 py-0.5 text-indigo-300">ANTHROPIC_API_KEY</code> = your key</li>
            <li>Save — Railway redeploys and the copilot switches on automatically.</li>
          </ol>
          <p className="text-slate-500 text-xs mt-4">
            Optional: set <code className="bg-slate-950 border border-slate-700 rounded px-1.5 py-0.5">AI_MODEL</code> to change the model (default <span className="text-slate-400">claude-haiku-4-5</span> — cheapest/fastest, ~₹0.45 per question). No code change needed to connect.
          </p>
        </div>

        <div className="rounded-2xl bg-slate-900 border border-slate-800 p-6 opacity-60">
          <p className="text-slate-400 text-sm mb-3">Once connected, you'll be able to ask things like:</p>
          <div className="flex flex-wrap gap-2">
            {SUGGESTIONS.map((s) => (
              <span key={s} className="text-sm text-slate-400 border border-slate-700 rounded-full px-3 py-1.5">{s}</span>
            ))}
          </div>
        </div>
      </section>
    );
  }

  // ── Connected: the live chat ──
  return (
    <section className="mt-8 space-y-6">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-3xl font-bold">AI Factory Copilot</h2>
          <p className="text-slate-400 mt-2 text-sm">Ask anything about your live factory data {status?.model && <span className="text-slate-500">· {status.model}</span>}</p>
        </div>
        <button onClick={report} disabled={loading} className="rounded-xl border border-indigo-500/40 text-indigo-300 px-4 py-2 text-sm hover:bg-indigo-500/10 disabled:opacity-50">
          Generate daily report
        </button>
      </div>

      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <div className="flex gap-2">
          <input
            className="bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-sm flex-1"
            placeholder="e.g. Why is OEE below target on CNC-02?"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ask()}
          />
          <button onClick={() => ask()} disabled={loading} className="rounded-xl bg-white text-slate-950 font-semibold px-5 py-3 text-sm disabled:opacity-50">
            {loading ? "Thinking…" : "Ask"}
          </button>
        </div>
        <div className="flex flex-wrap gap-2 mt-3">
          {SUGGESTIONS.map((s) => (
            <button key={s} onClick={() => ask(s)} disabled={loading} className="text-xs text-slate-400 border border-slate-700 rounded-full px-3 py-1.5 hover:border-slate-500 hover:text-white">
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
          </div>
        ))}
        {thread.length === 0 && !loading && (
          <p className="text-slate-500 text-sm">Ask a question above, or tap a suggestion. The copilot answers from your live machines, OEE, downtime, shifts and stock.</p>
        )}
      </div>
    </section>
  );
}
