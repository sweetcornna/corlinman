"use client";

/**
 * useSetupStatus — "is this deployment ready to chat?" derived from the
 * SAME react-query caches the model-hub components populate
 * (`["admin", "providers"]` ← fetchProviders, `["admin", "models"]` ←
 * fetchModels), so mounting the hook next to those surfaces costs no
 * extra requests and updates live as the setup flow invalidates them.
 *
 * configured = at least one enabled non-"mock" provider with a usable
 * key AND at least one alias AND a non-empty default model. "Usable key"
 * means api_key source value/env — or an OAuth-provisioned provider,
 * which carries no api_key in config but owns alias bindings (the OAuth
 * login backend always provisions aliases bound to the provider it
 * writes, so "enabled + keyless + owns an alias" is the OAuth signature).
 */

import { useQuery } from "@tanstack/react-query";

import { fetchModels, fetchProviders, type ProviderView } from "@/lib/api";
import { extractAliasBindings } from "@/components/model-hub/alias-helpers";

export interface SetupStatus {
  /** True while either underlying query is still pending. */
  loading: boolean;
  /** True when at least one query failed (gateway unreachable / auth). */
  errored: boolean;
  /** hasProvider && hasAliases && hasDefault. */
  configured: boolean;
  /** ≥1 enabled non-mock provider with a usable key (or OAuth-provisioned). */
  hasProvider: boolean;
  /** ≥1 model alias registered. */
  hasAliases: boolean;
  /** models.default is non-empty. */
  hasDefault: boolean;
  /** Raw provider count (any state) — drives "no provider at all" empty states. */
  providerCount: number;
  /** Name of the first usable provider (for summary cards). */
  providerName: string | null;
  /** Provider that actually serves `models.default` (resolved via the
   * default alias's binding). Distinct from `providerName` when several
   * providers exist — this is the one image-gen "reuse" should bind to. */
  defaultProviderName: string | null;
  /** Current default model alias ("" → null). */
  defaultModel: string | null;
}

function isMock(p: ProviderView): boolean {
  return p.name === "mock" || (p.kind as string) === "mock";
}

export function useSetupStatus(): SetupStatus {
  const providersQuery = useQuery({
    queryKey: ["admin", "providers"],
    queryFn: fetchProviders,
    retry: false,
  });
  const modelsQuery = useQuery({
    queryKey: ["admin", "models"],
    queryFn: fetchModels,
    retry: false,
  });

  const loading = providersQuery.isPending || modelsQuery.isPending;
  const errored = providersQuery.isError || modelsQuery.isError;

  const providers = providersQuery.data ?? [];
  // The skip-onboarding bootstrap seeds an alias "mock" → provider "mock";
  // it must not tick the "models added" box, so mock bindings are excluded
  // throughout (same rule as the provider check below).
  const aliases = extractAliasBindings(modelsQuery.data).filter(
    (a) => a.name !== "mock" && a.provider !== "mock",
  );
  const boundProviders = new Set(
    aliases.map((a) => a.provider).filter((p): p is string => !!p),
  );

  const usable = providers.find(
    (p) =>
      p.enabled &&
      !isMock(p) &&
      (p.api_key_source !== "unset" || boundProviders.has(p.name)),
  );

  const rawDefault =
    typeof modelsQuery.data?.default === "string"
      ? modelsQuery.data.default.trim()
      : "";
  // A default of "mock" is the skip-onboarding bootstrap, not a real setup.
  const hasDefault = rawDefault.length > 0 && rawDefault !== "mock";

  const hasProvider = !!usable;
  const hasAliases = aliases.length > 0;

  // The provider bound to the default alias — the one chat actually
  // routes through. Falls back to the first usable provider when the
  // default is a bare model id with no explicit alias binding.
  const defaultBinding = hasDefault
    ? aliases.find((a) => a.name === rawDefault)?.provider ?? null
    : null;
  const defaultProviderName = defaultBinding ?? usable?.name ?? null;

  return {
    loading,
    errored,
    configured: !loading && hasProvider && hasAliases && hasDefault,
    hasProvider,
    hasAliases,
    hasDefault,
    providerCount: providers.length,
    providerName: usable?.name ?? null,
    defaultProviderName,
    defaultModel: hasDefault ? rawDefault : null,
  };
}
