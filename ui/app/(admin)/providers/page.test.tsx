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

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchProviders: () => fetchProvidersMock(),
    listCustomProviders: () => listCustomProvidersMock(),
    upsertProvider: vi.fn(),
    deleteProvider: vi.fn(),
    deleteCustomProvider: vi.fn(),
    getProviderModels: (name: string) => getProviderModelsMock(name),
    probeProviderModels: (body: unknown) => probeProviderModelsMock(body),
  };
});

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));

import { ProvidersAdminContent } from "./page";

function renderPage() {
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
    getProviderModelsMock.mockClear();
    probeProviderModelsMock.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("fetches models from the add-provider draft without saving the provider", async () => {
    renderPage();

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
    renderPage();

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
    renderPage();

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
});
