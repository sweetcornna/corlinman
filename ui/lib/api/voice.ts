/**
 * Voice (TTS) admin API client.
 *
 * Mirrors the gateway routes in
 * `corlinman_server/gateway/routes_admin_b/voice.py`:
 *
 *   GET  /admin/voice/backends  → 200 VoiceBackendsResponse
 *   GET  /admin/voice/settings  → 200 VoiceSettings   (secrets redacted)
 *   PUT  /admin/voice/settings  → 200 { status: "ok" } | 400 | 503
 *   POST /admin/voice/preview   → 200 VoicePreview    | 404 | 502
 *
 * Two conventions worth knowing before touching this file:
 *
 * **Secrets never round-trip in plaintext.** `GET` returns
 * {@link REDACTED_SECRET} for any stored `api_key`. Submitting that
 * sentinel — or an empty string — means "keep what you have", so the
 * settings form can be saved without ever holding the real credential.
 *
 * **Backends are data, not an enum.** The catalog includes any
 * `[voice.backends.*]` an operator defined in config, so the picker must
 * render whatever it is handed rather than switch on known ids. A backend
 * with `free_form_voices` (voice clones — Fish, ElevenLabs, MiniMax) has
 * an empty `voices` list and needs a free-text field instead of a select.
 */

import { apiFetch } from "@/lib/api";

/** Sentinel the server sends in place of a stored credential. */
export const REDACTED_SECRET = "***REDACTED***";

/** One selectable voice inside a backend's catalog. */
export interface VoiceDef {
  id: string;
  label: string;
  description: string;
  /** Short timbre tag, e.g. `"温暖"`. */
  tone: string;
  /** Vendor-flagged current-generation pick. */
  recommended: boolean;
}

/** One TTS provider as described by the server-side registry. */
export interface VoiceBackend {
  id: string;
  label: string;
  /** `"http"` = one request/response; `"webrtc_live"` = GPT-Live session. */
  kind: "http" | "webrtc_live";
  description: string;
  models: string[];
  default_model: string;
  voices: VoiceDef[];
  formats: string[];
  default_voice: string;
  /** Voice ids are user-created handles → free-text, no validation. */
  free_form_voices: boolean;
  supports_instructions: boolean;
  supports_speed: boolean;
  /** Defined or extended by the operator via `[voice.backends.*]`. */
  custom: boolean;
  /** A credential is reachable (config pin, env var, or chat provider). */
  credential_set: boolean;
  api_key_env: string;
  base_url: string;
}

export interface VoiceBackendsResponse {
  backends: VoiceBackend[];
  /** format id → mime, e.g. `{ mp3: "audio/mpeg" }`. */
  formats: Record<string, string>;
  default_backend: string;
}

/** Per-backend overrides stored under `[voice.backends.<id>]`. */
export interface VoiceBackendOverride {
  api_key?: string;
  base_url?: string;
  voice?: string;
  reference_id?: string;
  model?: string;
  format?: string;
  speed?: number;
  [key: string]: unknown;
}

export interface VoiceSettings {
  enabled: boolean;
  backend: string;
  voice: string;
  model: string;
  format: string;
  instructions: string;
  speed: number | null;
  backends: Record<string, VoiceBackendOverride>;
}

/** Body for `PUT /admin/voice/settings`; omit a field to leave it alone. */
export type VoiceSettingsPatch = Partial<
  Omit<VoiceSettings, "backends">
> & {
  backends?: Record<string, VoiceBackendOverride>;
};

export interface VoicePreviewRequest {
  text?: string;
  backend?: string;
  voice?: string;
  model?: string;
  format?: string;
  instructions?: string;
  speed?: number;
}

export interface VoicePreview {
  ok: true;
  /** `/v1/files/{id}` — playable directly in an `<audio>` element. */
  url: string;
  mime: string;
  backend: string;
  voice: string;
  model: string;
  format: string;
  size_bytes: number;
}

/** A preview that failed upstream — carries the provider's own code. */
export interface VoicePreviewError {
  ok: false;
  /** e.g. `tts_unavailable`, `live_attestation_unavailable`. */
  error: string;
  message: string;
  backend: string;
  /** Upstream HTTP status when the failure came from a provider call. */
  upstream_status: number | null;
}

export type VoicePreviewResult = VoicePreview | VoicePreviewError;

export function listVoiceBackends(): Promise<VoiceBackendsResponse> {
  return apiFetch<VoiceBackendsResponse>("/admin/voice/backends");
}

export function getVoiceSettings(): Promise<VoiceSettings> {
  return apiFetch<VoiceSettings>("/admin/voice/settings");
}

export function putVoiceSettings(
  body: VoiceSettingsPatch,
): Promise<{ status: string }> {
  return apiFetch<{ status: string }>("/admin/voice/settings", {
    method: "PUT",
    body,
  });
}

/**
 * Synthesise a sample clip.
 *
 * A provider-side failure is a *normal outcome* here, not an exception:
 * an operator auditioning an unconfigured backend should see the reason
 * inline. So a 404/502 is decoded into a {@link VoicePreviewError} rather
 * than thrown; only genuine network faults propagate.
 */
export async function previewVoice(
  body: VoicePreviewRequest,
): Promise<VoicePreviewResult> {
  try {
    const res = await apiFetch<VoicePreview>("/admin/voice/preview", {
      method: "POST",
      body,
    });
    return { ...res, ok: true };
  } catch (err) {
    const status = (err as { status?: number }).status;
    if (status === 404 || status === 502 || status === 400 || status === 500) {
      const parsed = parsePreviewError(err, body.backend ?? "");
      if (parsed) return parsed;
    }
    throw err;
  }
}

/**
 * Recover the structured error body from a thrown `CorlinmanApiError`.
 *
 * `apiFetch` folds the response body into the message string, so the
 * JSON payload has to be dug back out. Returns `null` when the message
 * is not JSON, letting the caller rethrow.
 */
function parsePreviewError(
  err: unknown,
  backend: string,
): VoicePreviewError | null {
  const message = err instanceof Error ? err.message : String(err);
  const start = message.indexOf("{");
  if (start < 0) return null;
  try {
    const body = JSON.parse(message.slice(start)) as Record<string, unknown>;
    if (typeof body.error !== "string") return null;
    return {
      ok: false,
      error: body.error,
      message: typeof body.message === "string" ? body.message : message,
      backend: typeof body.backend === "string" ? body.backend : backend,
      upstream_status:
        typeof body.upstream_status === "number" ? body.upstream_status : null,
    };
  } catch {
    return null;
  }
}
