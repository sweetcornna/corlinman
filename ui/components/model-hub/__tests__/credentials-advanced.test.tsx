/**
 * CredentialsAdvanced tests (Wave 2.3 coverage, relocated).
 *
 * Moved from `app/(admin)/credentials/page.test.tsx` in the PR4 model-hub
 * consolidation — the per-provider credential cards now live in
 * `../credentials-advanced` (the page is a redirect stub).
 *
 * Coverage:
 *   1. Mount → listCredentials() runs → providers + count summary render.
 *   2. Search filters by provider name.
 *   3. Add flow: click Add on an unset row → enter value → Save → PUT
 *      fires → toast shown.
 *   4. Delete flow: click delete on a set row → confirm dialog opens →
 *      Confirm → DELETE fires → toast shown.
 *   5. Show-empty switch hides unconfigured providers.
 *
 * The `@/lib/api` module is mocked so we don't go through the real
 * apiFetch wrapper and so we can drive mutation lifecycle deterministically.
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

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listCredentials: vi.fn(),
    setCredential: vi.fn(),
    deleteCredential: vi.fn(),
    setProviderEnabled: vi.fn(),
  };
});

const { toastSuccess, toastError, toastMessage } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
  toastMessage: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: {
    success: toastSuccess,
    error: toastError,
    message: toastMessage,
  },
}));

import {
  listCredentials,
  setCredential,
  deleteCredential,
  setProviderEnabled,
  type CredentialsListResponse,
} from "@/lib/api";
import { CredentialsAdvanced } from "../credentials-advanced";

const mockedList = vi.mocked(listCredentials);
const mockedSet = vi.mocked(setCredential);
const mockedDelete = vi.mocked(deleteCredential);
const mockedEnable = vi.mocked(setProviderEnabled);

const LIST_PAYLOAD: CredentialsListResponse = {
  providers: [
    {
      name: "openai",
      kind: "openai",
      enabled: true,
      fields: [
        {
          key: "api_key",
          set: true,
          preview: "…xyz9",
          env_ref: "OPENAI_API_KEY",
        },
        {
          key: "base_url",
          set: false,
          preview: null,
          env_ref: "OPENAI_BASE_URL",
        },
        {
          key: "org_id",
          set: false,
          preview: null,
          env_ref: "OPENAI_ORG_ID",
        },
      ],
    },
    {
      name: "anthropic",
      kind: "anthropic",
      enabled: false,
      fields: [
        {
          key: "api_key",
          set: false,
          preview: null,
          env_ref: "ANTHROPIC_API_KEY",
        },
        {
          key: "base_url",
          set: false,
          preview: null,
          env_ref: "ANTHROPIC_BASE_URL",
        },
      ],
    },
  ],
};

function renderSection() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <CredentialsAdvanced />
    </QueryClientProvider>,
  );
}

describe("CredentialsAdvanced", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    toastSuccess.mockReset();
    toastError.mockReset();
    toastMessage.mockReset();
    mockedList.mockResolvedValue(LIST_PAYLOAD);
    mockedSet.mockResolvedValue({ status: "ok" });
    mockedDelete.mockResolvedValue(undefined);
    mockedEnable.mockResolvedValue({ status: "ok" });
  });

  afterEach(() => {
    cleanup();
  });

  it("lists providers + summarises configured count", async () => {
    renderSection();

    expect(
      await screen.findByTestId("credentials-provider-openai"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("credentials-provider-anthropic"),
    ).toBeInTheDocument();

    // Summary reads "2 个 provider（已配置 1 个）" — openai is configured,
    // anthropic is not.
    const summary = screen.getByTestId("credentials-count-summary");
    expect(summary).toHaveTextContent("2");
    expect(summary).toHaveTextContent("1");

    // The keys-here-don't-register-a-provider warning renders up top.
    expect(
      screen.getByTestId("credentials-advanced-warning"),
    ).toBeInTheDocument();
  });

  it("search filters by provider name", async () => {
    renderSection();

    await screen.findByTestId("credentials-provider-openai");

    const searchBox = screen.getByTestId("credentials-search");
    fireEvent.change(searchBox, { target: { value: "anthropic" } });

    await waitFor(() => {
      expect(
        screen.queryByTestId("credentials-provider-openai"),
      ).not.toBeInTheDocument();
    });
    expect(
      screen.getByTestId("credentials-provider-anthropic"),
    ).toBeInTheDocument();
  });

  it("Add → Save calls setCredential and emits a success toast", async () => {
    renderSection();

    await screen.findByTestId("credentials-provider-anthropic");

    // Anthropic is unconfigured + disabled, so its card renders collapsed
    // by default (hermes-EnvPage scannability UX). Expand it before the
    // per-field Add control is reachable.
    fireEvent.click(
      screen.getByTestId("credentials-provider-anthropic-toggle-expand"),
    );

    fireEvent.click(screen.getByTestId("cred-anthropic-api_key-add"));
    const input = screen.getByTestId(
      "cred-anthropic-api_key-input",
    ) as HTMLInputElement;
    fireEvent.paste(input, {
      clipboardData: { getData: () => "sk-ant-newvalue" },
    });
    fireEvent.click(screen.getByTestId("cred-anthropic-api_key-save"));

    await waitFor(() => {
      expect(mockedSet).toHaveBeenCalledWith(
        "anthropic",
        "api_key",
        "sk-ant-newvalue",
      );
    });
    await waitFor(() => {
      expect(toastSuccess).toHaveBeenCalled();
    });
  });

  it("delete flow: click trash → confirm dialog → confirm fires DELETE", async () => {
    renderSection();

    await screen.findByTestId("credentials-provider-openai");

    fireEvent.click(screen.getByTestId("cred-openai-api_key-delete"));
    // Confirmation dialog opens.
    expect(
      await screen.findByTestId("credentials-delete-dialog"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("credentials-delete-confirm"));

    await waitFor(() => {
      expect(mockedDelete).toHaveBeenCalledWith("openai", "api_key");
    });
    await waitFor(() => {
      expect(toastSuccess).toHaveBeenCalled();
    });
  });

  it("toggling the show-empty switch hides unconfigured providers", async () => {
    renderSection();

    await screen.findByTestId("credentials-provider-openai");
    // Anthropic is unconfigured — visible by default.
    expect(
      screen.getByTestId("credentials-provider-anthropic"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("credentials-show-empty"));

    await waitFor(() => {
      expect(
        screen.queryByTestId("credentials-provider-anthropic"),
      ).not.toBeInTheDocument();
    });
    // Openai (configured) stays visible.
    expect(
      screen.getByTestId("credentials-provider-openai"),
    ).toBeInTheDocument();
  });
});
