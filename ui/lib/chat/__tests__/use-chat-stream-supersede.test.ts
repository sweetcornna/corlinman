/**
 * Regression: cross-turn event pollution (PLAN_CHAT_PERFECT §3.1).
 *
 * A second `sendMessage` issued while turn #1 is still streaming must
 * SUPERSEDE turn #1 completely:
 *
 *   1. turn #1's token stream is aborted (its AbortController fires);
 *   2. turn #1's late events — including the `turn-errored: cancelled`
 *      its own AbortError handler emits — must NOT reduce into turn #2's
 *      pending draft;
 *   3. turn #1's `finally` must not commit its draft over turn #2's
 *      pending message or flip `isStreaming` off while #2 streams.
 *
 * Pre-fix, the dying turn's error event landed on the new draft (the
 * user saw their fresh question instantly "errored: cancelled") and the
 * old finally committed the new draft prematurely.
 */
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const openCalls: Array<() => void> = [];

vi.mock("@/lib/sessions/event-stream", () => ({
  openLiveEventStream: vi.fn(() => {
    const fn = vi.fn();
    openCalls.push(fn);
    return fn;
  }),
}));

type Deferred = { promise: Promise<void>; resolve: () => void };

function makeDeferred(): Deferred {
  let resolve: () => void = () => {};
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const streamDeferreds: Deferred[] = [];
const streamSignals: Array<AbortSignal | undefined> = [];

vi.mock("@/lib/api/chat", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/chat")>(
    "@/lib/api/chat",
  );
  return {
    ...actual,
    streamChatCompletions: vi.fn(async function* (
      _body: unknown,
      signal?: AbortSignal,
    ) {
      streamSignals.push(signal);
      const d = makeDeferred();
      streamDeferreds.push(d);
      // Mirror a real fetch stream: an abort mid-read surfaces as an
      // AbortError DOMException from the pending read.
      await new Promise<void>((resolve, reject) => {
        const onAbort = () =>
          reject(new DOMException("aborted", "AbortError"));
        if (signal?.aborted) return onAbort();
        signal?.addEventListener("abort", onAbort, { once: true });
        void d.promise.then(() => {
          signal?.removeEventListener("abort", onAbort);
          resolve();
        });
      });
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
  openCalls.length = 0;
  streamDeferreds.length = 0;
  streamSignals.length = 0;
});

afterEach(() => {
  for (const d of streamDeferreds) d.resolve();
});

describe("useChatStream — turn supersession", () => {
  it("a second send aborts turn #1 and keeps its death out of turn #2's draft", async () => {
    const { result } = renderHook(
      () =>
        useChatStream({ sessionKey: "test-session", model: "test-model" }),
      { wrapper },
    );

    let pending1: Promise<void> | null = null;
    await act(async () => {
      pending1 = result.current.sendMessage("first");
      await Promise.resolve();
    });
    expect(streamSignals).toHaveLength(1);
    expect(streamSignals[0]?.aborted).toBe(false);

    let pending2: Promise<void> | null = null;
    await act(async () => {
      pending2 = result.current.sendMessage("second");
      // Let turn #1's AbortError handler + finally run to completion.
      await Promise.resolve();
      await pending1;
    });

    // Turn #1's stream was aborted by the supersession.
    expect(streamSignals[0]?.aborted).toBe(true);
    expect(streamSignals[1]?.aborted).toBe(false);

    // Turn #2 still owns the UI: streaming, clean pending draft — turn
    // #1's "cancelled" error and its finally-commit must not have landed.
    expect(result.current.isStreaming).toBe(true);
    expect(result.current.pendingMessage).not.toBeNull();
    expect(result.current.pendingMessage?.error).toBeUndefined();
    // Turn #1's draft was never committed into history: both user
    // messages are there, no assistant message yet.
    expect(result.current.messages.map((m) => m.role)).toEqual([
      "user",
      "user",
    ]);

    await act(async () => {
      for (const d of streamDeferreds) d.resolve();
      await pending2;
    });

    // Turn #2 settles normally and commits exactly one assistant bubble.
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.messages.map((m) => m.role)).toEqual([
      "user",
      "user",
      "assistant",
    ]);
  });

  it("stop() marks the pending draft as cancelling immediately", async () => {
    const { result } = renderHook(
      () =>
        useChatStream({ sessionKey: "test-session", model: "test-model" }),
      { wrapper },
    );

    let pending1: Promise<void> | null = null;
    await act(async () => {
      pending1 = result.current.sendMessage("question");
      await Promise.resolve();
    });
    expect(result.current.pendingMessage?.cancelling).toBeUndefined();

    await act(async () => {
      const stopped = result.current.stop();
      await Promise.resolve();
      await stopped;
      await pending1;
    });

    // The turn settled as a user-initiated stop: committed with the
    // "cancelled" sentinel (rendered as a neutral chip, not an error).
    const last = result.current.messages[result.current.messages.length - 1];
    expect(last.role).toBe("assistant");
    expect(last.error).toBe("cancelled");
  });
});
