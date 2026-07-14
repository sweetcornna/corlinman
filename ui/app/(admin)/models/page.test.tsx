/**
 * /models — "Models & Keys" hub composition tests (PR4 model-hub
 * consolidation).
 *
 * The section components are mocked; these tests only cover the thin
 * composition itself: ?tab= resolution, active-tab-only mounting, tab
 * clicks driving router.replace, and the onCustomProvidersChanged →
 * credentials invalidation wiring that used to live on /credentials.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { replaceMock, searchParamsRef } = vi.hoisted(() => ({
  replaceMock: vi.fn(),
  searchParamsRef: { current: new URLSearchParams() },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: replaceMock,
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => "/models",
  useSearchParams: () => searchParamsRef.current,
  useParams: () => ({}),
}));

vi.mock("@/components/model-hub/providers-admin-content", () => ({
  ProvidersAdminContent: ({
    onCustomProvidersChanged,
  }: {
    onCustomProvidersChanged?: () => void;
  }) => (
    <button
      type="button"
      data-testid="mock-providers-admin-content"
      onClick={() => onCustomProvidersChanged?.()}
    >
      providers admin
    </button>
  ),
}));
vi.mock("@/components/model-hub/oauth-panel", () => ({
  OAuthPanel: () => <div data-testid="mock-oauth-panel" />,
}));
vi.mock("@/components/model-hub/routing-section", () => ({
  RoutingSection: () => <div data-testid="mock-routing-section" />,
}));
vi.mock("@/components/model-hub/credentials-advanced", () => ({
  CredentialsAdvanced: () => <div data-testid="mock-credentials-advanced" />,
}));
// PR5 — the guided flow itself has its own suite; here it's a marker so we
// can assert where the hub mounts it (inline empty state vs dialog).
vi.mock("@/components/model-hub/provider-setup-flow", () => ({
  ProviderSetupFlow: ({ variant }: { variant?: string }) => (
    <div data-testid="mock-provider-setup-flow" data-variant={variant} />
  ),
}));

// useSetupStatus reads the shared caches through these fetchers; route
// them through mutable refs so tests can flip provider presence/errors.
const { providersRef, modelsRef } = vi.hoisted(() => ({
  providersRef: {
    current: { value: [] as unknown[], error: null as Error | null },
  },
  modelsRef: {
    current: { default: "", aliases: [], providers: [] } as unknown,
  },
}));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return {
    ...actual,
    fetchProviders: vi.fn(async () => {
      if (providersRef.current.error) throw providersRef.current.error;
      return providersRef.current.value;
    }),
    fetchModels: vi.fn(async () => modelsRef.current),
  };
});

import ModelsPage from "./page";

let queryClient: QueryClient;

function renderPage() {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

describe("ModelsPage (Models & Keys hub)", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    searchParamsRef.current = new URLSearchParams();
    providersRef.current = { value: [], error: null };
    modelsRef.current = { default: "", aliases: [], providers: [] };
  });

  afterEach(() => {
    cleanup();
  });

  it("defaults to the providers tab (content + oauth panel, others unmounted)", () => {
    renderPage();
    expect(
      screen.getByTestId("mock-providers-admin-content"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("mock-oauth-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-routing-section")).toBeNull();
    expect(screen.queryByTestId("mock-credentials-advanced")).toBeNull();
    expect(screen.getByTestId("model-hub-tab-providers")).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("mounts only the routing section when ?tab=routing", () => {
    searchParamsRef.current = new URLSearchParams("tab=routing");
    renderPage();
    expect(screen.getByTestId("mock-routing-section")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-providers-admin-content")).toBeNull();
    expect(screen.queryByTestId("mock-credentials-advanced")).toBeNull();
  });

  it("mounts only the advanced section when ?tab=advanced", () => {
    searchParamsRef.current = new URLSearchParams("tab=advanced");
    renderPage();
    expect(screen.getByTestId("mock-credentials-advanced")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-providers-admin-content")).toBeNull();
    expect(screen.queryByTestId("mock-routing-section")).toBeNull();
  });

  it("falls back to the providers tab for an unknown ?tab value", () => {
    searchParamsRef.current = new URLSearchParams("tab=bogus");
    renderPage();
    expect(
      screen.getByTestId("mock-providers-admin-content"),
    ).toBeInTheDocument();
  });

  it("clicking a tab replaces the URL with the ?tab deep link", () => {
    renderPage();
    fireEvent.click(screen.getByTestId("model-hub-tab-routing"));
    expect(replaceMock).toHaveBeenCalledWith("/models?tab=routing", {
      scroll: false,
    });
    fireEvent.click(screen.getByTestId("model-hub-tab-advanced"));
    expect(replaceMock).toHaveBeenCalledWith("/models?tab=advanced", {
      scroll: false,
    });
  });

  it("invalidates the credentials cache when custom providers change", () => {
    renderPage();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    fireEvent.click(screen.getByTestId("mock-providers-admin-content"));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["admin", "credentials"] });
  });

  it("opens the quick-setup dialog from the header button", async () => {
    renderPage();
    expect(screen.queryByTestId("mock-provider-setup-flow")).toBeNull();
    fireEvent.click(screen.getByTestId("model-hub-quick-setup-btn"));
    const flow = await screen.findByTestId("mock-provider-setup-flow");
    expect(flow).toHaveAttribute("data-variant", "dialog");
    expect(
      screen.getByTestId("model-hub-quick-setup-dialog"),
    ).toBeInTheDocument();
  });

  it("leads the providers tab with the inline flow when no provider exists", async () => {
    renderPage();
    const inline = await screen.findByTestId("model-hub-inline-setup");
    expect(inline).toBeInTheDocument();
    expect(
      screen.getByTestId("mock-provider-setup-flow"),
    ).toHaveAttribute("data-variant", "page");
    // The regular providers surface still renders below it.
    expect(
      screen.getByTestId("mock-providers-admin-content"),
    ).toBeInTheDocument();
  });

  it("hides the inline flow when a provider is already registered", async () => {
    providersRef.current = {
      value: [
        {
          name: "openai",
          kind: "openai",
          enabled: true,
          base_url: null,
          api_key_source: "env",
          api_key_env_name: "OPENAI_API_KEY",
          params: {},
          params_schema: { type: "object", properties: {} },
        },
      ],
      error: null,
    };
    renderPage();
    // Wait for the status queries to settle, then assert absence.
    await waitFor(() => {
      expect(screen.queryByTestId("model-hub-inline-setup")).toBeNull();
    });
    expect(
      screen.getByTestId("mock-providers-admin-content"),
    ).toBeInTheDocument();
  });

  it("does not flash the inline flow while the status queries error out", async () => {
    providersRef.current = { value: [], error: new Error("503 pending") };
    renderPage();
    await waitFor(() => {
      expect(
        screen.getByTestId("mock-providers-admin-content"),
      ).toBeInTheDocument();
    });
    expect(screen.queryByTestId("model-hub-inline-setup")).toBeNull();
  });
});
