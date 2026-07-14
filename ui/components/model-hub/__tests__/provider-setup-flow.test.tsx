/**
 * ProviderSetupFlow state-machine tests (PR5).
 *
 * All api calls are mocked; the OAuthLoginModal is stubbed to a button
 * that fires `onSuccess` so the OAuth skip-to-step-5 path is drivable
 * without a PKCE handshake.
 *
 * Locked-in contracts:
 *   - key happy path: preset → key → probe → pick 2 models → default;
 *     the provider is upserted ONCE, each picked model becomes an alias
 *     bound to it, and the default is saved via `setDefaultModel` (the
 *     `{default}`-only body) — NEVER via the bulk `updateAliases` write
 *     (which drops omitted alias names).
 *   - OAuth path: login success skips straight to step 5 (the backend
 *     provisions provider + aliases + default) without probing or
 *     upserting anything client-side.
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

const {
  fetchModelsMock,
  probeMock,
  upsertProviderMock,
  upsertAliasMock,
  setDefaultModelMock,
  updateAliasesMock,
} = vi.hoisted(() => ({
  fetchModelsMock: vi.fn(),
  probeMock: vi.fn(),
  upsertProviderMock: vi.fn(),
  upsertAliasMock: vi.fn(),
  setDefaultModelMock: vi.fn(),
  updateAliasesMock: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return {
    ...actual,
    fetchModels: fetchModelsMock,
    probeProviderModels: probeMock,
    upsertProvider: upsertProviderMock,
    upsertAlias: upsertAliasMock,
    setDefaultModel: setDefaultModelMock,
    updateAliases: updateAliasesMock,
  };
});

vi.mock("@/components/admin/oauth-login-modal", () => ({
  OAuthLoginModal: ({
    open,
    provider,
    onSuccess,
  }: {
    open: boolean;
    provider?: string;
    onSuccess?: () => void;
  }) =>
    open ? (
      <button
        type="button"
        data-testid="mock-oauth-modal"
        data-provider={provider}
        onClick={() => onSuccess?.()}
      >
        oauth
      </button>
    ) : null,
}));

import {
  ProviderSetupFlow,
  type SetupFlowStatus,
} from "../provider-setup-flow";

const EMPTY_MODELS = {
  default: "",
  aliases: [] as unknown[],
  providers: [] as unknown[],
};

function renderFlow(onStatusChange?: (s: SetupFlowStatus) => void) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <ProviderSetupFlow onStatusChange={onStatusChange} />
    </QueryClientProvider>,
  );
}

function flowStep(): string | null {
  return screen
    .getByTestId("provider-setup-flow")
    .getAttribute("data-step");
}

describe("ProviderSetupFlow", () => {
  beforeEach(() => {
    fetchModelsMock.mockResolvedValue({ ...EMPTY_MODELS });
    probeMock.mockResolvedValue({
      models: [{ id: "gpt-4o" }, { id: "gpt-4o-mini" }, { id: "o1" }],
    });
    upsertProviderMock.mockResolvedValue({});
    upsertAliasMock.mockResolvedValue({});
    setDefaultModelMock.mockResolvedValue({
      status: "ok",
      default: "gpt-4o-mini",
      aliases: {},
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("walks the key happy path and saves the default via {default}-only", async () => {
    const statuses: SetupFlowStatus[] = [];
    renderFlow((s) => statuses.push(s));

    // Step 1 — preset grid; pick OpenAI.
    expect(flowStep()).toBe("1");
    fireEvent.click(screen.getByTestId("setup-preset-openai"));

    // Step 2 — provider name prefilled from the preset id, key input.
    expect(flowStep()).toBe("2");
    expect(screen.getByTestId("setup-name-input")).toHaveValue("openai");
    fireEvent.change(screen.getByTestId("setup-key-input"), {
      target: { value: "sk-test-not-a-real-key" },
    });
    fireEvent.click(screen.getByTestId("setup-auth-next"));

    // Step 3 — probe doubles as test-connection.
    expect(flowStep()).toBe("3");
    fireEvent.click(screen.getByTestId("setup-probe-btn"));
    await waitFor(() => {
      expect(flowStep()).toBe("4");
    });
    expect(probeMock).toHaveBeenCalledTimes(1);
    expect(probeMock.mock.calls[0]![0]).toMatchObject({
      kind: "openai",
      api_key: { value: "sk-test-not-a-real-key" },
    });

    // Step 4 — pick two of the three models.
    fireEvent.click(screen.getByTestId("setup-model-checkbox-gpt-4o"));
    fireEvent.click(screen.getByTestId("setup-model-checkbox-gpt-4o-mini"));
    fireEvent.click(screen.getByTestId("setup-add-models-btn"));

    await waitFor(() => {
      expect(flowStep()).toBe("5");
    });
    // Provider persisted exactly once, then one alias per picked model,
    // bound to it.
    expect(upsertProviderMock).toHaveBeenCalledTimes(1);
    expect(upsertProviderMock.mock.calls[0]![0]).toMatchObject({
      name: "openai",
      kind: "openai",
      api_key: { value: "sk-test-not-a-real-key" },
    });
    expect(upsertAliasMock).toHaveBeenCalledTimes(2);
    expect(upsertAliasMock).toHaveBeenCalledWith({
      name: "gpt-4o",
      provider: "openai",
      model: "gpt-4o",
    });
    expect(upsertAliasMock).toHaveBeenCalledWith({
      name: "gpt-4o-mini",
      provider: "openai",
      model: "gpt-4o-mini",
    });

    // Step 5 — first added alias preselected; switch to the mini model.
    expect(screen.getByTestId("setup-default-radio-gpt-4o")).toBeChecked();
    fireEvent.click(screen.getByTestId("setup-default-radio-gpt-4o-mini"));
    fireEvent.click(screen.getByTestId("setup-save-default-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("setup-done")).toBeInTheDocument();
    });
    // THE contract: default saved via the {default}-only wrapper, never a
    // bulk alias write (which would wipe omitted alias names).
    expect(setDefaultModelMock).toHaveBeenCalledTimes(1);
    expect(setDefaultModelMock).toHaveBeenCalledWith("gpt-4o-mini");
    expect(updateAliasesMock).not.toHaveBeenCalled();

    const final = statuses[statuses.length - 1]!;
    expect(final).toMatchObject({
      providerRegistered: true,
      testPassed: true,
      modelsAdded: true,
      defaultSet: true,
      providerName: "openai",
    });
  });

  it("surfaces a probe failure inline and stays on step 3", async () => {
    probeMock.mockResolvedValue({ models: [], error: "401 unauthorized" });
    renderFlow();

    fireEvent.click(screen.getByTestId("setup-preset-deepseek"));
    fireEvent.change(screen.getByTestId("setup-key-input"), {
      target: { value: "sk-bad" },
    });
    fireEvent.click(screen.getByTestId("setup-auth-next"));
    fireEvent.click(screen.getByTestId("setup-probe-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("setup-probe-error")).toHaveTextContent(
        "401 unauthorized",
      );
    });
    expect(flowStep()).toBe("3");
  });

  it("gates step 2 continue on a base_url for openai_compatible presets", () => {
    renderFlow();
    fireEvent.click(screen.getByTestId("setup-preset-custom"));
    // Custom preset: empty name + empty base_url → gated.
    expect(screen.getByTestId("setup-auth-next")).toBeDisabled();
    fireEvent.change(screen.getByTestId("setup-name-input"), {
      target: { value: "my-relay" },
    });
    expect(screen.getByTestId("setup-auth-next")).toBeDisabled();
    fireEvent.change(screen.getByTestId("setup-base-url-input"), {
      target: { value: "https://relay.example/v1" },
    });
    expect(screen.getByTestId("setup-auth-next")).toBeEnabled();
  });

  it("switches to the env-var key source with the suggested name prefilled", () => {
    renderFlow();
    fireEvent.click(screen.getByTestId("setup-preset-openai"));
    fireEvent.click(screen.getByTestId("setup-env-toggle"));
    expect(screen.getByTestId("setup-env-input")).toHaveValue(
      "OPENAI_API_KEY",
    );
    expect(screen.queryByTestId("setup-key-input")).toBeNull();
  });

  it("OAuth login skips straight to step 5 with the server-set default", async () => {
    // The OAuth backend provisions provider + aliases + default; the
    // shared models cache reflects that after invalidation.
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
        {
          name: "claude-sonnet-4-6",
          provider: "anthropic",
          model: "claude-sonnet-4-6",
          params: {},
          effective_params_schema: {},
        },
      ],
      providers: [],
    });
    const statuses: SetupFlowStatus[] = [];
    renderFlow((s) => statuses.push(s));

    fireEvent.click(screen.getByTestId("setup-preset-anthropic"));
    expect(flowStep()).toBe("2");
    fireEvent.click(screen.getByTestId("setup-oauth-btn"));
    const modal = await screen.findByTestId("mock-oauth-modal");
    expect(modal).toHaveAttribute("data-provider", "anthropic");
    fireEvent.click(modal);

    await waitFor(() => {
      expect(flowStep()).toBe("5");
    });
    // Server-provisioned aliases become the radio options; the server-set
    // default is preselected.
    await waitFor(() => {
      expect(
        screen.getByTestId("setup-default-radio-claude-opus-4-8"),
      ).toBeChecked();
    });
    expect(
      screen.getByTestId("setup-default-radio-claude-sonnet-4-6"),
    ).toBeInTheDocument();

    // Nothing client-side was probed or persisted.
    expect(probeMock).not.toHaveBeenCalled();
    expect(upsertProviderMock).not.toHaveBeenCalled();
    expect(upsertAliasMock).not.toHaveBeenCalled();
    expect(updateAliasesMock).not.toHaveBeenCalled();

    const final = statuses[statuses.length - 1]!;
    expect(final).toMatchObject({
      providerRegistered: true,
      testPassed: true,
      modelsAdded: true,
      defaultSet: true,
    });
  });
});
