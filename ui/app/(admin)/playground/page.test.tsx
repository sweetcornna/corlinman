/**
 * PlaygroundPage smoke test (F1).
 *
 * Covers the three contracts the new page introduces:
 *
 *   1. Overview cards render in the offline state — every admin query is
 *      stubbed to reject so the StatChips fall back to ``—`` + the
 *      ``endpointOffline`` foot string.
 *   2. The chat composer + send button render and stay disabled until the
 *      operator types a message.
 *   3. Sending a message POSTs `/v1/chat/completions` with `stream: true`,
 *      and a mocked SSE response surfaces in the transcript.
 *
 * The transcript test mocks `fetch` to return a `ReadableStream`-shaped
 * body emitting OpenAI-shape SSE chunks — the page's inline
 * `consumeChatSse` reader consumes them and the assistant turn shows the
 * streamed text.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nextProvider } from "react-i18next";

import { initI18n, i18next } from "@/lib/i18n";

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => "/playground",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({}),
}));

// `openEventStream` opens a real EventSource by default; jsdom doesn't
// implement it. Stub a no-op cleaner so the activity-tail effect mounts
// without blowing up.
vi.mock("@/lib/sse", () => ({
  openEventStream: vi.fn(() => () => undefined),
}));

import PlaygroundPage from "./page";

const i18n = initI18n();
void i18next.changeLanguage("zh-CN");

function wrap(): React.ReactElement {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <I18nextProvider i18n={i18n}>
        <PlaygroundPage />
      </I18nextProvider>
    </QueryClientProvider>
  );
}

/** Build a `Response` whose `body` streams a list of utf-8 string chunks. */
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[i]!));
      i += 1;
    },
  });
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    body: stream,
    text: async () => chunks.join(""),
    json: async () => ({}),
  } as unknown as Response;
}

function offlineResponse(): Response {
  return {
    ok: false,
    status: 503,
    headers: { get: () => null },
    text: async () => "offline",
    json: async () => ({}),
  } as unknown as Response;
}

function emptyAgentsResponse(): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    text: async () => "[]",
    json: async () => [],
  } as unknown as Response;
}

beforeEach(() => {
  // Every overview query 503s so the chips render the offline foot copy.
  // The AgentPicker hits `/admin/agents` — return [] so it renders empty
  // rather than throwing.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (typeof url === "string" && url.includes("/admin/agents")) {
        return emptyAgentsResponse();
      }
      return offlineResponse();
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("PlaygroundPage", () => {
  it("renders overview stat chips + chat composer in offline state", async () => {
    render(wrap());

    expect(await screen.findByTestId("playground-page")).toBeInTheDocument();
    expect(screen.getByTestId("stat-plugins")).toBeInTheDocument();
    expect(screen.getByTestId("stat-agents")).toBeInTheDocument();
    expect(screen.getByTestId("stat-personas")).toBeInTheDocument();
    expect(screen.getByTestId("stat-approvals")).toBeInTheDocument();

    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
    expect(screen.getByTestId("chat-composer")).toBeInTheDocument();
    expect(screen.getByTestId("chat-send")).toBeInTheDocument();
    expect(screen.getByTestId("chat-empty")).toBeInTheDocument();

    // Send button stays disabled until the operator types something.
    const send = screen.getByTestId("chat-send") as HTMLButtonElement;
    expect(send.disabled).toBe(true);
  });

  it("sending a message streams the assistant reply into the transcript", async () => {
    // Override fetch for this test: /admin/agents → [], /v1/chat/completions
    // → SSE chunks, everything else → 503.
    const sseChunks = [
      'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":"hel"},"finish_reason":null}]}\n\n',
      'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"content":"lo!"},"finish_reason":null}]}\n\n',
      'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
      "data: [DONE]\n\n",
    ];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (typeof url === "string" && url.includes("/v1/chat/completions")) {
        expect(init?.method).toBe("POST");
        const body = init?.body;
        expect(typeof body).toBe("string");
        const parsed = JSON.parse(body as string) as Record<string, unknown>;
        expect(parsed.stream).toBe(true);
        expect(parsed.model).toBe("gpt-4o");
        expect(Array.isArray(parsed.messages)).toBe(true);
        return sseResponse(sseChunks);
      }
      if (typeof url === "string" && url.includes("/admin/agents")) {
        return emptyAgentsResponse();
      }
      return offlineResponse();
    });
    vi.stubGlobal("fetch", fetchMock);

    render(wrap());

    const composer = (await screen.findByTestId(
      "chat-composer",
    )) as HTMLTextAreaElement;
    await act(async () => {
      fireEvent.change(composer, { target: { value: "hi there" } });
    });

    const send = screen.getByTestId("chat-send") as HTMLButtonElement;
    expect(send.disabled).toBe(false);

    await act(async () => {
      fireEvent.click(send);
    });

    // The user turn shows up immediately.
    await waitFor(() => {
      expect(screen.getByTestId("chat-turn-user")).toHaveTextContent("hi there");
    });

    // The assistant turn fills as the mock SSE chunks drain.
    await waitFor(
      () => {
        const assistant = screen.getByTestId("chat-turn-assistant");
        expect(assistant).toHaveTextContent("hello!");
        expect(assistant.getAttribute("data-pending")).toBeNull();
      },
      { timeout: 2000 },
    );

    // Exactly one fetch hit /v1/chat/completions.
    const chatCalls = fetchMock.mock.calls.filter(
      (c) => typeof c[0] === "string" && (c[0] as string).includes("/v1/chat/completions"),
    );
    expect(chatCalls).toHaveLength(1);
  });
});
