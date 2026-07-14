/**
 * Unit tests for the pure helpers behind the provider editor's model-add
 * flow (dirty-draft re-persistence, alias-conflict skipping, enabled gate).
 *
 * Moved from `app/(admin)/providers/page.test.tsx` in the PR4 model-hub
 * consolidation — the helpers now live in `../alias-helpers`.
 */

import { describe, expect, it } from "vitest";

import {
  computeAddModelsGate,
  extractAliasBindings,
  partitionAliasCandidates,
  patchAffectsPersistedProvider,
} from "../alias-helpers";

describe("patchAffectsPersistedProvider (Bug 1 helper)", () => {
  it.each([
    [{ base_url: "https://x" }],
    [{ api_key_value: "sk-1" }],
    [{ api_key_env_name: "KEY" }],
    [{ api_key_source: "value" as const }],
    [{ kind: "openai" as const }],
    [{ params: { a: 1 } }],
    [{ enabled: false }],
    [{ name: "n" }],
  ])("flags provider-config patch %j", (patch) => {
    expect(patchAffectsPersistedProvider(patch)).toBe(true);
  });

  it("ignores an empty patch", () => {
    expect(patchAffectsPersistedProvider({})).toBe(false);
  });
});

describe("extractAliasBindings / partitionAliasCandidates (Bug 2 helpers)", () => {
  it("reads the v2 aliases array shape", () => {
    expect(
      extractAliasBindings({
        aliases: [
          { name: "a", provider: "p1", model: "a" },
          { name: "b", provider: null, model: "b" },
        ],
      }),
    ).toEqual([
      { name: "a", provider: "p1" },
      { name: "b", provider: null },
    ]);
  });

  it("treats legacy record aliases as bound to an unknown provider", () => {
    expect(extractAliasBindings({ aliases: { a: "gpt-4o" } })).toEqual([
      { name: "a", provider: null },
    ]);
  });

  it("returns [] for undefined / malformed data", () => {
    expect(extractAliasBindings(undefined)).toEqual([]);
    expect(extractAliasBindings({ aliases: 42 })).toEqual([]);
  });

  it("partitions safe vs conflicting candidates", () => {
    const existing = [
      { name: "mine", provider: "me" },
      { name: "theirs", provider: "other" },
      { name: "unbound", provider: null },
    ];
    expect(
      partitionAliasCandidates(
        ["mine", "theirs", "unbound", "fresh"],
        existing,
        "me",
      ),
    ).toEqual({
      safe: ["mine", "fresh"],
      conflicting: ["theirs", "unbound"],
    });
  });
});

describe("computeAddModelsGate (Bug 3 helper)", () => {
  it("passes when identity is valid and the provider is enabled", () => {
    expect(
      computeAddModelsGate({
        nameOk: true,
        baseUrlOk: true,
        hasErrors: false,
        enabled: true,
      }),
    ).toEqual({ canAdd: true, reason: null });
  });

  it("blocks a disabled provider", () => {
    expect(
      computeAddModelsGate({
        nameOk: true,
        baseUrlOk: true,
        hasErrors: false,
        enabled: false,
      }),
    ).toEqual({ canAdd: false, reason: "disabled" });
  });

  it("reports identity problems ahead of the enabled gate", () => {
    expect(
      computeAddModelsGate({
        nameOk: false,
        baseUrlOk: true,
        hasErrors: false,
        enabled: false,
      }),
    ).toEqual({ canAdd: false, reason: "needsIdentity" });
  });
});
