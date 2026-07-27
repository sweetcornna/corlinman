/**
 * CapabilitiesSection — the web-search binding.
 *
 * The subtle part is the API key. `GET /admin/models/capabilities` never
 * echoes it (only `api_key_set`), so the field starts blank even when a key
 * is on file. If the save sent `api_key: ""` unconditionally, every
 * unrelated edit — switching backend, say — would silently delete a working
 * key. These tests pin the three-state contract:
 *
 *   untouched  → omit `api_key` entirely (server keeps the stored one)
 *   typed      → send the new value
 *   cleared    → send "" (server deletes it)
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

vi.mock("@/lib/api/model-capabilities", () => ({
  getModelCapabilities: vi.fn(),
  putImageCapability: vi.fn(),
  putSearchCapability: vi.fn(),
}));

vi.mock("@/lib/api/voice", () => ({
  listVoiceBackends: vi.fn().mockResolvedValue({ backends: [] }),
}));

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: { success: toastSuccess, error: toastError },
}));

import {
  getModelCapabilities,
  putSearchCapability,
  type ModelCapabilities,
} from "@/lib/api/model-capabilities";
import { CapabilitiesSection } from "../capabilities-section";

const mockedGet = vi.mocked(getModelCapabilities);
const mockedPut = vi.mocked(putSearchCapability);

function payload(overrides: Partial<ModelCapabilities["search"]> = {}): ModelCapabilities {
  return {
    text: { model: "gpt-5.2" },
    image: { provider: "", model: "", capable_providers: [] },
    voice: { enabled: true, backend: "", model: "", voice: "" },
    search: {
      backend: "",
      api_key_set: false,
      backends: ["ddg", "serpapi"],
      ...overrides,
    },
    aliases: [],
  };
}

function renderSection() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <CapabilitiesSection />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedGet.mockResolvedValue(payload());
  mockedPut.mockResolvedValue({ status: "ok", backend: "", api_key_set: false });
});

afterEach(cleanup);

describe("CapabilitiesSection — web search", () => {
  it("renders the card with the keyless default selected", async () => {
    renderSection();
    const select = (await screen.findByTestId(
      "capability-search-backend",
    )) as HTMLSelectElement;
    expect(select.value).toBe("");
  });

  it("keeps Save disabled until something changes", async () => {
    renderSection();
    const save = await screen.findByTestId("capability-search-save");
    expect(save).toBeDisabled();

    fireEvent.change(await screen.findByTestId("capability-search-backend"), {
      target: { value: "serpapi" },
    });
    await waitFor(() => expect(save).not.toBeDisabled());
  });

  it("omits api_key when the field was never touched", async () => {
    mockedGet.mockResolvedValue(payload({ backend: "serpapi", api_key_set: true }));
    renderSection();

    // Change only the backend; leave the key field alone.
    fireEvent.change(await screen.findByTestId("capability-search-backend"), {
      target: { value: "ddg" },
    });
    fireEvent.click(await screen.findByTestId("capability-search-save"));

    await waitFor(() => expect(mockedPut).toHaveBeenCalledTimes(1));
    expect(mockedPut).toHaveBeenCalledWith({ backend: "ddg" });
    expect(mockedPut.mock.calls[0][0]).not.toHaveProperty("api_key");
  });

  it("sends the key once typed", async () => {
    renderSection();
    fireEvent.change(await screen.findByTestId("capability-search-backend"), {
      target: { value: "serpapi" },
    });
    fireEvent.change(await screen.findByTestId("capability-search-key"), {
      target: { value: " k-1 " },
    });
    fireEvent.click(await screen.findByTestId("capability-search-save"));

    await waitFor(() => expect(mockedPut).toHaveBeenCalledTimes(1));
    expect(mockedPut).toHaveBeenCalledWith({ backend: "serpapi", api_key: "k-1" });
  });

  it("sends an empty key when the operator clears a stored one", async () => {
    mockedGet.mockResolvedValue(payload({ backend: "serpapi", api_key_set: true }));
    renderSection();

    const key = await screen.findByTestId("capability-search-key");
    // Typing then erasing leaves "" — an explicit delete, not "untouched".
    fireEvent.change(key, { target: { value: "x" } });
    fireEvent.change(key, { target: { value: "" } });
    fireEvent.click(await screen.findByTestId("capability-search-save"));

    await waitFor(() => expect(mockedPut).toHaveBeenCalledTimes(1));
    expect(mockedPut).toHaveBeenCalledWith({ backend: "serpapi", api_key: "" });
  });

  it("tells the operator a key is already on file", async () => {
    // Locale-agnostic: assert the two states say different things rather
    // than pinning either translation.
    mockedGet.mockResolvedValue(payload({ api_key_set: false }));
    renderSection();
    const unset = (
      (await screen.findByTestId("capability-search-key")) as HTMLInputElement
    ).placeholder;
    cleanup();

    mockedGet.mockResolvedValue(payload({ backend: "serpapi", api_key_set: true }));
    renderSection();
    const key = (await screen.findByTestId("capability-search-key")) as HTMLInputElement;

    expect(key.placeholder).not.toBe(unset);
    expect(key.placeholder).not.toBe("");
    // Never rendered as plain text.
    expect(key.type).toBe("password");
  });

  it("surfaces a rejected binding instead of pretending it saved", async () => {
    mockedPut.mockRejectedValue(new Error("api_key_required"));
    renderSection();
    fireEvent.change(await screen.findByTestId("capability-search-backend"), {
      target: { value: "serpapi" },
    });
    fireEvent.click(await screen.findByTestId("capability-search-save"));

    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastSuccess).not.toHaveBeenCalled();
  });
});
