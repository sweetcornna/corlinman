/**
 * useSetupStatus unit tests (PR5 provider-setup flow).
 *
 * The hook reads the shared `["admin","providers"]` / `["admin","models"]`
 * caches; both fetchers are mocked here. Locked-in semantics:
 *
 *   - configured = usable provider AND ≥1 alias AND non-empty default;
 *   - a "usable" provider is enabled, non-mock, and has a value/env key —
 *     OR owns an alias binding (the OAuth-provisioned signature: no
 *     api_key in config but the login wrote aliases bound to it);
 *   - the skip-onboarding mock bootstrap (provider "mock" + alias "mock"
 *     + default "mock") counts as NOT configured;
 *   - a failed query yields errored=true, configured=false.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { fetchProvidersMock, fetchModelsMock } = vi.hoisted(() => ({
  fetchProvidersMock: vi.fn(),
  fetchModelsMock: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return {
    ...actual,
    fetchProviders: fetchProvidersMock,
    fetchModels: fetchModelsMock,
  };
});

import { useSetupStatus } from "./use-setup-status";

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function provider(overrides: Record<string, unknown> = {}) {
  return {
    name: "openai",
    kind: "openai",
    enabled: true,
    base_url: null,
    api_key_source: "env",
    api_key_env_name: "OPENAI_API_KEY",
    params: {},
    params_schema: { type: "object", properties: {} },
    ...overrides,
  };
}

describe("useSetupStatus", () => {
  beforeEach(() => {
    fetchProvidersMock.mockResolvedValue([]);
    fetchModelsMock.mockResolvedValue({
      default: "",
      aliases: [],
      providers: [],
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("reports a pristine gateway as unconfigured", async () => {
    const { result } = renderHook(() => useSetupStatus(), { wrapper });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).toMatchObject({
      errored: false,
      configured: false,
      hasProvider: false,
      hasAliases: false,
      hasDefault: false,
      providerCount: 0,
      providerName: null,
      defaultModel: null,
    });
  });

  it("reports configured for an enabled keyed provider + alias + default", async () => {
    fetchProvidersMock.mockResolvedValue([provider()]);
    fetchModelsMock.mockResolvedValue({
      default: "gpt-4o",
      aliases: [
        {
          name: "gpt-4o",
          provider: "openai",
          model: "gpt-4o",
          params: {},
          effective_params_schema: {},
        },
      ],
      providers: [],
    });
    const { result } = renderHook(() => useSetupStatus(), { wrapper });
    await waitFor(() => expect(result.current.configured).toBe(true));
    expect(result.current).toMatchObject({
      hasProvider: true,
      hasAliases: true,
      hasDefault: true,
      providerCount: 1,
      providerName: "openai",
      defaultModel: "gpt-4o",
    });
  });

  it("treats a keyless OAuth-provisioned provider with alias bindings as usable", async () => {
    fetchProvidersMock.mockResolvedValue([
      provider({
        name: "anthropic",
        kind: "anthropic",
        api_key_source: "unset",
        api_key_env_name: null,
      }),
    ]);
    fetchModelsMock.mockResolvedValue({
      default: "claude-opus-4-8",
      aliases: [
        {
          name: "claude-opus-4-8",
          provider: "anthropic",
          model: "claude-opus-4-8",
          params: {},
          effective_params_schema: {},
        },
      ],
      providers: [],
    });
    const { result } = renderHook(() => useSetupStatus(), { wrapper });
    await waitFor(() => expect(result.current.configured).toBe(true));
    expect(result.current.providerName).toBe("anthropic");
  });

  it("does not count the skip-onboarding mock bootstrap as configured", async () => {
    fetchProvidersMock.mockResolvedValue([
      provider({ name: "mock", kind: "mock", api_key_source: "unset" }),
    ]);
    fetchModelsMock.mockResolvedValue({
      default: "mock",
      aliases: [
        {
          name: "mock",
          provider: "mock",
          model: "mock",
          params: {},
          effective_params_schema: {},
        },
      ],
      providers: [],
    });
    const { result } = renderHook(() => useSetupStatus(), { wrapper });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).toMatchObject({
      configured: false,
      hasProvider: false,
      hasAliases: false,
      hasDefault: false,
      providerCount: 1,
    });
  });

  it("ignores disabled and keyless providers without bindings", async () => {
    fetchProvidersMock.mockResolvedValue([
      provider({ enabled: false }),
      provider({ name: "bare", api_key_source: "unset" }),
    ]);
    fetchModelsMock.mockResolvedValue({
      default: "gpt-4o",
      aliases: [],
      providers: [],
    });
    const { result } = renderHook(() => useSetupStatus(), { wrapper });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hasProvider).toBe(false);
    expect(result.current.configured).toBe(false);
    expect(result.current.providerCount).toBe(2);
  });

  it("flags errored (and unconfigured) when a query fails", async () => {
    fetchProvidersMock.mockRejectedValue(new Error("503 pending"));
    const { result } = renderHook(() => useSetupStatus(), { wrapper });
    await waitFor(() => expect(result.current.errored).toBe(true));
    expect(result.current.configured).toBe(false);
  });

  it("also reads the legacy v0.1 string-map alias shape", async () => {
    fetchProvidersMock.mockResolvedValue([provider()]);
    fetchModelsMock.mockResolvedValue({
      default: "smart",
      aliases: { smart: "gpt-4o" },
      providers: [],
    });
    const { result } = renderHook(() => useSetupStatus(), { wrapper });
    await waitFor(() => expect(result.current.configured).toBe(true));
    expect(result.current.hasAliases).toBe(true);
  });
});
