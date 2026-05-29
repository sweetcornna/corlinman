import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nextProvider } from "react-i18next";
import * as React from "react";

import { i18next, initI18n } from "@/lib/i18n";
import type {
  AgentBindingPatch,
  AgentBindingsResponse,
  AgentSummary,
  ModelsResponseV2,
} from "@/lib/api";

const apiFetchMock = vi.fn(
  async (_path: string): Promise<AgentSummary[]> => [
    {
      name: "researcher",
      file_path: "agents/researcher.yaml",
      bytes: 128,
      last_modified: "2026-01-01T00:00:00Z",
      source: "user",
      description: "Research agent",
    },
  ],
);

const listAgentBindingsMock = vi.fn(
  async (): Promise<AgentBindingsResponse> => ({
    agents: [
      {
        name: "researcher",
        description: "Research agent",
        model: "gpt-4o",
        provider: "openai",
        show_action_trace: true,
      },
    ],
  }),
);

const fetchModelsV2Mock = vi.fn(
  async (): Promise<ModelsResponseV2> => ({
    default: "gpt-4o",
    providers: [],
    aliases: [
      {
        name: "gpt-4o",
        provider: "openai",
        model: "gpt-4o",
        params: {},
        effective_params_schema: {},
      },
    ],
  }),
);

const setAgentModelBindingMock = vi.fn(
  async (
    name: string,
    patch: AgentBindingPatch,
  ): Promise<{
    status: string;
    name: string;
    model: string | null;
    provider: string | null;
    show_action_trace: boolean;
  }> => ({
    status: "ok",
    name,
    model: patch.model,
    provider: patch.provider,
    show_action_trace: patch.show_action_trace,
  }),
);

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: (path: string) => apiFetchMock(path),
    deleteAgent: vi.fn(),
    fetchModelsV2: () => fetchModelsV2Mock(),
    listAgentBindings: () => listAgentBindingsMock(),
    setAgentModelBinding: (
      name: string,
      patch: AgentBindingPatch,
    ) => setAgentModelBindingMock(name, patch),
  };
});

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));

import AgentsPage from "./page";

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
        <AgentsPage />
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  initI18n();
  i18next.changeLanguage("en");
  apiFetchMock.mockClear();
  listAgentBindingsMock.mockClear();
  fetchModelsV2Mock.mockClear();
  setAgentModelBindingMock.mockClear();
});

afterEach(() => {
  cleanup();
});

describe("AgentsPage action trace switch", () => {
  it("patches the row binding with the current model and provider", async () => {
    renderPage();

    const traceSwitch = await screen.findByTestId(
      "agent-action-trace-researcher",
    );
    expect(traceSwitch).toHaveAttribute("aria-checked", "true");

    fireEvent.click(traceSwitch);

    await waitFor(() => {
      expect(setAgentModelBindingMock).toHaveBeenCalledWith("researcher", {
        model: "gpt-4o",
        provider: "openai",
        show_action_trace: false,
      });
    });
  });
});
