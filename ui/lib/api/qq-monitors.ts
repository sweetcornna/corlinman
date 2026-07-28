/**
 * QQ group-message monitor admin API client.
 *
 * Wraps the per-instance monitor CRUD surface:
 *
 *   GET  /admin/channels/qq/instances                          → instance list
 *   GET  /admin/channels/qq/instances/{id}/monitors            → specs + revision
 *   PUT  /admin/channels/qq/instances/{id}/monitors            → whole-list replace
 *        (optimistic concurrency via `If-Match: <revision>`; 409 on conflict)
 *   POST /admin/channels/qq/instances/{id}/monitors/{mid}/trigger → fire once
 *   GET  /admin/channels/qq/instances/{id}/monitors/status     → run stats
 *
 * The monitor list is replaced wholesale on save — there is no per-row
 * PATCH — so the UI keeps a local draft and PUTs the full array with the
 * revision it last saw. A 409 means someone else saved in between; the
 * caller should refetch and rebuild its draft.
 */

import { apiFetch } from "@/lib/api";

/* ------------------------------------------------------------------ */
/*                            Types                                   */
/* ------------------------------------------------------------------ */

export type QqMonitorScheduleType = "daily" | "interval";
export type QqMonitorTargetType = "group" | "user";

/** One watched group inside a monitor task. */
export interface QqMonitorSource {
  /** Group number being watched (digits only). */
  group: string;
  /** Capture scope: empty array = every member; otherwise only these
   * QQ ids (focused members are ALWAYS captured regardless). */
  watch_user_ids: string[];
  /** Focused members — independent of the capture scope (may coexist
   * with "all members"). Each gets a dedicated recap at the end of the
   * digest, and is always captured even when `watch_user_ids` narrows
   * the scope. */
  focus_user_ids: string[];
}

/** One monitor task. Mirrors the backend `QqMonitorSpec` model.
 * Legacy flat rows (`source_group` + top-level `watch_user_ids`) are
 * lifted into `sources` by the backend on GET — the client only ever
 * reads/writes the new shape. */
export interface QqMonitorSpec {
  /** `^[a-z0-9][a-z0-9_-]{0,63}$` — immutable after creation (it keys
   * the scheduler's per-monitor run state). */
  id: string;
  enabled: boolean;
  /** 1..20 sources; group numbers must be unique within a task
   * (the gateway 422s otherwise, and on an empty list). */
  sources: QqMonitorSource[];
  schedule_type: QqMonitorScheduleType;
  /** "HH:MM" — required when `schedule_type === "daily"`. */
  daily_time: string | null;
  /** >= 5 — required when `schedule_type === "interval"`. */
  interval_minutes: number | null;
  /** IANA zone; empty string lets the backend pick its default. */
  timezone: string;
  /** 0 = auto (daily → 1440, interval → interval_minutes). */
  window_minutes: number;
  /** Deliver the digest to a group or a private chat. */
  target_type: QqMonitorTargetType;
  /** Digits only. */
  target_id: string;
  /** Extra style directives appended to the built-in digest style. */
  style_extra: string;
  /** Whether to still deliver when the window captured nothing. */
  send_when_empty: boolean;
}

/** Envelope shared by `GET` and `PUT …/monitors`. */
export interface QqMonitorsResponse {
  monitors: QqMonitorSpec[];
  warnings: string[];
  revision: string;
}

export interface QqMonitorStatusEntry {
  last_run_ms?: number | null;
  last_ok?: boolean | null;
  last_error?: string | null;
  last_count?: number | null;
  last_reason?: string | null;
  last_delivered?: boolean | null;
}

/** `counts` = messages captured in the current (in-progress) window,
 * summed across ALL of the task's sources. */
export interface QqMonitorsStatusResponse {
  statuses: Record<string, QqMonitorStatusEntry>;
  counts: Record<string, number>;
}

interface QqInstanceRow {
  instance_id: string;
  display_name?: string;
  enabled?: boolean;
  is_default?: boolean;
}

/** Client-side mirror of the backend id rule. */
export const QQ_MONITOR_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/* ------------------------------------------------------------------ */
/*                            Calls                                   */
/* ------------------------------------------------------------------ */

/** Resolve the instance the monitor panel manages: the default QQ
 * instance, falling back to the conventional `"default"` id. */
export async function getQqDefaultInstanceId(): Promise<string> {
  const res = await apiFetch<{ instances: QqInstanceRow[] }>(
    "/admin/channels/qq/instances",
  );
  return res.instances.find((i) => i.is_default)?.instance_id ?? "default";
}

export function getQqMonitors(
  instanceId: string,
): Promise<QqMonitorsResponse> {
  return apiFetch<QqMonitorsResponse>(
    `/admin/channels/qq/instances/${encodeURIComponent(instanceId)}/monitors`,
  );
}

/** Whole-list replace. `revision` must be the one from the last GET/PUT
 * echo; the gateway answers 409 (`revision_conflict`) when it moved. */
export function putQqMonitors(
  instanceId: string,
  monitors: QqMonitorSpec[],
  revision: string,
): Promise<QqMonitorsResponse> {
  return apiFetch<QqMonitorsResponse>(
    `/admin/channels/qq/instances/${encodeURIComponent(instanceId)}/monitors`,
    {
      method: "PUT",
      body: { monitors },
      headers: { "If-Match": revision },
    },
  );
}

/** Fire one monitor now. 202 on accept; 404 unknown id; 409 when the
 * monitor loop isn't running (channel offline / feature inactive). */
export function triggerQqMonitor(
  instanceId: string,
  monitorId: string,
): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(
    `/admin/channels/qq/instances/${encodeURIComponent(instanceId)}/monitors/${encodeURIComponent(monitorId)}/trigger`,
    { method: "POST" },
  );
}

export function getQqMonitorsStatus(
  instanceId: string,
): Promise<QqMonitorsStatusResponse> {
  return apiFetch<QqMonitorsStatusResponse>(
    `/admin/channels/qq/instances/${encodeURIComponent(instanceId)}/monitors/status`,
  );
}
