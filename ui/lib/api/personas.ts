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
 * `created_at_ms` / `updated_at_ms` are **unix milliseconds** (the SQLite
 * column is i64) — the UI formats them via `new Date(ms).toLocaleString()`.
 *
 * Mirrors the error-tagging discipline of `lib/api/sessions.ts`: methods
 * that can fail in a known UX-meaningful way return a tagged-union result
 * (or `null` on 404) instead of throwing for 404 / "builtin_protected".
 * Genuine network / 5xx failures still throw `CorlinmanApiError`.
 */

import { CorlinmanApiError, apiFetch } from "@/lib/api";

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
