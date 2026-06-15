/**
 * ChatPage model-picker wiring tests (C2-ui-model-picker).
 *
 * Regression guard for the dead composer model-picker pill: ChatPage used to
 * render <ChatArea/> WITHOUT onOpenModelPicker, so the composer model pill
 * (data-testid="composer-model") and the `/model` slash command were no-ops —
 * `setPickerOpen` was only ever called with `null`, so <ChatModelPicker/>
 * (which returns null unless `open`) could never appear.
 *
 * Strategy: render ChatPage with an active `?session=` (so <ChatArea/> +
 * <Composer/> mount), mock the chat/models/providers API at module scope, then
 * click the composer model pill and assert the picker dialog opens.
 *
 * Mirrors the discipline used by the existing admin page tests under
 * `app/(admin)/.../page.test.tsx`.
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
import type { ModelsResponse, ProviderView } from "@/lib/api";
import type { ChatConversation } from "@/lib/chat/types";
import type { ReplayResult } from "@/lib/api/sessions";

const SESSION_KEY = "corlinman:test-session";

// ---------------------------------------------------------------------------
// Toast surface — jsdom doesn't host the sonner toaster.
// ---------------------------------------------------------------------------
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));

// ---------------------------------------------------------------------------
// API mocks — install before importing the page.
// ---------------------------------------------------------------------------

const listSessionsMock = vi.fn(
  async (): Promise<ChatConversation[]> => [
    {
      sessionKey: SESSION_KEY,
      title: "Test conversation",
      pinned: false,
      archived: false,
      messageCount: 0,
      lastMessageAt: Date.now(),
    } as ChatConversation,
  ],
);

const fetchModelsMock = vi.fn(
  async (): Promise<ModelsResponse> => ({
    default: "gpt-4o",
    aliases: { fast: "gpt-4o-mini" },
    providers: [],
  }),
);

const fetchProvidersMock = vi.fn(async (): Promise<ProviderView[]> => []);

vi.mock("@/lib/api/chat", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/chat")>("@/lib/api/chat");
  return {
    ...actual,
    listChatSessions: () => listSessionsMock(),
    patchChatSession: vi.fn(),
    deleteChatSession: vi.fn(),
  };
});

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchModels: () => fetchModelsMock(),
    fetchModelsV2: vi.fn(async () => ({
      default: "",
      aliases: [],
      providers: [],
    })),
    fetchProviders: () => fetchProvidersMock(),
    listAgents: vi.fn(async () => []),
    listAgentBindings: vi.fn(async () => ({ agents: [] })),
    getProviderModels: vi.fn(async () => ({ models: [] })),
  };
});

vi.mock("@/lib/api/sessions", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/sessions")>(
      "@/lib/api/sessions",
    );
  return {
    ...actual,
    replaySession: vi.fn(
      async (): Promise<ReplayResult> =>
        ({
          kind: "ok",
          replay: {
            session_key: SESSION_KEY,
            mode: "transcript",
            transcript: [],
            summary: { message_count: 0, tenant_id: "default" },
          },
        }) as ReplayResult,
    ),
  };
});

// next/navigation — ChatPage reads `?session=` and uses the router.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/chat",
  useSearchParams: () => new URLSearchParams(`session=${SESSION_KEY}`),
}));

import ChatPage from "./page";

// ---------------------------------------------------------------------------

beforeEach(() => {
  initI18n();
  i18next.changeLanguage("en");
  listSessionsMock.mockClear();
  fetchModelsMock.mockClear();
  fetchProvidersMock.mockClear();
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
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

describe("ChatPage model picker wiring", () => {
  it("opens the model picker when the composer model pill is clicked", async () => {
    render(
      <Harness>
        <ChatPage />
      </Harness>,
    );

    // The composer (and its model pill) only mount once the active session
    // resolves <ChatArea/>.
    const pill = await screen.findByTestId("composer-model");

    // Before the click the picker must be closed — ChatModelPicker returns
    // null unless `open`, so the dialog is absent from the DOM.
    expect(screen.queryByTestId("chat-model-picker")).not.toBeInTheDocument();

    fireEvent.click(pill);

    // After the click ChatPage must have set pickerOpen to a non-null kind,
    // so the LLM picker dialog appears.
    await waitFor(() => {
      const dialog = screen.getByTestId("chat-model-picker");
      expect(dialog).toBeInTheDocument();
      expect(dialog).toHaveAttribute("data-kind", "llm");
    });
  });

  it("opens the model picker via the /model slash command", async () => {
    render(
      <Harness>
        <ChatPage />
      </Harness>,
    );

    const textarea = (await screen.findByTestId(
      "composer-textarea",
    )) as HTMLTextAreaElement;

    expect(screen.queryByTestId("chat-model-picker")).not.toBeInTheDocument();

    // Type the /model slash command and run it via Enter, exercising the same
    // onOpenModelPicker handler the slash menu invokes.
    fireEvent.change(textarea, { target: { value: "/model" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => {
      expect(screen.getByTestId("chat-model-picker")).toBeInTheDocument();
    });
  });

  it("enables max reasoning for a Codex-provisioned GPT alias", async () => {
    fetchModelsMock.mockResolvedValueOnce({
      default: "gpt-5.5",
      aliases: [
        {
          name: "gpt-5.5",
          provider: "codex",
          model: "gpt-5.5",
          params: {},
          effective_params_schema: {},
        },
      ],
      providers: [],
    } as unknown as ModelsResponse);
    try {
      localStorage.setItem("corlinman:chat:reasoning-effort", "xhigh");
    } catch {
      /* ignore */
    }

    render(
      <Harness>
        <ChatPage />
      </Harness>,
    );

    expect(await screen.findByTestId("composer-reasoning-xhigh")).toBeInTheDocument();
  });

  it("closes the picker again via the close control", async () => {
    render(
      <Harness>
        <ChatPage />
      </Harness>,
    );

    const pill = await screen.findByTestId("composer-model");
    fireEvent.click(pill);
    await screen.findByTestId("chat-model-picker");

    fireEvent.click(screen.getByLabelText(i18next.t("common.close")));

    await waitFor(() => {
      expect(screen.queryByTestId("chat-model-picker")).not.toBeInTheDocument();
    });
  });
});
