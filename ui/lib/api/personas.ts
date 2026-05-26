/**
 * Personas admin API client.
 *
 * Mirrors the gateway routes (see backend agent's PR) at
 * `/admin/personas*` and the QQ-channel humanlike toggle at
 * `/admin/channels/qq/humanlike`:
 *
 *   GET    /admin/personas                  → 200 { personas: Persona[] }
 *   GET    /admin/personas/{id}             → 200 Persona | 404
 *   POST   /admin/personas                  → 201 Persona
 *     body: NewPersona
 *   PATCH  /admin/personas/{id}             → 200 Persona | 404
 *     body: PartialPersona  (omit field → unchanged)
 *   DELETE /admin/personas/{id}             → 204 | 404 (builtin-protected)
 *
 *   GET /admin/channels/qq/humanlike        → 200 { enabled, persona_id }
 *   PUT /admin/channels/qq/humanlike        → 200 { enabled, persona_id }
 *
 * Asset surface (W1 — already shipped on the gateway):
 *   GET    /admin/personas/{id}/assets[?kind=emoji|reference]
 *                                            → 200 { assets: AssetRecord[] }
 *   POST   /admin/personas/{id}/assets       → 201 AssetRecord
 *     multipart form: kind, label, file
 *   GET    /admin/personas/{id}/assets/{aid} → 200 image/* blob
 *   DELETE /admin/personas/{id}/assets/{aid} → 204
 *
 * `created_at_ms` / `updated_at_ms` are **unix milliseconds** (the SQLite
 * column is i64) — the UI formats them via `new Date(ms).toLocaleString()`.
 *
 * Mirrors the error-tagging discipline of `lib/api/sessions.ts`: methods
 * that can fail in a known UX-meaningful way return a tagged-union result
 * (or `null` on 404) instead of throwing for 404 / "builtin_protected".
 * Genuine network / 5xx failures still throw `CorlinmanApiError`.
 */

import { CorlinmanApiError, GATEWAY_BASE_URL, apiFetch } from "@/lib/api";

/* ------------------------------------------------------------------ */
/*                           Public types                             */
/* ------------------------------------------------------------------ */

/** One row in `GET /admin/personas`. */
export interface Persona {
  /** Slug, e.g. `"grantley"`. */
  id: string;
  /** Human-readable name, e.g. `"格兰特利·贝尔"`. */
  display_name: string;
  /** One- or two-line summary used in list rows. */
  short_summary: string;
  /** Markdown system prompt; may be long (5–10k chars). */
  system_prompt: string;
  /** Builtins can be **edited** but NOT deleted. */
  is_builtin: boolean;
  /** Unix milliseconds. */
  created_at_ms: number;
  /** Unix milliseconds. */
  updated_at_ms: number;
}

/** Body shape for `POST /admin/personas`. */
export interface NewPersona {
  id: string;
  display_name: string;
  short_summary: string;
  system_prompt: string;
}

/** Body shape for `PATCH /admin/personas/{id}`. All fields optional —
 * omitting a field leaves the persona's existing value untouched. */
export interface PartialPersona {
  display_name?: string;
  short_summary?: string;
  system_prompt?: string;
}

/** QQ humanlike toggle. `persona_id` is the slug of the active persona;
 * `null` means no persona is bound yet. */
export interface QqHumanlikeState {
  enabled: boolean;
  persona_id: string | null;
}

/** Tagged sentinel for `deletePersona` so the UI can branch on
 * "builtin tried to delete → 404" without inspecting exception messages. */
export type DeletePersonaResult = void | "builtin_protected";

/* ------------------------------------------------------------------ */
/*                          URL builders                              */
/* ------------------------------------------------------------------ */

/** GET / POST path for the personas collection. */
export const PERSONAS_LIST_PATH = "/admin/personas";

/** Per-id path (GET / PATCH / DELETE). Slugs round-trip through
 * `encodeURIComponent` so any non-`[a-z0-9-]` byte is escaped. */
export function personaPath(id: string): string {
  return `/admin/personas/${encodeURIComponent(id)}`;
}

/** GET / PUT path for the QQ humanlike state. */
export const QQ_HUMANLIKE_PATH = "/admin/channels/qq/humanlike";

/* ------------------------------------------------------------------ */
/*                          Error helpers                             */
/* ------------------------------------------------------------------ */

function is404(err: unknown): boolean {
  return err instanceof CorlinmanApiError && err.status === 404;
}

/* ------------------------------------------------------------------ */
/*                           Public fetches                           */
/* ------------------------------------------------------------------ */

/**
 * `GET /admin/personas` — returns the array directly, unwrapping the
 * `{ personas }` envelope. The wire envelope exists so future cursors
 * can be tacked on without breaking the response shape.
 */
export async function fetchPersonas(): Promise<Persona[]> {
  const res = await apiFetch<{ personas: Persona[] }>(PERSONAS_LIST_PATH);
  return res.personas ?? [];
}

/**
 * `GET /admin/personas/{id}` — returns `null` on 404 instead of throwing,
 * so callers can use a missing persona as a normal control-flow branch
 * (the editor modal does exactly this).
 */
export async function fetchPersona(id: string): Promise<Persona | null> {
  try {
    return await apiFetch<Persona>(personaPath(id));
  } catch (err) {
    if (is404(err)) return null;
    throw err;
  }
}

/** `POST /admin/personas` — creates a new persona, returns it. */
export function createPersona(body: NewPersona): Promise<Persona> {
  return apiFetch<Persona>(PERSONAS_LIST_PATH, {
    method: "POST",
    body,
  });
}

/**
 * `PATCH /admin/personas/{id}` — partial update. Pass only the fields you
 * want to change; omit a field to leave it unchanged. The wire contract
 * does NOT allow renaming `id` (the URL is the source of truth), so the
 * patch type intentionally excludes `id`.
 */
export function updatePersona(
  id: string,
  patch: PartialPersona,
): Promise<Persona> {
  return apiFetch<Persona>(personaPath(id), {
    method: "PATCH",
    body: patch,
  });
}

/**
 * `DELETE /admin/personas/{id}` — wipes a persona.
 *
 * Backend returns 204 for user-created personas and 404 for builtins
 * (which are non-deletable by design). We tag the 404 as
 * `"builtin_protected"` so the UI can paint an inline "cannot delete a
 * builtin" toast without parsing error bodies. Other 404s (unknown id)
 * are treated the same way — either way the row is already gone.
 */
export async function deletePersona(id: string): Promise<DeletePersonaResult> {
  try {
    await apiFetch<void>(personaPath(id), { method: "DELETE" });
    return undefined;
  } catch (err) {
    if (is404(err)) return "builtin_protected";
    throw err;
  }
}

/* ------------------------------------------------------------------ */
/*                  QQ channel humanlike toggle                       */
/* ------------------------------------------------------------------ */

/**
 * `GET /admin/channels/qq/humanlike` — current state of the toggle.
 * `persona_id` is the slug of the persona that's currently active when
 * `enabled === true`, or `null` when the toggle has never been set.
 */
export function fetchQqHumanlike(): Promise<QqHumanlikeState> {
  return apiFetch<QqHumanlikeState>(QQ_HUMANLIKE_PATH);
}

/**
 * `PUT /admin/channels/qq/humanlike` — writes the toggle.
 *
 * Backend additionally restarts the QQ channel runtime so the next
 * inbound message picks up the new persona (or drops it on disable).
 * Wire shape echoes the request body back as confirmation.
 */
export function setQqHumanlike(
  payload: QqHumanlikeState,
): Promise<QqHumanlikeState> {
  return apiFetch<QqHumanlikeState>(QQ_HUMANLIKE_PATH, {
    method: "PUT",
    body: payload,
  });
}

/* ------------------------------------------------------------------ */
/*                       Persona asset surface                        */
/* ------------------------------------------------------------------ */

/** Asset kinds enforced by the backend. `emoji` rows are surfaced to
 * the agent via `send_attachment` / `send_emoji`; `reference` rows are
 * fed to `image_with_refs` as character立绘. */
export type AssetKind = "emoji" | "reference";

/** Wire shape of one row in `GET /admin/personas/{id}/assets`. Mirrors
 * the gateway's `AssetOut` model. `url` is already an absolute admin
 * path (`/admin/personas/{persona_id}/assets/{id}`), safe to drop into
 * an `<img src>` as-is since the admin UI is same-origin with the
 * gateway under nginx (or proxied via `NEXT_PUBLIC_GATEWAY_URL`). */
export interface AssetRecord {
  id: string;
  persona_id: string;
  kind: AssetKind;
  label: string;
  file_name: string;
  mime: string;
  size_bytes: number;
  sha256: string;
  /** Unix milliseconds. */
  created_at_ms: number;
  /** Absolute admin path (`/admin/personas/{persona_id}/assets/{id}`). */
  url: string;
}

/** MIME types the backend's `PersonaAssetStore.put` accepts. Kept in
 * sync so the client-side gate rejects bad files before the upload
 * even leaves the browser. */
export const ASSET_ALLOWED_MIMES: readonly string[] = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
];

/** Per-asset cap enforced by the backend (8 MiB). */
export const ASSET_MAX_BYTES = 8 * 1024 * 1024;

/** Per-persona total cap enforced by the backend (200 MiB). */
export const PERSONA_TOTAL_BYTES_CAP = 200 * 1024 * 1024;

/** Backend slug rule for labels: lowercase / digits / hyphen / underscore,
 * 1–64 chars. Matched character-for-character against the FastAPI
 * route's regex so the client gate is identical to the server gate. */
export const ASSET_LABEL_RE = /^[a-z0-9_-]{1,64}$/;

/** Practical limit for `image_with_refs` reference packs. Reference uploads
 * past this point keep persisting (so users can swap which 8 are active),
 * but the model only sees the first N. Surfaced as a hint in the editor. */
export const REFERENCE_VISIBLE_CAP = 8;

/** Tagged error codes the asset upload route can return in
 * `detail.error`. Maps the FastAPI HTTPException bodies the gateway
 * raises (`payload_too_large`, `unsupported_media_type`, …) into
 * something the toast layer can switch on. */
export type AssetUploadErrorCode =
  | "payload_too_large"
  | "persona_quota_exceeded"
  | "unsupported_media_type"
  | "invalid_label"
  | "duplicate_label"
  | "invalid_kind"
  | "persona_not_found"
  | "upload_failed";

/** Error thrown by `uploadAsset` when the backend rejects the upload
 * with a known 4xx (413 / 415 / 400 / 409). Carries the parsed
 * `detail.error` code so the UI can pick a friendly toast without
 * regexing the raw error body. */
export class AssetUploadError extends Error {
  readonly code: AssetUploadErrorCode;
  readonly status: number;
  constructor(code: AssetUploadErrorCode, status: number, message?: string) {
    super(message ?? code);
    this.name = "AssetUploadError";
    this.code = code;
    this.status = status;
  }
}

/** Collection path. Slug round-trips through `encodeURIComponent`. */
export function personaAssetsPath(personaId: string, kind?: AssetKind): string {
  const base = `/admin/personas/${encodeURIComponent(personaId)}/assets`;
  return kind ? `${base}?kind=${kind}` : base;
}

/** Per-asset path (GET serves the blob, DELETE removes it). */
export function personaAssetItemPath(personaId: string, assetId: string): string {
  return `/admin/personas/${encodeURIComponent(personaId)}/assets/${encodeURIComponent(assetId)}`;
}

/**
 * Parse an unknown error body coming back from the assets upload
 * route. FastAPI wraps `HTTPException(detail=...)` either as
 *   - `{ "detail": "<string>" }` when the route raised a bare string
 *     detail, or
 *   - `{ "detail": { "error": "<code>", ... } }` for structured details.
 * We normalise both into a `(code, message)` pair plus a sensible
 * default keyed off the HTTP status so the UI always has a code to
 * branch on.
 */
function parseAssetUploadError(
  status: number,
  bodyText: string,
): { code: AssetUploadErrorCode; message: string } {
  let code: AssetUploadErrorCode;
  let message = bodyText;
  let detail: unknown = null;
  try {
    const parsed = JSON.parse(bodyText);
    detail = (parsed as { detail?: unknown })?.detail ?? parsed;
  } catch {
    detail = bodyText;
  }
  if (detail && typeof detail === "object") {
    const obj = detail as Record<string, unknown>;
    const errField = typeof obj.error === "string" ? obj.error : null;
    if (errField) {
      code = errField as AssetUploadErrorCode;
      message = errField;
      return { code, message };
    }
  }
  // Status-keyed fallback so the toast still says something useful when
  // the backend returns a bare string detail.
  if (status === 413) code = "payload_too_large";
  else if (status === 415) code = "unsupported_media_type";
  else if (status === 404) code = "persona_not_found";
  else if (status === 409) code = "duplicate_label";
  else if (status === 400) code = "invalid_label";
  else code = "upload_failed";
  return { code, message };
}

/**
 * `GET /admin/personas/{id}/assets[?kind=…]` — list every asset belonging
 * to one persona. Pass `kind` to filter to a single section (emoji vs
 * reference); omit it to get everything in one shot for the totals
 * banner. Returns the bare array, dropping the `{assets}` envelope.
 */
export async function listAssets(
  personaId: string,
  kind?: AssetKind,
): Promise<AssetRecord[]> {
  const res = await apiFetch<{ assets: AssetRecord[] }>(
    personaAssetsPath(personaId, kind),
  );
  return res.assets ?? [];
}

/**
 * `POST /admin/personas/{id}/assets` — multipart upload.
 *
 * FormData fields:
 *   - `kind`  — "emoji" | "reference"
 *   - `label` — `[a-z0-9_-]{1,64}`
 *   - `file`  — the actual blob (`image/png|jpeg|webp|gif`)
 *
 * On non-2xx, throws `AssetUploadError(code, status)` so the editor
 * modal can show a per-code toast (e.g. "label collides", "file too
 * large", "out of quota"). 5xx and network failures fall through as
 * the generic `CorlinmanApiError` because they're not actionable in
 * the UI beyond "try again".
 *
 * We bypass `apiFetch` here because that wrapper hard-codes
 * `content-type: application/json` and stringifies the body — neither
 * is correct for multipart. We still preserve `credentials: "include"`
 * so the gateway session cookie rides along, and we forward the
 * `x-request-id` for log correlation.
 */
export async function uploadAsset(
  personaId: string,
  kind: AssetKind,
  label: string,
  file: File,
): Promise<AssetRecord> {
  const form = new FormData();
  form.append("kind", kind);
  form.append("label", label);
  form.append("file", file, file.name);

  const res = await fetch(
    `${GATEWAY_BASE_URL}${personaAssetsPath(personaId)}`,
    {
      method: "POST",
      credentials: "include",
      body: form,
      // No `content-type` header — the browser will inject the right
      // `multipart/form-data; boundary=…` for us. Setting it manually
      // breaks the boundary token.
    },
  );

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    // 5xx / network style — surface the regular error so callers don't
    // accidentally swallow infra failures as upload errors.
    if (res.status >= 500) {
      throw new CorlinmanApiError(
        text || `Upload failed: ${res.status}`,
        res.status,
        res.headers.get("x-request-id") ?? undefined,
      );
    }
    const { code, message } = parseAssetUploadError(res.status, text);
    throw new AssetUploadError(code, res.status, message);
  }
  return (await res.json()) as AssetRecord;
}

/**
 * `DELETE /admin/personas/{id}/assets/{asset_id}` — 204 on success.
 *
 * 404 on a missing asset is collapsed into a successful no-op so the
 * UI can stay optimistic (the row is already gone — refetching will
 * confirm). Other errors propagate so the caller can rollback.
 */
export async function deleteAsset(
  personaId: string,
  assetId: string,
): Promise<void> {
  try {
    await apiFetch<void>(personaAssetItemPath(personaId, assetId), {
      method: "DELETE",
    });
  } catch (err) {
    if (is404(err)) return;
    throw err;
  }
}
