/**
 * PersonaPage tests.
 *
 * Covers:
 *   - Empty state when the personas list comes back empty
 *   - Row rendering, including the `built-in` badge on builtins
 *   - Delete button is disabled for builtins (no API call fires)
 *   - Editor opens (create + edit) and triggers the right API call
 *   - QQ humanlike toggle: status line + Save calls setQqHumanlike
 *   - Status line reflects "enabled → persona" / "disabled" / "no persona"
 *
 * Mirrors the discipline used by `app/(admin)/sessions/page.test.tsx`:
 * mock the API client at module scope, install before importing the
 * page, render under a fresh `QueryClient`.
 */

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
  DeletePersonaResult,
  NewPersona,
  PartialPersona,
  Persona,
  QqHumanlikeState,
} from "@/lib/api/personas";
import type { ProviderView } from "@/lib/api";

// ---------------------------------------------------------------------------
// Sonner toaster — stub so we can assert success/error paths fired without
// pulling the real Toaster into jsdom.
// ---------------------------------------------------------------------------
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

// ---------------------------------------------------------------------------
// API mocks — installed before the page import.
// ---------------------------------------------------------------------------

const fetchPersonasMock = vi.fn(async (): Promise<Persona[]> => {
  throw new Error("fetchPersonasMock not configured");
});
const fetchQqHumanlikeMock = vi.fn(async (): Promise<QqHumanlikeState> => {
  throw new Error("fetchQqHumanlikeMock not configured");
});
const createPersonaMock = vi.fn(
  async (_p: NewPersona): Promise<Persona> => {
    throw new Error("createPersonaMock not configured");
  },
);
const updatePersonaMock = vi.fn(
  async (_id: string, _patch: PartialPersona): Promise<Persona> => {
    throw new Error("updatePersonaMock not configured");
  },
);
const deletePersonaMock = vi.fn(
  async (_id: string): Promise<DeletePersonaResult> => {
    throw new Error("deletePersonaMock not configured");
  },
);
const setQqHumanlikeMock = vi.fn(
  async (_payload: QqHumanlikeState): Promise<QqHumanlikeState> => {
    throw new Error("setQqHumanlikeMock not configured");
  },
);
const fetchProvidersMock = vi.fn(async (): Promise<ProviderView[]> => {
  throw new Error("fetchProvidersMock not configured");
});
const getProviderModelsMock = vi.fn(
  async (_provider: string): Promise<{
    models: { id: string; display_name?: string }[];
    error?: string;
  }> => {
    throw new Error("getProviderModelsMock not configured");
  },
);

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return {
    ...actual,
    fetchProviders: () => fetchProvidersMock(),
    getProviderModels: (provider: string) => getProviderModelsMock(provider),
  };
});

vi.mock("@/lib/api/personas", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/personas")>(
    "@/lib/api/personas",
  );
  return {
    ...actual,
    fetchPersonas: () => fetchPersonasMock(),
    fetchQqHumanlike: () => fetchQqHumanlikeMock(),
    // The card now calls the parameterized helpers; the persona page tests
    // exercise the default channel (qq), so route them to the same mocks so
    // the existing per-test setup keeps driving the card unchanged.
    fetchHumanlike: (_channel: unknown) => fetchQqHumanlikeMock(),
    setHumanlike: (_channel: unknown, payload: QqHumanlikeState) =>
      setQqHumanlikeMock(payload),
    createPersona: (p: NewPersona) => createPersonaMock(p),
    updatePersona: (id: string, patch: PartialPersona) =>
      updatePersonaMock(id, patch),
    deletePersona: (id: string) => deletePersonaMock(id),
    setQqHumanlike: (payload: QqHumanlikeState) =>
      setQqHumanlikeMock(payload),
  };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/persona",
  useSearchParams: () => new URLSearchParams(),
}));

import PersonaPage from "./page";

// ---------------------------------------------------------------------------

const SAMPLE_BUILTIN: Persona = {
  id: "grantley",
  display_name: "Grantley Bell",
  short_summary: "Second son of the Bell family. Dry, self-deprecating.",
  system_prompt: "# Grantley\nYou are Grantley Bell.",
  is_builtin: true,
  created_at_ms: 1_777_000_000_000,
  updated_at_ms: 1_777_593_600_000,
  avatar_url: null,
  model_bindings: {
    text: { provider: null, model: null },
    image: { provider: null, model: null },
    voice: { provider: null, model: null },
  },
};
const SAMPLE_CUSTOM: Persona = {
  id: "alyssa",
  display_name: "Alyssa P. Hacker",
  short_summary: "MIT Scheme hacker.",
  system_prompt: "# Alyssa\nYou are Alyssa.",
  is_builtin: false,
  created_at_ms: 1_777_400_000_000,
  updated_at_ms: 1_777_500_000_000,
  avatar_url: null,
  model_bindings: {
    text: { provider: "relay", model: "gpt-4o-mini" },
    image: { provider: null, model: null },
    voice: { provider: null, model: null },
  },
};

beforeEach(() => {
  initI18n();
  void i18next.changeLanguage("en");
  fetchPersonasMock.mockReset();
  fetchQqHumanlikeMock.mockReset();
  createPersonaMock.mockReset();
  updatePersonaMock.mockReset();
  deletePersonaMock.mockReset();
  setQqHumanlikeMock.mockReset();
  fetchProvidersMock.mockReset();
  getProviderModelsMock.mockReset();
  fetchProvidersMock.mockResolvedValue([
    {
      name: "relay",
      kind: "openai_compatible",
      enabled: true,
      base_url: null,
      api_key_source: "env",
      api_key_env_name: "OPENAI_API_KEY",
      params: {},
      params_schema: { type: "object", additionalProperties: true },
      capabilities: { chat: true, embedding: true },
    },
    {
      name: "voice",
      kind: "openai_compatible",
      enabled: true,
      base_url: null,
      api_key_source: "env",
      api_key_env_name: "VOICE_API_KEY",
      params: {},
      params_schema: { type: "object", additionalProperties: true },
      capabilities: { chat: true, embedding: false },
    },
  ] as ProviderView[]);
  getProviderModelsMock.mockImplementation(async (provider: string) => ({
    models:
      provider === "voice"
        ? [{ id: "tts-large", display_name: "tts-large" }]
        : [
            { id: "gpt-4o-mini", display_name: "gpt-4o-mini" },
            { id: "gpt-5.5", display_name: "gpt-5.5" },
          ],
  }));
});

afterEach(() => {
  cleanup();
});

function Harness({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false },
          mutations: { retry: false },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18next}>{children}</I18nextProvider>
    </QueryClientProvider>
  );
}

// Convenience — most tests don't care about the humanlike query; stub a
// minimal "disabled, no persona" reply so the card paints without crashing.
function stubHumanlikeOff() {
  fetchQqHumanlikeMock.mockResolvedValue({
    enabled: false,
    persona_id: null,
  });
}

describe("PersonaPage — personas list", () => {
  it("renders the empty state when no personas are returned", async () => {
    fetchPersonasMock.mockResolvedValue([]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("personas-empty")).toBeInTheDocument();
    });
  });

  it("renders a row per persona and the built-in badge for builtins", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN, SAMPLE_CUSTOM]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("persona-row-grantley")).toBeInTheDocument();
      expect(screen.getByTestId("persona-row-alyssa")).toBeInTheDocument();
    });
    // Builtin badge only on the builtin row.
    expect(screen.getByTestId("persona-builtin-grantley")).toBeInTheDocument();
    expect(
      screen.queryByTestId("persona-builtin-alyssa"),
    ).not.toBeInTheDocument();
    // Summary text bubbles through.
    expect(screen.getByTestId("persona-row-alyssa").textContent).toMatch(
      /MIT Scheme/,
    );
  });

  it("disables the delete button on builtin rows", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN, SAMPLE_CUSTOM]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    const builtinDelete = await screen.findByTestId(
      "persona-delete-grantley",
    );
    expect(builtinDelete).toBeDisabled();
    const customDelete = screen.getByTestId("persona-delete-alyssa");
    expect(customDelete).not.toBeDisabled();
    // Clicking the disabled builtin button must NOT fire the API.
    fireEvent.click(builtinDelete);
    expect(deletePersonaMock).not.toHaveBeenCalled();
  });

  it("delete confirm fires deletePersona and removes the row", async () => {
    fetchPersonasMock.mockResolvedValueOnce([SAMPLE_BUILTIN, SAMPLE_CUSTOM]);
    // After invalidate, return the pruned list.
    fetchPersonasMock.mockResolvedValueOnce([SAMPLE_BUILTIN]);
    deletePersonaMock.mockResolvedValueOnce(undefined);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    fireEvent.click(await screen.findByTestId("persona-delete-alyssa"));
    fireEvent.click(
      await screen.findByTestId("persona-delete-confirm-confirm"),
    );

    await waitFor(() => {
      expect(deletePersonaMock).toHaveBeenCalledWith("alyssa");
    });
    await waitFor(() => {
      expect(
        screen.queryByTestId("persona-row-alyssa"),
      ).not.toBeInTheDocument();
    });
  });

  it("renders a load-failed cell when the personas query rejects", async () => {
    fetchPersonasMock.mockRejectedValue(new Error("network down"));
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("personas-load-failed")).toBeInTheDocument();
    });
  });
});

describe("PersonaPage — editor modal", () => {
  it("opens the create editor with empty fields when clicking '+ New persona'", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    // Wait for the list to settle.
    await screen.findByTestId("persona-row-grantley");
    fireEvent.click(screen.getByTestId("persona-new"));

    const editor = await screen.findByTestId("persona-editor");
    expect(editor).toBeInTheDocument();
    const idInput = screen.getByTestId(
      "persona-id-input",
    ) as HTMLInputElement;
    expect(idInput.value).toBe("");
    // The slug input is editable in create mode.
    expect(idInput).not.toBeDisabled();
  });

  it("keeps the create editor body scrollable while header and footer stay fixed", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await screen.findByTestId("persona-row-grantley");
    fireEvent.click(screen.getByTestId("persona-new"));

    const editor = await screen.findByTestId("persona-editor");
    const scrollBody = screen.getByTestId("persona-editor-scroll");
    const footer = screen.getByTestId("persona-editor-footer");

    expect(editor).toHaveClass(
      "flex",
      "max-h-[85dvh]",
      "flex-col",
      "overflow-hidden",
    );
    expect(scrollBody).toHaveClass(
      "min-h-0",
      "flex-1",
      "overflow-y-auto",
      "overscroll-contain",
    );
    expect(footer).toHaveClass("shrink-0");
  });

  it("opens the edit editor pre-populated, locks the slug, and PATCHes on save", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN, SAMPLE_CUSTOM]);
    updatePersonaMock.mockResolvedValue({
      ...SAMPLE_CUSTOM,
      display_name: "Alyssa P. Hacker (v2)",
    });
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    fireEvent.click(await screen.findByTestId("persona-edit-alyssa"));

    const idInput = (await screen.findByTestId(
      "persona-id-input",
    )) as HTMLInputElement;
    expect(idInput.value).toBe("alyssa");
    expect(idInput).toBeDisabled();
    const nameInput = screen.getByTestId(
      "persona-display-name-input",
    ) as HTMLInputElement;
    expect(nameInput.value).toBe("Alyssa P. Hacker");

    // Change the display name and save.
    fireEvent.change(nameInput, { target: { value: "Alyssa P. Hacker (v2)" } });
    fireEvent.click(screen.getByTestId("persona-editor-save"));

    await waitFor(() => {
      expect(updatePersonaMock).toHaveBeenCalledWith("alyssa", {
        display_name: "Alyssa P. Hacker (v2)",
      });
    });
  });

  it("edits persona text/image/voice model bindings and PATCHes them", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_CUSTOM]);
    updatePersonaMock.mockResolvedValue({
      ...SAMPLE_CUSTOM,
      model_bindings: {
        ...SAMPLE_CUSTOM.model_bindings,
        text: { provider: "relay", model: "gpt-5.5" },
      },
    });
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    fireEvent.click(await screen.findByTestId("persona-edit-alyssa"));
    expect(await screen.findByTestId("persona-model-binding-text")).toHaveTextContent(
      "gpt-4o-mini",
    );
    expect(screen.getByTestId("persona-model-binding-image")).toHaveTextContent(
      "Inherit",
    );
    expect(screen.getByTestId("persona-model-binding-voice")).toHaveTextContent(
      "Inherit",
    );

    fireEvent.click(screen.getByTestId("persona-model-pick-text"));
    expect(await screen.findByTestId("model-picker-dialog")).toBeInTheDocument();
    fireEvent.click(await screen.findByTestId("model-picker-model-gpt-5.5"));

    await waitFor(() => {
      expect(screen.queryByTestId("model-picker-dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("persona-model-binding-text")).toHaveTextContent(
      "gpt-5.5",
    );

    fireEvent.click(screen.getByTestId("persona-editor-save"));

    await waitFor(() => {
      expect(updatePersonaMock).toHaveBeenCalledWith("alyssa", {
        model_bindings: {
          text: { provider: "relay", model: "gpt-5.5" },
          image: { provider: null, model: null },
          voice: { provider: null, model: null },
        },
      });
    });
  });

  it("create flow validates required fields before POSTing", async () => {
    fetchPersonasMock.mockResolvedValue([]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await screen.findByTestId("personas-empty");
    fireEvent.click(screen.getByTestId("persona-new"));
    fireEvent.click(await screen.findByTestId("persona-editor-save"));

    // Slug error appears, no API call fires.
    expect(await screen.findByTestId("persona-id-error")).toBeInTheDocument();
    expect(createPersonaMock).not.toHaveBeenCalled();
  });

  it("create flow POSTs the full body on save", async () => {
    fetchPersonasMock.mockResolvedValueOnce([]);
    fetchPersonasMock.mockResolvedValueOnce([SAMPLE_CUSTOM]);
    createPersonaMock.mockResolvedValue(SAMPLE_CUSTOM);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await screen.findByTestId("personas-empty");
    fireEvent.click(screen.getByTestId("persona-new"));

    fireEvent.change(await screen.findByTestId("persona-id-input"), {
      target: { value: "alyssa" },
    });
    fireEvent.change(screen.getByTestId("persona-display-name-input"), {
      target: { value: "Alyssa P. Hacker" },
    });
    fireEvent.change(screen.getByTestId("persona-short-summary-input"), {
      target: { value: "MIT Scheme hacker." },
    });
    fireEvent.change(screen.getByTestId("persona-system-prompt-textarea"), {
      target: { value: "# Alyssa\nYou are Alyssa." },
    });
    fireEvent.click(screen.getByTestId("persona-editor-save"));

    await waitFor(() => {
      expect(createPersonaMock).toHaveBeenCalledWith({
        id: "alyssa",
        display_name: "Alyssa P. Hacker",
        short_summary: "MIT Scheme hacker.",
        system_prompt: "# Alyssa\nYou are Alyssa.",
        model_bindings: {
          text: { provider: null, model: null },
          image: { provider: null, model: null },
          voice: { provider: null, model: null },
        },
      });
    });
  });

  it("enables reset-to-default for builtins; no dead test box", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    fireEvent.click(await screen.findByTestId("persona-edit-grantley"));
    // Reset-to-default only shows when editing a builtin, and is now live.
    const resetBtn = await screen.findByTestId("persona-reset-default");
    expect(resetBtn).not.toBeDisabled();
    // The permanently-disabled preview "test box" was removed — the
    // backend has no preview endpoint.
    expect(screen.queryByTestId("persona-test-box")).not.toBeInTheDocument();
    expect(screen.queryByTestId("persona-test-input")).not.toBeInTheDocument();
    expect(screen.queryByTestId("persona-test-button")).not.toBeInTheDocument();
  });

  it("auto-derives the slug from the display name in create mode", async () => {
    fetchPersonasMock.mockResolvedValue([]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await screen.findByTestId("personas-empty");
    fireEvent.click(screen.getByTestId("persona-new"));

    const idInput = (await screen.findByTestId(
      "persona-id-input",
    )) as HTMLInputElement;
    fireEvent.change(screen.getByTestId("persona-display-name-input"), {
      target: { value: "Alyssa P. Hacker" },
    });
    expect(idInput.value).toBe("alyssa-p-hacker");

    // Clearing the display name clears the derived slug again.
    fireEvent.change(screen.getByTestId("persona-display-name-input"), {
      target: { value: "" },
    });
    expect(idInput.value).toBe("");
  });

  it("falls back to a stable persona-<rand> slug for CJK display names", async () => {
    fetchPersonasMock.mockResolvedValue([]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await screen.findByTestId("personas-empty");
    fireEvent.click(screen.getByTestId("persona-new"));

    const idInput = (await screen.findByTestId(
      "persona-id-input",
    )) as HTMLInputElement;
    fireEvent.change(screen.getByTestId("persona-display-name-input"), {
      target: { value: "格兰" },
    });
    expect(idInput.value).toMatch(/^persona-[a-z0-9]{4}$/);
    const first = idInput.value;

    // Deterministic across keystrokes — the suffix must not re-roll.
    fireEvent.change(screen.getByTestId("persona-display-name-input"), {
      target: { value: "格兰特利" },
    });
    expect(idInput.value).toBe(first);
  });

  it("stops auto-deriving once the slug is manually edited", async () => {
    fetchPersonasMock.mockResolvedValue([]);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await screen.findByTestId("personas-empty");
    fireEvent.click(screen.getByTestId("persona-new"));

    const idInput = (await screen.findByTestId(
      "persona-id-input",
    )) as HTMLInputElement;
    fireEvent.change(screen.getByTestId("persona-display-name-input"), {
      target: { value: "Alyssa" },
    });
    expect(idInput.value).toBe("alyssa");

    fireEvent.change(idInput, { target: { value: "custom-slug" } });
    fireEvent.change(screen.getByTestId("persona-display-name-input"), {
      target: { value: "Ben Bitdiddle" },
    });
    expect(idInput.value).toBe("custom-slug");
  });

  it("create flow allows an empty short summary", async () => {
    fetchPersonasMock.mockResolvedValueOnce([]);
    fetchPersonasMock.mockResolvedValueOnce([SAMPLE_CUSTOM]);
    createPersonaMock.mockResolvedValue(SAMPLE_CUSTOM);
    stubHumanlikeOff();

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    await screen.findByTestId("personas-empty");
    fireEvent.click(screen.getByTestId("persona-new"));

    fireEvent.change(await screen.findByTestId("persona-display-name-input"), {
      target: { value: "Alyssa P. Hacker" },
    });
    fireEvent.change(screen.getByTestId("persona-system-prompt-textarea"), {
      target: { value: "# Alyssa\nYou are Alyssa." },
    });
    fireEvent.click(screen.getByTestId("persona-editor-save"));

    await waitFor(() => {
      expect(createPersonaMock).toHaveBeenCalledWith({
        id: "alyssa-p-hacker",
        display_name: "Alyssa P. Hacker",
        short_summary: "",
        system_prompt: "# Alyssa\nYou are Alyssa.",
        model_bindings: {
          text: { provider: null, model: null },
          image: { provider: null, model: null },
          voice: { provider: null, model: null },
        },
      });
    });
  });
});

describe("PersonaPage — QQ humanlike toggle", () => {
  it("renders the disabled status line on a fresh install", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN]);
    fetchQqHumanlikeMock.mockResolvedValue({
      enabled: false,
      persona_id: null,
    });

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    const status = await screen.findByTestId("qq-humanlike-status");
    expect(status.textContent ?? "").toMatch(/disabled/i);
  });

  it("renders the enabled status line with the bound persona display name", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN]);
    fetchQqHumanlikeMock.mockResolvedValue({
      enabled: true,
      persona_id: "grantley",
    });

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    const status = await screen.findByTestId("qq-humanlike-status");
    // We surface the display_name, not the raw slug.
    await waitFor(() => {
      expect(status.textContent ?? "").toMatch(/Grantley Bell/);
    });
  });

  it("toggling on and picking a persona writes both via setQqHumanlike", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN]);
    fetchQqHumanlikeMock.mockResolvedValue({
      enabled: false,
      persona_id: null,
    });
    setQqHumanlikeMock.mockImplementation(async (payload) => payload);

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    // Wait for the humanlike state to actually settle (otherwise the
    // Switch is still disabled=isLoading and clicks no-op).
    await waitFor(() => {
      expect(screen.getByTestId("qq-humanlike-toggle")).not.toBeDisabled();
    });
    const toggle = screen.getByTestId("qq-humanlike-toggle");
    // Initially: disabled state → save button is disabled (nothing to save).
    expect(screen.getByTestId("qq-humanlike-save")).toBeDisabled();

    fireEvent.click(toggle);

    // Persona select now visible.
    const select = (await screen.findByTestId(
      "qq-humanlike-persona-select",
    )) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "grantley" } });

    // Save fires the API.
    fireEvent.click(screen.getByTestId("qq-humanlike-save"));
    await waitFor(() => {
      expect(setQqHumanlikeMock).toHaveBeenCalledWith({
        enabled: true,
        persona_id: "grantley",
      });
    });
  });

  it("Save button stays disabled while the toggle is on but no persona is picked", async () => {
    fetchPersonasMock.mockResolvedValue([SAMPLE_BUILTIN]);
    fetchQqHumanlikeMock.mockResolvedValue({
      enabled: false,
      persona_id: null,
    });

    render(
      <Harness>
        <PersonaPage />
      </Harness>,
    );

    // Wait for the humanlike query to settle before driving the toggle.
    await waitFor(() => {
      expect(
        screen.getByTestId("qq-humanlike-toggle"),
      ).not.toBeDisabled();
    });
    fireEvent.click(screen.getByTestId("qq-humanlike-toggle"));
    // Toggle on, but no persona selected → save still disabled.
    expect(screen.getByTestId("qq-humanlike-save")).toBeDisabled();
  });
});
