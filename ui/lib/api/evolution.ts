/**
 * Evolution settings client. Wraps the `/admin/evolution/settings` GET/PUT
 * pair on the gateway (`routes_admin_b/evolution.py`).
 *
 * The page-level proposal-queue / curator clients live in the big
 * `@/lib/api` barrel; this focused module owns only the three operator
 * tunables that previously had no UI:
 *
 *   - `meta_approver_users` — the `[admin].meta_approver_users` allow-list.
 *     Empty by default, which 403s EVERY meta-kind approval
 *     (`engine_config` / `engine_prompt` / `observer_filter` /
 *     `cluster_threshold`) until at least one operator id is listed.
 *   - `budget` — the `[evolution.budget]` weekly quota (enabled flag +
 *     weekly total + per-kind caps).
 *   - `auto_rollback` — the `[evolution.auto_rollback]` grace window +
 *     metrics-breach thresholds.
 *
 *   GET /admin/evolution/settings  → 200 EvolutionSettings
 *   PUT /admin/evolution/settings  → 200 { status: "ok", settings }
 *                                     503 `config_path_unset` when the
 *                                     gateway booted without a config file.
 *
 * Both routes ride the admin session cookie (`apiFetch` forwards it via
 * `credentials: "include"`). The write persists through the same atomic
 * config-write path `/admin/config` uses.
 */

import { CorlinmanApiError, apiFetch } from "@/lib/api";

export interface AutoRollbackThresholds {
  default_err_rate_delta_pct: number;
  default_p95_latency_delta_pct: number;
  signal_window_secs: number;
  min_baseline_signals: number;
}

export interface AutoRollbackSettings {
  enabled: boolean;
  grace_window_hours: number;
  thresholds: AutoRollbackThresholds;
}

export interface BudgetSettings {
  enabled: boolean;
  weekly_total: number;
  /** kind → weekly cap (0 means "block this kind entirely"). */
  per_kind: Record<string, number>;
}

export interface EvolutionSettings {
  /** Operator ids allowed to approve meta-kind proposals. */
  meta_approver_users: string[];
  budget: BudgetSettings;
  auto_rollback: AutoRollbackSettings;
}

/** 200 body of `PUT /admin/evolution/settings`. Echoes the persisted shape. */
export interface PutEvolutionSettingsResponse {
  status: string;
  settings: EvolutionSettings;
}

/**
 * Tagged result for the read call. `disabled` is the non-fatal
 * 503 `config_path_unset` path (gateway booted without a config file) — the
 * settings are still readable from the live snapshot but cannot be written,
 * so the editor renders in a read-only / unavailable state instead of
 * throwing. Everything else throws `CorlinmanApiError`.
 */
export type EvolutionSettingsState =
  | { kind: "ok"; settings: EvolutionSettings }
  | { kind: "disabled" }
  | { kind: "error"; message: string };

/** Fill any missing nested section with a safe default so the editor never
 *  dereferences `undefined` on a partial / legacy snapshot. */
export function normalizeEvolutionSettings(
  raw: Partial<EvolutionSettings> | null | undefined,
): EvolutionSettings {
  return {
    meta_approver_users: Array.isArray(raw?.meta_approver_users)
      ? raw!.meta_approver_users.map((u) => String(u))
      : [],
    budget: {
      enabled: Boolean(raw?.budget?.enabled),
      weekly_total: Number(raw?.budget?.weekly_total ?? 0),
      per_kind: { ...(raw?.budget?.per_kind ?? {}) },
    },
    auto_rollback: {
      enabled: Boolean(raw?.auto_rollback?.enabled),
      grace_window_hours: Number(raw?.auto_rollback?.grace_window_hours ?? 72),
      thresholds: {
        default_err_rate_delta_pct: Number(
          raw?.auto_rollback?.thresholds?.default_err_rate_delta_pct ?? 0,
        ),
        default_p95_latency_delta_pct: Number(
          raw?.auto_rollback?.thresholds?.default_p95_latency_delta_pct ?? 0,
        ),
        signal_window_secs: Number(
          raw?.auto_rollback?.thresholds?.signal_window_secs ?? 0,
        ),
        min_baseline_signals: Number(
          raw?.auto_rollback?.thresholds?.min_baseline_signals ?? 0,
        ),
      },
    },
  };
}

/** GET /admin/evolution/settings → tagged state. */
export async function fetchEvolutionSettings(): Promise<EvolutionSettingsState> {
  try {
    const res = await apiFetch<Partial<EvolutionSettings>>(
      "/admin/evolution/settings",
    );
    return { kind: "ok", settings: normalizeEvolutionSettings(res) };
  } catch (err) {
    if (err instanceof CorlinmanApiError) {
      if (err.status === 503 && /config_path_unset/.test(err.message)) {
        return { kind: "disabled" };
      }
      return { kind: "error", message: err.message };
    }
    return {
      kind: "error",
      message: err instanceof Error ? err.message : String(err),
    };
  }
}

/** PUT /admin/evolution/settings. Throws `CorlinmanApiError` on 503
 *  (`config_path_unset`) / 500 (`write_failed`) so the form can surface it. */
export function saveEvolutionSettings(
  settings: EvolutionSettings,
): Promise<PutEvolutionSettingsResponse> {
  return apiFetch<PutEvolutionSettingsResponse>("/admin/evolution/settings", {
    method: "PUT",
    body: settings,
  });
}
