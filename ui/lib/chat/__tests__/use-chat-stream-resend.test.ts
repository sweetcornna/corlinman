/**
 * Regression: R1-005 — rapid resend must close the previous live SSE
 * before opening a new one.
 *
 * The `closeLiveRef.current = openLiveEventStream(...)` reassignment inside
 * `runTurn` previously overwrote the prior close-fn without calling it. A
 * second `sendMessage` issued while the first turn was still streaming
 * would therefore leak the first `EventSource`; its `onEvent` callback
 * would continue reducing into a stale `pendingMessage` until the page
 * unmounted (or the user navigated away).
 *
 * This test stubs `openLiveEventStream` with a close-fn spy and verifies
 * that each new `runTurn` closes the prior live stream before opening
 * the next one.
 */
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// --- module mocks ----------------------------------------------------------
//
// We mock both the live-event-stream client (so we can count opens/closes)
// and the OpenAI-compat completions stream (so we can hold a turn open
// long enough to race a second `sendMessage` against it).

const openCalls: Array<() => void> = [];
const closeSpy = vi.fn();

vi.mock("@/lib/sessions/event-stream", () => ({
  openLiveEventStream: vi.fn(() => {
    // Each invocation returns its own close fn so the test can detect
    // whether a *specific* previous close fn was invoked.
    const fn = vi.fn(closeSpy);
    openCalls.push(fn);
    return fn;
  }),
}));

// A controllable async generator: `streamChatCompletions` returns one
// pending generator per call. The generator yields nothing and only
// resolves when we manually fire its deferred. That lets us call
// `sendMessage` twice without awaiting — turn #1 is mid-flight at the
// moment turn #2 starts, which is exactly the leak scenario.
type Deferred = {
  promise: Promise<void>;
  resolve: () => void;
};

function makeDeferred(): Deferred {
  let resolve: () => void = () => {};
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const streamDeferreds: Deferred[] = [];

vi.mock("@/lib/api/chat", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/chat")>(
    "@/lib/api/chat",
  );
  return {
    ...actual,
    streamChatCompletions: vi.fn(async function* () {
      const d = makeDeferred();
      streamDeferreds.push(d);
      // Block here until the test explicitly resolves us. No chunks
      // yielded — `finishReceived` stays false, which is fine because
      // we never abort and never throw.
      await d.promise;
    }),
    cancelChatSession: vi.fn(async () => ({ cancelled: true })),
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

beforeEach(() => {
  openCalls.length = 0;
  streamDeferreds.length = 0;
  closeSpy.mockClear();
});

afterEach(() => {
  // Drain any still-pending generators so vitest's afterEach doesn't
  // leave dangling promises that show up in the next test.
  for (const d of streamDeferreds) d.resolve();
});

describe("useChatStream — rapid resend (R1-005)", () => {
  it("closes the previous live SSE before opening a new one on a second send", async () => {
    const { result } = renderHook(
      () =>
        useChatStream({
          sessionKey: "test-session",
          model: "test-model",
        }),
      { wrapper },
    );

    // Kick off turn #1 — do NOT await. The mocked `streamChatCompletions`
    // hangs on its deferred so the turn stays in-flight.
    let pending1: Promise<void> | null = null;
    await act(async () => {
      pending1 = result.current.sendMessage("first message");
      // microtask flush so React commits the user message and runTurn
      // opens the live stream.
      await Promise.resolve();
    });

    expect(openCalls).toHaveLength(1);
    const firstClose = openCalls[0];
    expect(firstClose).not.toHaveBeenCalled();

    // Now issue a second send while turn #1 is still streaming. The bug:
    // `runTurn` reassigns `closeLiveRef.current` to the new close fn
    // without invoking the existing one — the first EventSource leaks.
    let pending2: Promise<void> | null = null;
    await act(async () => {
      pending2 = result.current.sendMessage("second message");
      await Promise.resolve();
    });

    // The second openLiveEventStream call should have fired by now.
    expect(openCalls.length).toBeGreaterThanOrEqual(2);

    // The critical assertion: the prior close fn MUST have been
    // invoked exactly once before the second stream was opened.
    expect(firstClose).toHaveBeenCalledTimes(1);

    // Cleanup: drain both pending generators so the hook can settle.
    await act(async () => {
      for (const d of streamDeferreds) d.resolve();
      await pending1;
      await pending2;
    });
  });
});
