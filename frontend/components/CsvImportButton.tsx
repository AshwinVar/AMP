"use client";

import { useRef, useState } from "react";
import { API_URL, getDownloadHeaders } from "../lib/api";

type ImportResult = {
  created: number;
  updated: number;
  skipped: number;
  errors: string[];
  // How the backend decoded the file. Absent on older deployments.
  encoding?: string;
};

// One-click CSV onboarding: pick a file, POST it multipart to the given import
// endpoint (auth + founder-preview headers), and show the created/updated/
// skipped outcome inline. Used for the machines and suppliers imports — the
// mirror of the Exports card.
export default function CsvImportButton({ label, path }: { label: string; path: string }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState(false);

  const upload = async (file: File) => {
    setBusy(true);
    setResult(null);
    setError(false);
    try {
      const form = new FormData();
      form.append("file", file);
      // getDownloadHeaders carries Authorization (+ X-Tenant preview) WITHOUT a
      // Content-Type — the browser sets the multipart boundary itself.
      const res = await fetch(`${API_URL}${path}`, {
        method: "POST",
        headers: getDownloadHeaders(),
        body: form,
        cache: "no-store",
      });
      if (!res.ok) {
        // The backend rejects a bad upload with a 400 that says what to fix
        // ("This looks like an Excel workbook… Save As and pick CSV"). Showing
        // our own generic line instead would throw that away.
        const detail = await res.json().then((b) => b?.detail).catch(() => null);
        throw new Error(typeof detail === "string" && detail ? detail : `Import failed (${res.status}).`);
      }
      const r = (await res.json()) as ImportResult;
      const errNote = r.errors.length ? ` · ${r.errors.length} row error${r.errors.length === 1 ? "" : "s"}` : "";
      // Flag a non-UTF-8 file: it imported fine, but if the accents look wrong
      // this is why, and re-saving as CSV UTF-8 fixes it.
      const encNote = r.encoding && r.encoding !== "utf-8-sig" ? ` · read as ${r.encoding}` : "";
      setResult(`${r.created} created · ${r.updated} updated · ${r.skipped} skipped${errNote}${encNote}`);
    } catch (e) {
      setError(true);
      setResult(e instanceof Error ? e.message : "Import failed — check the file and your permissions.");
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  return (
    <span className="inline-flex items-center gap-2">
      <input
        ref={inputRef}
        type="file"
        accept=".csv,text/csv"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) upload(f);
        }}
      />
      <button
        type="button"
        disabled={busy}
        onClick={() => inputRef.current?.click()}
        className="rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition disabled:opacity-50"
      >
        {busy ? "Importing…" : `↑ ${label}`}
      </button>
      {result && (
        <span className={`text-xs ${error ? "text-red-400" : "text-emerald-400"}`}>{result}</span>
      )}
    </span>
  );
}
