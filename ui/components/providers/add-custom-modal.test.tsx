import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CustomProviderCreateBody } from "@/lib/api";
import { AddCustomProviderModal } from "./add-custom-modal";

const createCustomProviderMock = vi.fn(async (body: CustomProviderCreateBody) => ({
  slug: body.slug,
  kind: body.kind,
  base_url: body.base_url ?? null,
  has_api_key: Boolean(body.api_key),
  params: body.params ?? {},
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    createCustomProvider: (body: CustomProviderCreateBody) =>
      createCustomProviderMock(body),
    listProviderKindDescriptors: vi.fn(async () => [
      {
        kind: "openai_compatible",
        label: "OpenAI-compatible",
        description: "OpenAI wire-compatible endpoint.",
        params_schema: { type: "object", properties: {} },
      },
    ]),
  };
});

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));

function renderModal() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <AddCustomProviderModal open onOpenChange={() => {}} />
    </QueryClientProvider>,
  );
}

describe("AddCustomProviderModal provider presets", () => {
  beforeEach(() => {
    createCustomProviderMock.mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it("pre-fills Fish Audio as an OpenAI-compatible TTS provider", async () => {
    renderModal();

    fireEvent.click(await screen.findByTestId("custom-provider-preset-fish-audio"));

    expect(screen.getByTestId("custom-provider-slug-input")).toHaveValue(
      "fish_audio",
    );
    expect(screen.getByTestId("custom-provider-kind-select")).toHaveValue(
      "openai_compatible",
    );
    expect(screen.getByTestId("custom-provider-base-url-input")).toHaveValue(
      "https://api.fish.audio",
    );

    expect(screen.getByDisplayValue("tts_backend")).toBeInTheDocument();
    expect(screen.getByDisplayValue("fish")).toBeInTheDocument();
    expect(screen.getByDisplayValue("reference_id")).toBeInTheDocument();
    expect(screen.getByDisplayValue("format")).toBeInTheDocument();
    expect(screen.getByDisplayValue("mp3")).toBeInTheDocument();

    const referenceRow = screen
      .getAllByTestId(/custom-provider-params-row-/)
      .find((row) => within(row).queryByDisplayValue("reference_id"));
    expect(referenceRow).toBeTruthy();
    const [, referenceValue] = within(referenceRow!).getAllByRole("textbox");
    fireEvent.change(referenceValue!, { target: { value: "voice-123" } });

    fireEvent.change(screen.getByTestId("custom-provider-api-key-input"), {
      target: { value: "fish-key" },
    });
    fireEvent.click(screen.getByTestId("custom-provider-submit"));

    await waitFor(() => {
      expect(createCustomProviderMock).toHaveBeenCalledWith({
        slug: "fish_audio",
        kind: "openai_compatible",
        base_url: "https://api.fish.audio",
        api_key: { value: "fish-key" },
        params: {
          tts_backend: "fish",
          reference_id: "voice-123",
          format: "mp3",
          custom: true,
        },
      });
    });
  });
});
