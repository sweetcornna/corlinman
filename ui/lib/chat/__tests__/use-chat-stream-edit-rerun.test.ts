/**
 * Regression: B2-ui-edit-rerun — `editAndRerun` must send the *truncated*
 * history (everything before the edited message + the edited content), NOT
 * the stale, full pre-truncation history.
 *
 * The bug: `editAndRerun` truncated `messages` via
 * `setMessages(prev => [...prev.slice(0, idx), edited])` then did
 * `await Promise.resolve(); await runTurn(editedUser)`. But `runTurn` is a
 * `useCallback` closing over the PRE-truncation `messages` value (deps
 * `[args, messages, reduceEvent]`); a microtask does not re-commit React
 * state nor recreate the callback, so `runTurn`'s
 * `for (const m of messages)` loop built the request body from the FULL
 * original history and then appended the edited content. The model saw
 * stale, un-truncated history while the UI showed the truncated view.
 *
 * This test hydrates [u1, a1, u2, a2], captures the request body handed to
 * `streamChatCompletions`, calls `editAndRerun(u1.id, "EDITED")`, and
 * asserts the request `messages` is exactly [{role:user, content:"EDITED"}]
 * — it must NOT contain a1/u2/a2 nor the original u1 text.
 */
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { ChatCompletionRequest } from "@/lib/api/chat";
import type { ChatMessage } from "@/lib/chat/types";

// --- module mocks ----------------------------------------------------------
//
// Mock the live-event-stream client (no-op close fn) and the OpenAI-compat
// completions stream so we can capture the exact request body that
// `runTurn` builds for the edit-and-rerun turn.

vi.mock("@/lib/sessions/event-stream", () => ({
  openLiveEventStream: vi.fn(() => vi.fn()),
}));

const capturedBodies: ChatCompletionRequest[] = [];

vi.mock("@/lib/api/chat", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/chat")>(
    "@/lib/api/chat",
  );
  return {
    ...actual,
    // Capture the body, then drive a single text delta + finish so the
    // turn completes cleanly and the hook settles.
    streamChatCompletions: vi.fn(async function* (body: ChatCompletionRequest) {
      capturedBodies.push(body);
      yield {
        choices: [{ index: 0, delta: { content: "ok" }, finish_reason: "stop" }],
        corlinman: { turn_id: "t-edit" },
      };
    }),
    cancelChatSession: vi.fn(async () => ({ status: "cancelled" })),
  };
});

// --- import under test (after mocks) --------------------------------------

import { useChatStream } from "@/lib/chat/use-chat-stream";

// --- harness --------------------------------------------------------------

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return React.createElement(QueryClientProvider, { client: qc }, children);
}

function msg(id: string, role: ChatMessage["role"], content: string): ChatMessage {
  return { id, role, content, createdAt: Date.now() };
}

beforeEach(() => {
  capturedBodies.length = 0;
});

afterEach(() => {
  vi.clearAllTimers();
});

describe("useChatStream — editAndRerun (B2-ui-edit-rerun)", () => {
  it("sends the truncated history (only the edited user message), not the stale full history", async () => {
    const { result } = renderHook(
      () =>
        useChatStream({
          sessionKey: "test-session",
          model: "test-model",
        }),
      { wrapper },
    );

    // Hydrate a 4-message conversation: u1, a1, u2, a2.
    act(() => {
      result.current.hydrate([
        msg("u1", "user", "original first question"),
        msg("a1", "assistant", "first answer"),
        msg("u2", "user", "second question"),
        msg("a2", "assistant", "second answer"),
      ]);
    });

    // Edit the FIRST user message and re-run. Everything after u1 must be
    // dropped, and the request must be built from the truncated history.
    await act(async () => {
      await result.current.editAndRerun("u1", "EDITED");
    });

    expect(capturedBodies).toHaveLength(1);
    const sent = capturedBodies[0].messages;

    // The request must contain exactly the edited user message — no
    // system prompt was configured, so a single message is expected.
    expect(sent).toEqual([{ role: "user", content: "EDITED" }]);

    // Explicit negative assertions for clarity on failure.
    const contents = sent.map((m) => m.content);
    expect(contents).not.toContain("original first question");
    expect(contents).not.toContain("first answer");
    expect(contents).not.toContain("second question");
    expect(contents).not.toContain("second answer");
  });
});
