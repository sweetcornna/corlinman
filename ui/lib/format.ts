/**
 * Locale-aware date/number formatting tied to the ACTIVE UI language.
 *
 * Every user-visible timestamp/number must render in the language the
 * operator selected in the UI — not the OS/browser locale. Bare
 * `toLocaleString()` calls drift to the system locale (zh UI showing
 * "Jul 18, 09:00 AM" and vice versa); route them through these helpers
 * instead. Components re-render on language change via `useTranslation`,
 * so reading `i18next.language` at render time stays current.
 */

import { i18next } from "@/lib/i18n";

/** BCP-47 tag for the active UI language. */
export function uiLocale(): string {
  return i18next.language === "en" ? "en-US" : "zh-CN";
}

type DateInput = Date | string | number | null | undefined;

function toDate(input: DateInput): Date | null {
  if (input === null || input === undefined || input === "") return null;
  if (input instanceof Date) return Number.isNaN(input.getTime()) ? null : input;
  if (typeof input === "number") {
    // Epoch seconds vs milliseconds.
    const ms = input < 1e12 ? input * 1_000 : input;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const trimmed = input.trim();
  if (/^\d+$/.test(trimmed)) return toDate(Number(trimmed));
  const d = new Date(trimmed);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Full date + time in the UI language and the viewer's local timezone. */
export function formatDateTime(
  input: DateInput,
  opts?: Intl.DateTimeFormatOptions,
): string {
  const d = toDate(input);
  if (d === null) return "—";
  try {
    return d.toLocaleString(uiLocale(), opts);
  } catch {
    return d.toISOString();
  }
}

/** Date only, UI language. */
export function formatDate(
  input: DateInput,
  opts?: Intl.DateTimeFormatOptions,
): string {
  const d = toDate(input);
  if (d === null) return "—";
  try {
    return d.toLocaleDateString(uiLocale(), opts);
  } catch {
    return d.toISOString().slice(0, 10);
  }
}

/** Time-of-day only, UI language, viewer's local timezone. */
export function formatTime(
  input: DateInput,
  opts?: Intl.DateTimeFormatOptions,
): string {
  const d = toDate(input);
  if (d === null) return "—";
  try {
    return d.toLocaleTimeString(uiLocale(), { hour12: false, ...opts });
  } catch {
    return d.toISOString().slice(11, 19);
  }
}

/** Compact `HH:MM:SS` in the viewer's LOCAL timezone (never a UTC slice). */
export function formatTimeShort(input: DateInput): string {
  const d = toDate(input);
  if (d === null) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Number with grouping in the UI language. */
export function formatNumber(
  n: number | null | undefined,
  opts?: Intl.NumberFormatOptions,
): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  try {
    return n.toLocaleString(uiLocale(), opts);
  } catch {
    return String(n);
  }
}
