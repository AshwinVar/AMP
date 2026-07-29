/**
 * Duration parsing — the frontend half of a pair.
 *
 * `downtime_logs.duration` is free text ("2 hrs 15 min", "45 min", a bare "90",
 * a decimal "1.5 hrs"). The backend reads it with
 * `backend/duration.py:parse_duration_to_minutes` to produce OEE, the
 * cost-of-losses figure and the risk scores. The dashboard reads the same rows
 * to render its Total Downtime tile. The two have to agree, so this is a
 * deliberate port of that function rather than an independent implementation —
 * `lib/duration.test.ts` pins it against the backend's actual output.
 *
 * The number is matched as a decimal, because an integer-only `\d+` skips the
 * "1." and matches the "5" in "1.5 h", reading a 90-minute stop as 5 hours.
 * That is the bug the backend fixed and this side had kept.
 */

const NUM = String.raw`(\d+(?:\.\d+)?)`;

const HOUR_RE = new RegExp(`${NUM}\\s*h`);
const MINUTE_RE = new RegExp(`${NUM}\\s*m`);
const PLAIN_RE = new RegExp(NUM);

// Python's round() breaks ties to even: round(2.5) is 2, round(3.5) is 4.
// Math.round would give 3 and 4. Matching it keeps the tile and the API on the
// same minute for every input, not just most of them.
//
// Exported because it is not a duration rule, it is the Python rule — anything
// this codebase ports from a `round(...)` in backend/ needs it. lib/shift.ts is
// the second caller: a shift that made 1 of a target 40 is exactly 2.5%, where
// Math.round says 3% and the API says 2%.
export function roundHalfToEven(n: number) {
  const floor = Math.floor(n);
  const remainder = n - floor;

  if (remainder > 0.5) return floor + 1;
  if (remainder < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

export function parseDurationToMinutes(value: string | null | undefined) {
  if (!value) return 0;

  const lower = String(value).toLowerCase();
  let total = 0;

  const hourMatch = lower.match(HOUR_RE);
  const minuteMatch = lower.match(MINUTE_RE);

  if (hourMatch) total += Number(hourMatch[1]) * 60;
  if (minuteMatch) total += Number(minuteMatch[1]);

  // No unit at all — treat a bare number (integer or decimal) as minutes.
  if (!hourMatch && !minuteMatch) {
    const plain = lower.match(PLAIN_RE);
    total += plain ? Number(plain[1]) : 0;
  }

  return roundHalfToEven(total);
}
