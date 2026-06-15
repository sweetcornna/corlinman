import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { ChatCompletionRequest } from "@/lib/api/chat";

const streamBodies: ChatCompletionRequest[] = [];

vi.mock("@/lib/sessions/event-stream", () => ({
  openLiveEventStream: vi.fn(() => vi.fn()),
}));

vi.mock("@/lib/api/chat", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/chat")>(
    "@/lib/api/chat",
  );
  return {
    ...actual,
    streamChatCompletions: vi.fn(async function* (
      body: ChatCompletionRequest,
    ) {
      streamBodies.push(body);
      yield {
        choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
      };
    }),
    cancelChatSession: vi.fn(async () => ({ cancelled: true })),
  };
});

import { useChatStream } from "@/lib/chat/use-chat-stream";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return React.createElement(QueryClientProvider, { client: qc }, children);
}

beforeEach(() => {
  streamBodies.length = 0;
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useChatStream — reasoning effort", () => {
  it("includes the selected reasoning effort in the chat request body", async () => {
    const { result } = renderHook(
      () =>
        useChatStream({
          sessionKey: "test-session",
          model: "gpt-5.5",
          reasoningEffort: "high",
        }),
      { wrapper },
    );

    await act(async () => {
      await result.current.sendMessage("think carefully");
    });

    expect(streamBodies).toHaveLength(1);
    expect(streamBodies[0].reasoning_effort).toBe("high");
  });
});
