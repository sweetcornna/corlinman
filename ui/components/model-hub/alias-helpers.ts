/**
 * Pure helpers behind the provider editor's model-add flow.
 *
 * Extracted verbatim from `app/(admin)/providers/page.tsx` (PR4 model-hub
 * consolidation). These back the flow's three safety rails: dirty-draft
 * re-persistence, alias-conflict skipping, and the enabled gate — plus the
 * draft <-> wire-shape converters shared by the editor dialog.
 *
 * Unit tests live in `./__tests__/alias-helpers.test.tsx`.
 */

import type {
  ProviderKind,
  ProviderModelProbeRequest,
  ProviderUpsert,
  ProviderView,
} from "@/lib/api";

export const KINDS: ProviderKind[] = [
  "anthropic",
  "openai",
  "google",
  "deepseek",
  "qwen",
  "glm",
  "openai_compatible",
  // Market kinds added with the free-form-providers refactor. The backend
  // accepts them via /admin/providers and the dropdown now offers them as
  // first-class choices instead of forcing operators to reach for
  // openai_compatible + a hand-rolled base_url.
  "mistral",
  "cohere",
  "together",
  "groq",
  "replicate",
  "bedrock",
  "azure",
];

export type KeySource = "env" | "value" | "unset";

export type DraftProvider = {
  name: string;
  kind: ProviderKind;
  enabled: boolean;
  base_url: string;
  api_key_source: KeySource;
  api_key_env_name: string;
  api_key_value: string;
  params: Record<string, unknown>;
};

export const BLANK_DRAFT: DraftProvider = {
  name: "",
  kind: "openai_compatible",
  enabled: true,
  base_url: "",
  api_key_source: "env",
  api_key_env_name: "",
  api_key_value: "",
  params: {},
};

export function toDraft(p: ProviderView): DraftProvider {
  return {
    name: p.name,
    kind: p.kind,
    enabled: p.enabled,
    base_url: p.base_url ?? "",
    api_key_source: p.api_key_source,
    api_key_env_name: p.api_key_env_name ?? "",
    api_key_value: "",
    params: p.params ?? {},
  };
}

export function toUpsert(d: DraftProvider): ProviderUpsert {
  let api_key: ProviderUpsert["api_key"] = null;
  if (d.api_key_source === "env" && d.api_key_env_name.trim()) {
    api_key = { env: d.api_key_env_name.trim() };
  } else if (d.api_key_source === "value" && d.api_key_value.trim()) {
    api_key = { value: d.api_key_value.trim() };
  }
  return {
    name: d.name.trim(),
    kind: d.kind,
    enabled: d.enabled,
    base_url: d.base_url.trim() || undefined,
    api_key,
    params: d.params,
  };
}

export function canReuseSavedLiteralKey(
  editing: ProviderView | null,
  draft: DraftProvider,
): editing is ProviderView {
  return (
    !!editing &&
    draft.name.trim() === editing.name &&
    editing.api_key_source === "value" &&
    draft.api_key_source === "value" &&
    !draft.api_key_value.trim()
  );
}

export function toModelProbe(
  d: DraftProvider,
  editing: ProviderView | null,
): ProviderModelProbeRequest {
  const body: ProviderModelProbeRequest = {
    kind: d.kind,
    params: d.params,
  };
  const baseUrl = d.base_url.trim();
  if (baseUrl) {
    body.base_url = baseUrl;
  }
  if (d.api_key_source === "env" && d.api_key_env_name.trim()) {
    body.api_key = { env: d.api_key_env_name.trim() };
  } else if (d.api_key_source === "value" && d.api_key_value.trim()) {
    body.api_key = { value: d.api_key_value.trim() };
  } else if (canReuseSavedLiteralKey(editing, d)) {
    body.existing_name = editing.name;
  }
  return body;
}

function sameDiscoveryParams(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
) {
  return JSON.stringify(left ?? {}) === JSON.stringify(right ?? {});
}

export function shouldUseSavedModelDiscovery(
  editing: ProviderView | null,
  draft: DraftProvider,
) {
  return (
    canReuseSavedLiteralKey(editing, draft) &&
    draft.kind === editing.kind &&
    draft.base_url.trim() === (editing.base_url ?? "").trim() &&
    sameDiscoveryParams(draft.params, editing.params ?? {})
  );
}

export type ModelDiscoveryRequest = {
  generation: number;
  draft: DraftProvider;
  editing: ProviderView | null;
};

/** Every `DraftProvider` field is part of the persisted
 * ``[providers.<name>]`` block, so an edit to ANY of them invalidates a
 * previously persisted provider: the next model-add must re-upsert the
 * provider first or its aliases would bind against the stale stored config
 * (the backend keeps a stored api_key when the upsert carries none, so a
 * re-upsert never clobbers a literal key the operator didn't re-enter). */
const PROVIDER_CONFIG_KEYS = [
  "name",
  "kind",
  "enabled",
  "base_url",
  "api_key_source",
  "api_key_env_name",
  "api_key_value",
  "params",
] as const satisfies readonly (keyof DraftProvider)[];

export function patchAffectsPersistedProvider(
  patch: Partial<DraftProvider>,
): boolean {
  return PROVIDER_CONFIG_KEYS.some((key) => patch[key] !== undefined);
}

export type AliasBinding = { name: string; provider: string | null };

/** Liberal reader for `/admin/models` — v2 gateways reply with an
 * `AliasView[]`, v0.1 gateways with a `Record<alias, model>` (no provider
 * information → `provider: null`). Anything malformed yields `[]`. */
export function extractAliasBindings(data: unknown): AliasBinding[] {
  if (!data || typeof data !== "object") return [];
  const aliases = (data as { aliases?: unknown }).aliases;
  if (Array.isArray(aliases)) {
    const out: AliasBinding[] = [];
    for (const entry of aliases) {
      if (
        entry &&
        typeof entry === "object" &&
        typeof (entry as { name?: unknown }).name === "string"
      ) {
        const provider = (entry as { provider?: unknown }).provider;
        out.push({
          name: (entry as { name: string }).name,
          provider: typeof provider === "string" ? provider : null,
        });
      }
    }
    return out;
  }
  if (aliases && typeof aliases === "object") {
    return Object.keys(aliases).map((name) => ({ name, provider: null }));
  }
  return [];
}

/** Split model-add candidates into `safe` (no alias yet, or the alias
 * already routes to THIS provider — an idempotent rebind) and `conflicting`
 * (alias routes to a different — or unknown — provider). Upserting a
 * conflicting id would silently reroute every chat using that alias, so the
 * caller must skip those and tell the operator. */
export function partitionAliasCandidates(
  ids: string[],
  existing: AliasBinding[],
  providerName: string,
): { safe: string[]; conflicting: string[] } {
  const byName = new Map(existing.map((a) => [a.name, a]));
  const safe: string[] = [];
  const conflicting: string[] = [];
  for (const id of ids) {
    const bound = byName.get(id);
    if (!bound || bound.provider === providerName) {
      safe.push(id);
    } else {
      conflicting.push(id);
    }
  }
  return { safe, conflicting };
}

export type AddModelsGate = {
  canAdd: boolean;
  reason: "needsIdentity" | "disabled" | null;
};

/** Gate for the Add / Add-all controls. Identity problems (missing name /
 * base_url, param errors) rank ahead of the enabled gate; a disabled
 * provider is never built by the runtime registry, so aliases bound to it
 * would not resolve. */
export function computeAddModelsGate(args: {
  nameOk: boolean;
  baseUrlOk: boolean;
  hasErrors: boolean;
  enabled: boolean;
}): AddModelsGate {
  if (!args.nameOk || !args.baseUrlOk || args.hasErrors) {
    return { canAdd: false, reason: "needsIdentity" };
  }
  if (!args.enabled) {
    return { canAdd: false, reason: "disabled" };
  }
  return { canAdd: true, reason: null };
}
