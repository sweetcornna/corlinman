/**
 * Behavior tests for the composed providers admin surface (table + editor
 * dialog + model discovery / add-models flow).
 *
 * Moved from `app/(admin)/providers/page.test.tsx` in the PR4 model-hub
 * consolidation — `ProvidersAdminContent` now lives in
 * `../providers-admin-content` (the page is a redirect stub).
 */

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18next, initI18n } from "@/lib/i18n";
import type { ProviderView } from "@/lib/api";

const fetchProvidersMock = vi.fn(async (): Promise<ProviderView[]> => []);
const listCustomProvidersMock = vi.fn(async () => []);
const fetchModelsMock = vi.fn(async (): Promise<unknown> => ({
  default: "",
  providers: [],
  aliases: [],
}));
const getProviderModelsMock = vi.fn(async (name: string) => ({
  models: [{ id: `${name}-saved-model`, display_name: "Saved Model" }],
}));
const probeProviderModelsMock = vi.fn(async (body: unknown) => {
  void body;
  return {
    models: [
      { id: "relay-model-a", display_name: "Relay Model A" },
      { id: "relay-model-b", display_name: "Relay Model B" },
    ],
  };
});
const upsertProviderMock = vi.fn(async (body: unknown) => {
  void body;
  return {};
});
const upsertAliasMock = vi.fn(async (body: unknown) => {
  void body;
  return {};
});

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchProviders: () => fetchProvidersMock(),
    listCustomProviders: () => listCustomProvidersMock(),
    fetchModels: () => fetchModelsMock(),
    upsertProvider: (body: unknown) => upsertProviderMock(body),
    upsertAlias: (body: unknown) => upsertAliasMock(body),
    deleteProvider: vi.fn(),
    deleteCustomProvider: vi.fn(),
    getProviderModels: (name: string) => getProviderModelsMock(name),
    probeProviderModels: (body: unknown) => probeProviderModelsMock(body),
  };
});

const toastSuccessMock = vi.fn();
const toastErrorMock = vi.fn();
const toastWarningMock = vi.fn();

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: (...args: unknown[]) => toastSuccessMock(...args),
    error: (...args: unknown[]) => toastErrorMock(...args),
    warning: (...args: unknown[]) => toastWarningMock(...args),
  }),
}));

import { ProvidersAdminContent } from "../providers-admin-content";

function renderContent() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18next}>
        <ProvidersAdminContent />
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const STORED_LITERAL_PROVIDER: ProviderView = {
  name: "relay",
  kind: "openai_compatible",
  enabled: true,
  base_url: "https://saved.example/v1",
  api_key_source: "value",
  api_key_env_name: null,
  params: {},
  params_schema: { type: "object", properties: {} },
};

describe("ProvidersAdminContent model discovery", () => {
  beforeEach(() => {
    initI18n();
    void i18next.changeLanguage("zh-CN");
    fetchProvidersMock.mockClear();
    listCustomProvidersMock.mockClear();
    fetchModelsMock.mockClear();
    fetchModelsMock.mockResolvedValue({ default: "", providers: [], aliases: [] });
    getProviderModelsMock.mockClear();
    probeProviderModelsMock.mockClear();
    upsertProviderMock.mockClear();
    upsertAliasMock.mockClear();
    toastSuccessMock.mockClear();
    toastErrorMock.mockClear();
    toastWarningMock.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("fetches models from the add-provider draft without saving the provider", async () => {
    renderContent();

    fireEvent.click(await screen.findByTestId("providers-add-btn"));

    fireEvent.change(screen.getByLabelText("名称"), {
      target: { value: "relay" },
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://relay.example/v1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "字面量" }));
    fireEvent.change(screen.getByPlaceholderText("sk-..."), {
      target: { value: "sk-test" },
    });

    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));

    await waitFor(() => {
      expect(probeProviderModelsMock).toHaveBeenCalledWith({
        kind: "openai_compatible",
        base_url: "https://relay.example/v1",
        api_key: { value: "sk-test" },
        params: {},
      });
    });
    expect(await screen.findByText("relay-model-a")).toBeInTheDocument();
    expect(screen.getByText("relay-model-b")).toBeInTheDocument();
  });

  it("fetches models from edited draft fields when a saved literal key is hidden", async () => {
    fetchProvidersMock.mockResolvedValueOnce([STORED_LITERAL_PROVIDER]);
    renderContent();

    await screen.findByTestId("provider-row-relay");
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://edited.example/v1" },
    });

    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));

    await waitFor(() => {
      expect(probeProviderModelsMock).toHaveBeenCalledWith({
        kind: "openai_compatible",
        base_url: "https://edited.example/v1",
        existing_name: "relay",
        params: {},
      });
    });
    expect(getProviderModelsMock).not.toHaveBeenCalled();
  });

  it("ignores stale model discovery results after the draft changes", async () => {
    const pending = deferred<{
      models: { id: string; display_name: string }[];
    }>();
    probeProviderModelsMock.mockImplementationOnce(async () => pending.promise);
    renderContent();

    fireEvent.click(await screen.findByTestId("providers-add-btn"));
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://old.example/v1" },
    });
    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));

    await waitFor(() => {
      expect(probeProviderModelsMock).toHaveBeenCalledTimes(1);
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://new.example/v1" },
    });

    await act(async () => {
      pending.resolve({
        models: [{ id: "stale-model", display_name: "Stale Model" }],
      });
      await pending.promise;
    });

    expect(screen.queryByText("stale-model")).not.toBeInTheDocument();
    expect(screen.getByText("尚未获取模型。")).toBeInTheDocument();
  });

  it("adds a fetched model as an alias bound to the draft provider", async () => {
    renderContent();

    fireEvent.click(await screen.findByTestId("providers-add-btn"));
    fireEvent.change(screen.getByLabelText("名称"), {
      target: { value: "relay" },
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://relay.example/v1" },
    });
    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));
    await screen.findByText("relay-model-a");

    fireEvent.click(screen.getByTestId("provider-model-add-relay-model-a"));

    await waitFor(() => {
      // The brand-new draft is persisted exactly once so the alias has a
      // real provider to reference.
      expect(upsertProviderMock).toHaveBeenCalledTimes(1);
      expect(upsertAliasMock).toHaveBeenCalledWith({
        name: "relay-model-a",
        provider: "relay",
        model: "relay-model-a",
      });
    });
    // The row flips to the "added" affordance.
    expect(
      await screen.findByTestId("provider-model-added-relay-model-a"),
    ).toBeInTheDocument();
  });

  it("adds every fetched model with a single provider upsert via Add all", async () => {
    renderContent();

    fireEvent.click(await screen.findByTestId("providers-add-btn"));
    fireEvent.change(screen.getByLabelText("名称"), {
      target: { value: "relay" },
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://relay.example/v1" },
    });
    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));
    await screen.findByText("relay-model-a");

    fireEvent.click(screen.getByTestId("provider-add-all-models-btn"));

    await waitFor(() => {
      expect(upsertProviderMock).toHaveBeenCalledTimes(1);
      expect(upsertAliasMock).toHaveBeenCalledWith({
        name: "relay-model-a",
        provider: "relay",
        model: "relay-model-a",
      });
      expect(upsertAliasMock).toHaveBeenCalledWith({
        name: "relay-model-b",
        provider: "relay",
        model: "relay-model-b",
      });
    });
  });

  it("disables the add control until the provider has a name", async () => {
    renderContent();

    fireEvent.click(await screen.findByTestId("providers-add-btn"));
    // Base URL set but no name yet → cannot bind an alias.
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://relay.example/v1" },
    });
    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));
    await screen.findByText("relay-model-a");

    expect(screen.getByTestId("provider-model-add-relay-model-a")).toBeDisabled();
    expect(screen.getByTestId("provider-add-all-models-btn")).toBeDisabled();
  });

  // ------------------------------------------------------------------
  // Bug 1 — a dirty draft must be re-persisted before aliases bind
  // ------------------------------------------------------------------

  const STORED_ENV_PROVIDER: ProviderView = {
    name: "relay",
    kind: "openai_compatible",
    enabled: true,
    base_url: "https://saved.example/v1",
    api_key_source: "env",
    api_key_env_name: "RELAY_KEY",
    params: {},
    params_schema: { type: "object", properties: {} },
  };

  it("re-upserts the provider before binding aliases after base_url changed (Bug 1)", async () => {
    fetchProvidersMock.mockResolvedValueOnce([STORED_ENV_PROVIDER]);
    renderContent();

    await screen.findByTestId("provider-row-relay");
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://edited.example/v2" },
    });

    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));
    fireEvent.click(
      await screen.findByTestId("provider-model-add-relay-model-a"),
    );

    await waitFor(() => {
      expect(upsertAliasMock).toHaveBeenCalledTimes(1);
    });
    // The edited config must be persisted so the alias binds against the
    // NEW base_url, not the stale stored block.
    expect(upsertProviderMock).toHaveBeenCalledTimes(1);
    expect(upsertProviderMock.mock.calls[0]![0]).toMatchObject({
      name: "relay",
      base_url: "https://edited.example/v2",
    });
    // …and persisted BEFORE the alias references it.
    expect(upsertProviderMock.mock.invocationCallOrder[0]!).toBeLessThan(
      upsertAliasMock.mock.invocationCallOrder[0]!,
    );
  });

  it("does not re-upsert an untouched editing provider on add", async () => {
    fetchProvidersMock.mockResolvedValueOnce([STORED_ENV_PROVIDER]);
    renderContent();

    await screen.findByTestId("provider-row-relay");
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));

    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));
    fireEvent.click(
      await screen.findByTestId("provider-model-add-relay-model-a"),
    );

    await waitFor(() => {
      expect(upsertAliasMock).toHaveBeenCalledTimes(1);
    });
    // Nothing changed this session → the stored block stays untouched.
    expect(upsertProviderMock).not.toHaveBeenCalled();
  });

  // ------------------------------------------------------------------
  // Bug 2 — never silently rebind an alias routed to another provider
  // ------------------------------------------------------------------

  it("skips candidates whose alias is bound to a different provider (Bug 2)", async () => {
    fetchModelsMock.mockResolvedValue({
      default: "",
      providers: [],
      aliases: [
        // Same model id already routed to ANOTHER provider → conflict.
        {
          name: "relay-model-a",
          provider: "other",
          model: "relay-model-a",
          params: {},
          effective_params_schema: {},
        },
        // Already routed to THIS provider → safe (idempotent rebind).
        {
          name: "relay-model-b",
          provider: "relay",
          model: "relay-model-b",
          params: {},
          effective_params_schema: {},
        },
      ],
    });
    renderContent();

    fireEvent.click(await screen.findByTestId("providers-add-btn"));
    fireEvent.change(screen.getByLabelText("名称"), {
      target: { value: "relay" },
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://relay.example/v1" },
    });
    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));
    await screen.findByText("relay-model-a");

    fireEvent.click(screen.getByTestId("provider-add-all-models-btn"));

    await waitFor(() => {
      expect(toastSuccessMock).toHaveBeenCalledTimes(1);
    });
    // Only the safe id was written; the conflicting alias was left alone.
    expect(
      upsertAliasMock.mock.calls.map((c) => (c[0] as { name: string }).name),
    ).toEqual(["relay-model-b"]);
    // The skip is surfaced to the operator with the conflict count.
    expect(toastWarningMock).toHaveBeenCalledTimes(1);
    expect(String(toastWarningMock.mock.calls[0]![0])).toContain("1");
    // The conflicting model stays addable rather than flipping to "added".
    expect(
      screen.getByTestId("provider-model-add-relay-model-a"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("provider-model-added-relay-model-b"),
    ).toBeInTheDocument();
  });

  it("adds nothing and never persists the draft when every candidate conflicts", async () => {
    fetchModelsMock.mockResolvedValue({
      default: "",
      providers: [],
      aliases: [
        {
          name: "relay-model-a",
          provider: "other",
          model: "relay-model-a",
          params: {},
          effective_params_schema: {},
        },
      ],
    });
    probeProviderModelsMock.mockResolvedValueOnce({
      models: [{ id: "relay-model-a", display_name: "Relay Model A" }],
    });
    renderContent();

    fireEvent.click(await screen.findByTestId("providers-add-btn"));
    fireEvent.change(screen.getByLabelText("名称"), {
      target: { value: "relay" },
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://relay.example/v1" },
    });
    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));
    await screen.findByText("relay-model-a");

    fireEvent.click(screen.getByTestId("provider-model-add-relay-model-a"));

    await waitFor(() => {
      expect(toastWarningMock).toHaveBeenCalledTimes(1);
    });
    expect(upsertAliasMock).not.toHaveBeenCalled();
    expect(upsertProviderMock).not.toHaveBeenCalled();
    expect(toastSuccessMock).not.toHaveBeenCalled();
  });

  // ------------------------------------------------------------------
  // Bug 3 — Add / Add-all gate on draft.enabled
  // ------------------------------------------------------------------

  it("disables Add and Add-all while the draft provider is disabled (Bug 3)", async () => {
    renderContent();

    fireEvent.click(await screen.findByTestId("providers-add-btn"));
    fireEvent.change(screen.getByLabelText("名称"), {
      target: { value: "relay" },
    });
    fireEvent.change(screen.getByLabelText("Base URL"), {
      target: { value: "https://relay.example/v1" },
    });
    // Toggle enabled → off (the dialog's only switch).
    fireEvent.click(screen.getByRole("switch"));

    fireEvent.click(screen.getByTestId("provider-fetch-models-btn"));
    await screen.findByText("relay-model-a");

    expect(
      screen.getByTestId("provider-model-add-relay-model-a"),
    ).toBeDisabled();
    expect(screen.getByTestId("provider-add-all-models-btn")).toBeDisabled();
  });
});
