/**
 * Scheduler admin API client — W6 of PLAN_PERSONA_STUDIO.md.
 *
 * The legacy types (`SchedulerJob` / `SchedulerHistory` /
 * `fetchSchedulerJobs` / `fetchSchedulerHistory` / `triggerSchedulerJob`)
 * still live in `lib/api.ts` for back-compat — every existing import
 * site there keeps working. This module wraps the W6 extensions:
 *
 *   POST   /admin/scheduler/jobs                          → 200 SchedulerJob
 *     body: NewSchedulerJob
 *   POST   /admin/scheduler/qzone/templates/{id}/enable   → 200 SchedulerJob
 *
 * The new `SchedulerJob` shape is a superset of the legacy one so the
 * existing list/trigger pages don't need to change — the extra W6
 * fields (`action_type`, `enabled`, `persona_id`, `prompt_template`,
 * `qq_account`, `last_*`, `source`) are optional and present only on
 * runtime-overlay rows.
 */

import { apiFetch } from "@/lib/api";
import { uiLocale } from "@/lib/format";

/* ------------------------------------------------------------------ */
/*                            Types                                   */
/* ------------------------------------------------------------------ */

/** Wire-stable name of the QZone daily-publish builtin (mirrors the
 * Python constant `QZONE_DAILY_BUILTIN_NAME`). */
export const QZONE_DAILY_ACTION_TYPE = "qzone.daily_publish" as const;

/** Job row shape returned by `GET /admin/scheduler/jobs`.
 *
 * Legacy fields stay non-optional so existing code keeps compiling;
 * W6 extras are optional + carried only on `source === "runtime"` rows.
 */
export interface SchedulerJobRow {
  name: string;
  cron: string;
  timezone: string | null;
  /** Legacy action-kind discriminant — `run_agent` / `run_tool` /
   * `subprocess` / `unknown`. Used by the existing scheduler page's
   * badge logic. W6 sets this to `"run_tool"` for runtime qzone jobs. */
  action_kind: string;
  next_fire_at: string | null;
  last_status: string | null;
  /** W6 — slug of the registered builtin to dispatch. */
  action_type?: string | null;
  enabled?: boolean;
  persona_id?: string | null;
  prompt_template?: string | null;
  qq_account?: string | null;
  last_run_at_ms?: number | null;
  last_run_ok?: boolean | null;
  last_qzone_url?: string | null;
  last_error?: string | null;
  /** `"config"` for `[[scheduler.jobs]]`-derived rows; `"runtime"` for
   * operator-created jobs sitting in the AdminState overlay. */
  source?: "config" | "runtime";
}

/** Body shape for `POST /admin/scheduler/jobs`. */
export interface NewSchedulerJob {
  name: string;
  cron: string;
  action_type: string;
  timezone?: string | null;
  enabled?: boolean;
  persona_id?: string | null;
  prompt_template?: string | null;
  qq_account?: string | null;
  metadata?: Record<string, unknown>;
}

/** Response from `POST /admin/scheduler/jobs/{name}/trigger` for
 * runtime QZone jobs — the route returns the captured audit dict
 * alongside the refreshed row. */
export interface SchedulerTriggerResult {
  ok: boolean;
  recorded?: {
    job: string;
    at: string;
    source: string;
    status: string;
    message: string;
  };
  result?: {
    ok: boolean;
    tid?: string | null;
    qzone_url?: string | null;
    error?: string | null;
    message?: string | null;
    tools_called?: string[];
    [key: string]: unknown;
  };
  job?: SchedulerJobRow;
}

/* ------------------------------------------------------------------ */
/*                            Calls                                   */
/* ------------------------------------------------------------------ */

/** Fetch every job (config jobs + runtime overlay) for the admin page. */
export function fetchSchedulerJobsTyped(): Promise<SchedulerJobRow[]> {
  return apiFetch<SchedulerJobRow[]>("/admin/scheduler/jobs");
}

/** Create or update a runtime scheduler job. Same `name` upserts in
 * place (the backend treats the route as idempotent for the
 * Grantley-template enable flow). Returns the updated row. */
export function createSchedulerJob(
  body: NewSchedulerJob,
): Promise<SchedulerJobRow> {
  return apiFetch<SchedulerJobRow>("/admin/scheduler/jobs", {
    method: "POST",
    body,
  });
}

/** Activate a bundled persona template (today: only `grantley`).
 * Reads `<DATA_DIR>/bundled_personas/{id}/daily_job.json` server-side
 * and upserts the corresponding runtime job. */
export function enableQzoneTemplate(
  templateId: string,
): Promise<SchedulerJobRow> {
  return apiFetch<SchedulerJobRow>(
    `/admin/scheduler/qzone/templates/${encodeURIComponent(templateId)}/enable`,
    { method: "POST" },
  );
}

/** Fire a job manually. The runtime fallback for `qzone.daily_publish`
 * jobs returns the captured audit envelope so the UI can show the
 * resulting `tid` / `qzone_url` immediately. */
export function triggerSchedulerJobTyped(
  name: string,
): Promise<SchedulerTriggerResult> {
  return apiFetch<SchedulerTriggerResult>(
    `/admin/scheduler/jobs/${encodeURIComponent(name)}/trigger`,
    { method: "POST" },
  );
}

/* ------------------------------------------------------------------ */
/*                       Pure helpers                                 */
/* ------------------------------------------------------------------ */

/** Convenience filter for "show me the QZone daily-publish jobs only"
 * — keeps the page logic out of the API module while letting the test
 * suite exercise the predicate in isolation. */
export function isQzoneDailyJob(j: SchedulerJobRow): boolean {
  return j.action_type === QZONE_DAILY_ACTION_TYPE;
}

/** Compute the next firing time from a 5-field cron expression.
 *
 * This is a deliberately small helper — it understands the subset the
 * gateway scheduler ships (standard 5-field crontabs: minute hour
 * day-of-month month day-of-week). The backend uses croniter so a
 * fully-validated firing time would require shipping the same library
 * to the browser; here we just project the next minute that satisfies
 * each field. Returns `null` for un-parseable expressions so the UI
 * can fall back to "—".
 */
export function nextFireTime(cron: string, from: Date = new Date()): Date | null {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minSpec, hourSpec, domSpec, monSpec, dowSpec] = parts;

  const matchesField = (
    spec: string,
    value: number,
    min: number,
    max: number,
  ): boolean => {
    // Iterate the comma-separated alternatives so `1,15` works.
    for (const alt of spec.split(",")) {
      if (alt === "*") return true;
      // Step form: `*/N` or `M-N/S`.
      const stepIdx = alt.indexOf("/");
      let stepBase = alt;
      let step = 1;
      if (stepIdx >= 0) {
        stepBase = alt.slice(0, stepIdx) || "*";
        const parsed = Number.parseInt(alt.slice(stepIdx + 1), 10);
        if (!Number.isFinite(parsed) || parsed <= 0) return false;
        step = parsed;
      }
      let lo: number;
      let hi: number;
      if (stepBase === "*") {
        lo = min;
        hi = max;
      } else if (stepBase.includes("-")) {
        const [loRaw, hiRaw] = stepBase.split("-");
        lo = Number.parseInt(loRaw, 10);
        hi = Number.parseInt(hiRaw, 10);
        if (!Number.isFinite(lo) || !Number.isFinite(hi)) return false;
      } else {
        lo = hi = Number.parseInt(stepBase, 10);
        if (!Number.isFinite(lo)) return false;
      }
      if (value < lo || value > hi) continue;
      if ((value - lo) % step === 0) return true;
    }
    return false;
  };

  // Brute-force minute-by-minute walk up to a year. Cheap enough for
  // an admin page projecting one row at a time, and entirely free of
  // edge cases (`5 0 * 8 *` etc. all fall out for free).
  const probe = new Date(from);
  probe.setSeconds(0, 0);
  probe.setMinutes(probe.getMinutes() + 1);
  for (let i = 0; i < 60 * 24 * 366; i += 1) {
    const minute = probe.getMinutes();
    const hour = probe.getHours();
    const dom = probe.getDate();
    const mon = probe.getMonth() + 1; // JS months are 0-indexed
    const dow = probe.getDay(); // 0=Sun .. 6=Sat
    if (
      matchesField(minSpec, minute, 0, 59) &&
      matchesField(hourSpec, hour, 0, 23) &&
      matchesField(domSpec, dom, 1, 31) &&
      matchesField(monSpec, mon, 1, 12) &&
      matchesField(dowSpec, dow, 0, 6)
    ) {
      return probe;
    }
    probe.setMinutes(probe.getMinutes() + 1);
  }
  return null;
}

/** Format a `Date` as a human-readable "next run in X" string.
 * Follows the active UI language unless an explicit locale is given. */
export function formatNextFire(date: Date | null, locale?: string): string {
  if (date === null) return "—";
  try {
    return date.toLocaleString(locale ?? uiLocale(), {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return date.toISOString();
  }
}
