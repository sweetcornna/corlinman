/**
 * Operator API-keys client. Wraps the `/admin/api_keys*` mint surface on
 * the gateway (`routes_admin_a/api_keys.py`).
 *
 * Three endpoints, all behind the admin session cookie (forwarded
 * automatically by `apiFetch` via `credentials: "include"`):
 *
 *   POST   /admin/api_keys           → 201 MintApiKeyResponse (cleartext
 *                                       `token` returned EXACTLY ONCE).
 *   GET    /admin/api_keys           → 200 { keys: ApiKeyRow[] }
 *   DELETE /admin/api_keys/{key_id}  → 200 { revoked, key_id }; 404 when
 *                                       the id is unknown / already revoked.
 *
 * When the gateway boots without a tenant admin DB every route answers
 * 503 `tenants_disabled`; `listApiKeys` maps that onto a tagged
 * `{ kind: "disabled" }` so the panel can render a "feature unavailable"
 * note instead of an error toast. Mint / revoke surface the raw
 * `CorlinmanApiError` for inline form handling.
 *
 * The server resolves the tenant from the session / middleware, so we do
 * NOT send a `?tenant=` query — the brief's note that a client-supplied
 * tenant can no longer override the resolved scope means it would be
 * ignored anyway.
 */

import { CorlinmanApiError, apiFetch } from "@/lib/api";

/** One active key as returned by `GET /admin/api_keys`. Never carries the
 *  cleartext token or its hash. Timestamps are epoch-ms ints. */
export interface ApiKeyRow {
  key_id: string;
  tenant_id: string;
  username: string;
  scope: string;
  label: string | null;
  /** epoch-ms */
  created_at_ms: number;
  /** epoch-ms; null until the key has authenticated at least once. */
  last_used_at_ms: number | null;
}

/** 201 body of `POST /admin/api_keys`. `token` is the cleartext bearer and
 *  is returned only here — it can never be read back afterwards. */
export interface MintApiKeyResponse extends ApiKeyRow {
  token: string;
}

export interface MintApiKeyBody {
  scope: string;
  username?: string;
  label?: string;
}

/** 200 body of `DELETE /admin/api_keys/{key_id}`. */
export interface RevokeApiKeyResponse {
  revoked: boolean;
  key_id: string;
}

/**
 * Tagged result for the list call. `disabled` is the non-fatal
 * 503 `tenants_disabled` path (gateway booted without an admin DB);
 * everything else throws so the panel can branch without try/catch noise.
 */
export type ApiKeysListState =
  | { kind: "ok"; keys: ApiKeyRow[] }
  | { kind: "disabled" }
  | { kind: "error"; message: string };

/** Build the revoke path with the key id percent-encoded. Exported for
 *  unit testing so the colon-/slash-safe encoding is pinned. */
export function apiKeyRevokePath(keyId: string): string {
  return `/admin/api_keys/${encodeURIComponent(keyId)}`;
}

/** GET /admin/api_keys → tagged list. Tolerates a missing `keys` field. */
export async function listApiKeys(): Promise<ApiKeysListState> {
  try {
    const res = await apiFetch<{ keys?: ApiKeyRow[] }>("/admin/api_keys");
    return { kind: "ok", keys: res.keys ?? [] };
  } catch (err) {
    if (err instanceof CorlinmanApiError) {
      if (err.status === 503 && /tenants_disabled/.test(err.message)) {
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

/** POST /admin/api_keys → 201 + cleartext token. Throws `CorlinmanApiError`
 *  on 400 (empty scope) / 503 (tenants disabled). */
export function mintApiKey(
  body: MintApiKeyBody,
): Promise<MintApiKeyResponse> {
  return apiFetch<MintApiKeyResponse>("/admin/api_keys", {
    method: "POST",
    body,
  });
}

/** DELETE /admin/api_keys/{key_id}. Throws `CorlinmanApiError` on 404
 *  (unknown / already revoked) so the caller can re-list to reconcile. */
export function revokeApiKey(keyId: string): Promise<RevokeApiKeyResponse> {
  return apiFetch<RevokeApiKeyResponse>(apiKeyRevokePath(keyId), {
    method: "DELETE",
  });
}
