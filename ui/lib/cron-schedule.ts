/**
 * Pure cron ⇄ schedule-picker helpers (no UI / React deps).
 *
 * Foundation for the timer UI rework where operators pick a trigger time
 * instead of hand-typing a cron expression. This module owns the two
 * lossy-but-safe conversions between a friendly `ScheduleState` and a
 * 5-field crontab string:
 *
 *   composeCron(state) → cron | null   (picker → cron, null disables save)
 *   parseCron(cron)    → state         (cron → picker, never throws)
 *
 * Design note — Sunday is emitted as `0`, never `7`.
 * The browser-side firing preview (`nextFireTime` in
 * `lib/api/scheduler.ts`) matches the day-of-week field against
 * `Date.getDay()`, which only ever yields 0..6. A cron of `* * * * 7`
 * therefore matches *no* real day and previews as "never". So on the way
 * out we normalise 7 → 0; on the way in we accept either and fold 7 → 0.
 */

export type ScheduleMode = "daily" | "weekly" | "advanced";

export interface ScheduleState {
  mode: ScheduleMode;
  /** Wall-clock trigger time, 24h `HH:MM`. Ignored in `advanced` mode. */
  time: string;
  /** Selected weekdays, 0..6 with 0 = Sunday. Only used by `weekly`. */
  weekdays: number[];
  /** Verbatim cron string — the source of truth in `advanced` mode and a
   * loss-free carry-through of whatever `parseCron` was handed. */
  raw: string;
}

/* ------------------------------------------------------------------ */
/*                          Small parsers                             */
/* ------------------------------------------------------------------ */

/** Parse a strict `HH:MM` (1–2 digit hour, exactly 2 digit minute) into
 * numeric fields, or `null` when malformed / out of range. Rejects the
 * spec's bad inputs: `"25:00"` (hour > 23) and `"9:5"` (1-digit minute). */
function parseHHMM(time: string): { h: number; m: number } | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(time.trim());
  if (!match) return null;
  const h = Number.parseInt(match[1], 10);
  const m = Number.parseInt(match[2], 10);
  if (h < 0 || h > 23 || m < 0 || m > 59) return null;
  return { h, m };
}

/** A cron field that is a single, plain non-negative integer, else null.
 * Rejects `*`, step fields, ranges, and lists — those force `advanced`. */
function parsePlainInt(spec: string): number | null {
  if (!/^\d+$/.test(spec)) return null;
  return Number.parseInt(spec, 10);
}

/** Parse a day-of-week field as a comma list of plain ints in 0..7, or
 * `null` when any element is a step / range / out-of-range value. */
function parseDowList(spec: string): number[] | null {
  const out: number[] = [];
  for (const part of spec.split(",")) {
    if (!/^\d+$/.test(part)) return null;
    const n = Number.parseInt(part, 10);
    if (n < 0 || n > 7) return null;
    out.push(n);
  }
  return out;
}

/** Fold weekdays into the canonical set: 7 → 0 (both mean Sunday),
 * clamp to 0..6, dedupe, and sort ascending for stable output. */
function normalizeWeekdays(days: readonly number[]): number[] {
  const set = new Set<number>();
  for (const d of days) {
    const n = d === 7 ? 0 : d;
    if (Number.isInteger(n) && n >= 0 && n <= 6) set.add(n);
  }
  return [...set].sort((a, b) => a - b);
}

/* ------------------------------------------------------------------ */
/*                          Public API                                */
/* ------------------------------------------------------------------ */

/**
 * Build a 5-field cron from picker state, or `null` when the state can't
 * produce a valid expression (callers disable the save button on `null`).
 *
 *   daily    → `M H * * *`
 *   weekly   → `M H * * d,d,…`   (Sunday emitted as 0, never 7)
 *   advanced → `raw` verbatim
 */
export function composeCron(state: ScheduleState): string | null {
  if (state.mode === "advanced") {
    // Verbatim pass-through — advanced is the escape hatch for anything
    // the picker can't express.
    return state.raw;
  }

  const hm = parseHHMM(state.time);
  if (hm === null) return null;
  const { h, m } = hm;

  if (state.mode === "daily") {
    return `${m} ${h} * * *`;
  }

  // weekly
  const days = normalizeWeekdays(state.weekdays);
  if (days.length === 0) return null;
  return `${m} ${h} * * ${days.join(",")}`;
}

/**
 * Reverse a cron string back into picker state. Never throws — anything
 * the picker can't represent round-trips through `advanced` with `raw`
 * preserved so the operator's original expression is never lost.
 *
 *   `M H * * *`      (plain int M/H)             → daily
 *   `M H * * d,d,…`  (plain int M/H, int dow列)  → weekly (7 → 0)
 *   everything else  (steps, ranges, 6-field, …) → advanced
 */
export function parseCron(cron: string): ScheduleState {
  const advanced = (): ScheduleState => ({
    mode: "advanced",
    time: "00:00",
    weekdays: [],
    raw: cron,
  });

  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return advanced();

  const [minSpec, hourSpec, domSpec, monSpec, dowSpec] = parts;

  const m = parsePlainInt(minSpec);
  const h = parsePlainInt(hourSpec);
  if (m === null || h === null) return advanced();
  if (m > 59 || h > 23) return advanced();
  if (domSpec !== "*" || monSpec !== "*") return advanced();

  const time = `${pad2(h)}:${pad2(m)}`;

  if (dowSpec === "*") {
    return { mode: "daily", time, weekdays: [], raw: cron };
  }

  const days = parseDowList(dowSpec);
  if (days === null) return advanced();
  return { mode: "weekly", time, weekdays: normalizeWeekdays(days), raw: cron };
}

/** Zero-pad a 0..59 field to two digits for `HH:MM` display. */
function pad2(n: number): string {
  return String(n).padStart(2, "0");
}
