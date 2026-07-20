/**
 * Stream reattach (resumeInFlight) + live-attachment reduction + the
 * journal/token double-text guard.
 *
 * Generation is not tied to the browser connection: navigating away
 * mid-turn and back used to show only the committed history and never
 * the live tail. `resumeInFlight` detects an `in_progress` latest turn,
 * rebuilds the pending bubble from the journal event backlog (JSON
 * replay), then tails `/events/live` from where the backlog ended and
 * finalizes on the journal terminal event.
 */
import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { LiveEvent } from "@/lib/sessions/event-stream";

// --- module mocks ----------------------------------------------------------

let capturedOnEvent: ((live: LiveEvent) => void) | null = null;
let capturedLastEventId: string | undefined;
const closeSpy = vi.fn();

vi.mock("@/lib/sessions/event-stream", () => ({
  openLiveEventStream: vi.fn(
    (
      _key: string,
      opts: { onEvent: (live: LiveEvent) => void; initialLastEventId?: string },
    ) => {
      capturedOnEvent = opts.onEvent;
      capturedLastEventId = opts.initialLastEventId;
      return closeSpy;
    },
  ),
}));

const listSessionTurnsMock = vi.fn();
const fetchTurnEventsMock = vi.fn();
vi.mock("@/lib/api/sessions", () => ({
  listSessionTurns: (...args: unknown[]) => listSessionTurnsMock(...args),
  fetchTurnEvents: (...args: unknown[]) => fetchTurnEventsMock(...args),
}));

vi.mock("@/lib/api/chat", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/chat")>(
    "@/lib/api/chat",
  );
  return {
    ...actual,
    streamChatCompletions: vi.fn(async function* () {
      await new Promise(() => {}); // hangs — tests that need it never resolve it
    }),
    cancelChatSession: vi.fn(async () => ({ status: "cancelled" })),
  };
});

import { useChatStream } from "@/lib/chat/use-chat-stream";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return React.createElement(QueryClientProvider, { client: qc }, children);
}

function env(
  turnId: string,
  sequence: number,
  eventType: string,
  payload: unknown,
): LiveEvent {
  return {
    turn_id: turnId,
    sequence,
    timestamp_ms: 1000 + sequence,
    event_type: eventType as LiveEvent["event_type"],
    payload,
  };
}

beforeEach(() => {
  capturedOnEvent = null;
  capturedLastEventId = undefined;
  closeSpy.mockClear();
  listSessionTurnsMock.mockReset();
  fetchTurnEventsMock.mockReset();
});

describe("useChatStream.resumeInFlight", () => {
  it("rebuilds the pending bubble from the backlog and finalizes on the live terminal event", async () => {
    listSessionTurnsMock.mockResolvedValue([
      { turn_id: "t9", status: "in_progress" },
    ]);
    fetchTurnEventsMock.mockResolvedValue([
      env("t9", 0, "TurnStart", { model: "m" }),
      env("t9", 1, "TextDelta", { index: 0, text: "partial " }),
      env("t9", 2, "ToolStateRunning", {
        tool_call_id: "c1",
        tool_name: "image_generate",
        args_json: "{}",
        started_at_ms: 1,
      }),
    ]);

    const { result } = renderHook(
      () => useChatStream({ sessionKey: "s-resume", model: "m" }),
      { wrapper },
    );

    await act(async () => {
      await result.current.resumeInFlight();
    });

    // Backlog reconstructed: text + the journal-named tool call (the wire
    // field is `tool_call_id` — the old `call_id` mapping never matched).
    expect(result.current.pendingMessage).not.toBeNull();
    expect(result.current.pendingMessage!.content).toBe("partial ");
    expect(result.current.pendingMessage!.toolCalls?.[0]?.callId).toBe("c1");
    expect(result.current.isStreaming).toBe(true);
    // Live tail resumes exactly after the last backlog sequence.
    expect(capturedLastEventId).toBe("t9:2");

    // Live tail: more text (allowed — no fetch owns this turn), an
    // attachment, then the terminal event.
    act(() => {
      capturedOnEvent!(env("t9", 3, "TextDelta", { index: 0, text: "tail" }));
      capturedOnEvent!(
        env("t9", 4, "AttachmentAdded", {
          kind: "image",
          url: "/v1/files/abc123",
          name: "cat.png",
          mime: "image/png",
        }),
      );
      capturedOnEvent!(
        env("t9", 5, "TurnComplete", { finish_reason: "stop", usage: {} }),
      );
    });

    await waitFor(() => {
      expect(result.current.pendingMessage).toBeNull();
    });
    // Committed into history with the streamed tail + attachment.
    const committed = result.current.messages.at(-1)!;
    expect(committed.content).toBe("partial tail");
    expect(committed.attachments?.[0]?.name).toBe("cat.png");
    expect(committed.attachments?.[0]?.remoteUrl).toContain("/v1/files/abc123");
    expect(committed.pending).toBe(false);
    expect(result.current.isStreaming).toBe(false);
  });

  it("renders a user bubble from the turn's user_text_preview (C2 / #108)", async () => {
    listSessionTurnsMock.mockResolvedValue([
      {
        turn_id: "t10",
        status: "in_progress",
        started_at_ms: 1234,
        user_text_preview: "what's the weather?",
      },
    ]);
    fetchTurnEventsMock.mockResolvedValue([
      env("t10", 0, "TurnStart", { model: "m" }),
      env("t10", 1, "TextDelta", { index: 0, text: "checking…" }),
    ]);

    const { result } = renderHook(
      () => useChatStream({ sessionKey: "s-user-bubble", model: "m" }),
      { wrapper },
    );
    await act(async () => {
      await result.current.resumeInFlight();
    });

    // The settled transcript excludes the in-progress turn, so the resume
    // path itself must re-render the user's message above the live bubble.
    const userMsg = result.current.messages.find((m) => m.role === "user");
    expect(userMsg).toBeDefined();
    expect(userMsg!.content).toBe("what's the weather?");
    expect(userMsg!.turnId).toBe("t10");
    expect(userMsg!.createdAt).toBe(1234);
    expect(result.current.pendingMessage!.content).toBe("checking…");

    // Resuming twice (e.g. rapid tab focus events) must not duplicate it —
    // and the second resume no-ops anyway while the first owns the turn.
    await act(async () => {
      await result.current.resumeInFlight();
    });
    expect(
      result.current.messages.filter((m) => m.role === "user"),
    ).toHaveLength(1);
  });

  it("skips the user bubble when the preview is empty", async () => {
    listSessionTurnsMock.mockResolvedValue([
      { turn_id: "t11", status: "in_progress", user_text_preview: "" },
    ]);
    fetchTurnEventsMock.mockResolvedValue([]);
    const { result } = renderHook(
      () => useChatStream({ sessionKey: "s-no-preview", model: "m" }),
      { wrapper },
    );
    await act(async () => {
      await result.current.resumeInFlight();
    });
    expect(result.current.messages.find((m) => m.role === "user")).toBeUndefined();
    expect(result.current.pendingMessage).not.toBeNull();
  });

  it("no-ops when the latest turn is settled", async () => {
    listSessionTurnsMock.mockResolvedValue([
      { turn_id: "t1", status: "completed" },
    ]);
    const { result } = renderHook(
      () => useChatStream({ sessionKey: "s-settled", model: "m" }),
      { wrapper },
    );
    await act(async () => {
      await result.current.resumeInFlight();
    });
    expect(result.current.pendingMessage).toBeNull();
    expect(result.current.isStreaming).toBe(false);
    expect(fetchTurnEventsMock).not.toHaveBeenCalled();
  });

  it("dedups an attachment that arrives via both token stream and journal", async () => {
    listSessionTurnsMock.mockResolvedValue([
      { turn_id: "t2", status: "in_progress" },
    ]);
    fetchTurnEventsMock.mockResolvedValue([
      env("t2", 0, "AttachmentAdded", {
        kind: "file",
        url: "/v1/files/doc1",
        name: "report.pdf",
        mime: "application/pdf",
      }),
    ]);
    const { result } = renderHook(
      () => useChatStream({ sessionKey: "s-dedup", model: "m" }),
      { wrapper },
    );
    await act(async () => {
      await result.current.resumeInFlight();
    });
    // Same url again from the live stream (e.g. replay overlap).
    act(() => {
      capturedOnEvent!(
        env("t2", 7, "AttachmentAdded", {
          kind: "file",
          url: "/v1/files/doc1",
          name: "report.pdf",
          mime: "application/pdf",
        }),
      );
    });
    const atts = result.current.pendingMessage!.attachments!;
    expect(atts).toHaveLength(1);
    // Non-media kinds map onto the renderable "document" card.
    expect(atts[0].kind).toBe("document");
  });

  it("drops journal text deltas while a fetch owns the turn (double-text guard)", async () => {
    const { result } = renderHook(
      () => useChatStream({ sessionKey: "s-guard", model: "m" }),
      { wrapper },
    );
    // Open a fetch-owned turn (the mocked stream hangs forever).
    await act(async () => {
      void result.current.sendMessage("hi");
      await Promise.resolve();
    });
    expect(result.current.pendingMessage).not.toBeNull();

    const turn = result.current.pendingMessage!.turnId ?? "tX";
    act(() => {
      // Journal echo of the same tokens the fetch streams — must be dropped.
      capturedOnEvent!(env(turn, 1, "TextDelta", { index: 0, text: "dup" }));
      // Journal-only signals still apply.
      capturedOnEvent!(
        env(turn, 2, "AttachmentAdded", {
          kind: "image",
          url: "/v1/files/x1",
          name: "x.png",
          mime: "image/png",
        }),
      );
    });
    expect(result.current.pendingMessage!.content).toBe("");
    expect(result.current.pendingMessage!.attachments).toHaveLength(1);
  });
});
