"use client";

import { useCallback, useState } from "react";

/**
 * Failure state for a write, so a save that did not happen cannot look like one
 * that did.
 *
 * Every create/update/delete handler on the dashboard ended the same way:
 *
 *     } catch (error) {
 *       console.error(error);
 *     }
 *
 * The user clicks Add Machine, the request 400s, and nothing at all appears.
 * The form keeps its values, no row shows up, and the only account of what
 * happened is in a console nobody on a shop floor has open. This is the write
 * side of the same bug `useLoadError` fixed for reads (#383).
 *
 * It also throws away work the backend does. #382 made the ingest endpoints
 * answer a bad field with a 400 that names it — "quantity must be a whole
 * number" — and that message was being dropped on the floor.
 */

const MAX_DETAIL = 160;

function truncate(text: string) {
  return text.length > MAX_DETAIL ? `${text.slice(0, MAX_DETAIL).trimEnd()}…` : text;
}

/**
 * Dig the useful sentence out of whatever lib/api.ts threw.
 *
 * Two shapes reach here: apiPost/apiPut throw the raw response body, while
 * apiGet/apiPatch/apiDelete throw `Failed request: <path> | <status> | <body>`.
 * Either way the part worth showing is FastAPI's `detail`, which is a string
 * for a hand-written HTTPException and a list of {loc, msg} for a validation
 * error.
 */
function extractDetail(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;

  const parts = trimmed.split(" | ");
  const body = parts.length >= 3 ? parts.slice(2).join(" | ") : trimmed;

  try {
    const parsed = JSON.parse(body);
    const detail = (parsed as { detail?: unknown })?.detail;

    if (typeof detail === "string") return detail.trim() || null;

    if (Array.isArray(detail)) {
      const messages = detail
        .map((entry) => (entry as { msg?: unknown })?.msg)
        .filter((msg): msg is string => typeof msg === "string" && msg.trim() !== "");
      if (messages.length > 0) return messages.join("; ");
    }

    return null;
  } catch {
    // Not JSON — an HTML error page or a proxy message. Nothing quotable.
    return null;
  }
}

export function describeActionFailure(error: unknown, what: string): string {
  const raw = error instanceof Error ? error.message : String(error ?? "");

  // fetch() rejects with a TypeError when it never reached the server at all,
  // which is a different instruction to the user than a rejected request.
  if (/failed to fetch|networkerror|network error|load failed/i.test(raw)) {
    return `Could not ${what} — no response from the server. Check your connection and try again.`;
  }

  const detail = extractDetail(raw);
  if (detail) return `Could not ${what}: ${truncate(detail)}`;

  return `Could not ${what}. Please try again.`;
}

export function useActionError() {
  const [error, setError] = useState<string | null>(null);

  const report = useCallback((error: unknown, what: string) => {
    setError(describeActionFailure(error, what));
  }, []);

  const clear = useCallback(() => setError(null), []);

  return { error, report, clear };
}

export function ActionError({
  message,
  onDismiss,
}: {
  message: string | null;
  onDismiss: () => void;
}) {
  if (!message) return null;

  return (
    <div
      role="alert"
      className="mb-4 flex items-start justify-between gap-4 rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3"
    >
      <p className="text-sm text-red-300">{message}</p>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss error"
        className="text-red-300/70 hover:text-red-200 text-sm"
      >
        ✕
      </button>
    </div>
  );
}
