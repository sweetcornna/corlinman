/**
 * Perf regression: PERF-010 — `reduceEvent` must NOT rebuild the entire
 * `pendingMessage` on every event.
 *
 * The old implementation spread `prev` and *unconditionally* shallow-copied
 * `toolCalls` (per element), `subagents` (per element, incl. each nested
 * `events` array), `approvals` (per element) and `usage` on EVERY event —
 * including pure token-content deltas that only touch `content`. For long
 * streams with many tool calls / sub-agents, every single text-delta then
 * re-allocated those sub-structures, defeating React's referential-equality
 * memoization downstream (every tool card / subagent card re-renders on each
 * token).
 *
 * Root-cause fix: clone only the branch the event actually mutates and reuse
 * the references for untouched branches, while still never mutating `prev`
 * in place (so React's `setState` sees a new top-level object).
 *
 * This test drives a content-only `text-delta` through the reducer and
 * asserts:
 *   - `content` is updated immutably (new top-level object),
 *   - the untouched `toolCalls` / `subagents` / `approvals` / `usage`
 *     references are PRESERVED (reference-equal to the previous render),
 *   - and that a `tool-completed` event (which *does* touch toolCalls) still
 *     clones `toolCalls` while leaving `subagents` untouched.
 */
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { ChatEvent } from "@/lib/chat/types";

// --- module mocks ----------------------------------------------------------
//
// Capture the live-stream `onEvent` callback so the test can inject
// arbitrary events into `reduceEvent`. The close fn is a noop spy.
let capturedOnEvent: ((live: unknown) => void) | null = null;

vi.mock("@/lib/sessions/event-stream", () => ({
  openLiveEventStream: vi.fn(
    (_key: string, opts: { onEvent: (live: unknown) => void }) => {
      capturedOnEvent = opts.onEvent;
      return vi.fn();
    },
  ),
}));

// Make `liveEventToChatEvent` a pass-through: the test injects fully-formed
// `ChatEvent`s and we want them reduced verbatim. `EventDedupSet` stays real
// so the (turnId, sequence) dedup still behaves like production.
vi.mock("@/lib/chat/event-merger", async () => {
  const actual = await vi.importActual<typeof import("@/lib/chat/event-merger")>(
    "@/lib/chat/event-merger",
  );
  return {
    ...actual,
    liveEventToChatEvent: (ev: unknown) => ev as ChatEvent,
  };
});

// Hold the turn open so `pendingMessage` stays non-null while we inject
// events. The generator blocks on a deferred we resolve in afterEach.
type Deferred = { promise: Promise<void>; resolve: () => void };
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
      await d.promise;
    }),
    cancelChatSession: vi.fn(async () => ({ cancelled: true })),
  };
});

// --- import under test (after mocks) --------------------------------------
import { useChatStream } from "@/lib/chat/use-chat-stream";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return React.createElement(QueryClientProvider, { client: qc }, children);
}

beforeEach(() => {
  capturedOnEvent = null;
  streamDeferreds.length = 0;
});

afterEach(() => {
  for (const d of streamDeferreds) d.resolve();
});

/** Fire a fully-formed ChatEvent through the captured live `onEvent`. */
function fire(ev: ChatEvent) {
  if (!capturedOnEvent) throw new Error("live onEvent not captured");
  act(() => {
    capturedOnEvent!(ev);
  });
}

describe("useChatStream — reduceEvent allocation (PERF-010)", () => {
  it("a content-only text-delta does not re-allocate untouched sub-structures", async () => {
    const { result } = renderHook(
      () => useChatStream({ sessionKey: "perf-session", model: "m" }),
      { wrapper },
    );

    // Open a turn so `pendingMessage` exists. Don't await — the stream hangs.
    await act(async () => {
      void result.current.sendMessage("hi");
      await Promise.resolve();
    });

    const TURN = result.current.pendingMessage!.turnId ?? "t1";

    // Seed the pending message with one tool call, one subagent (with a
    // nested events entry), one approval and a usage block. We use a single
    // turnId and a monotonically-increasing sequence so the dedup set lets
    // every event through.
    let seq = 0;
    fire({
      kind: "tool-running",
      turnId: TURN,
      sequence: seq++,
      callId: "c1",
      toolName: "bash",
      argsJson: "{}",
      startedAtMs: 1,
    });
    fire({
      kind: "subagent-spawned",
      turnId: TURN,
      sequence: seq++,
      childSessionKey: "child-1",
      depth: 1,
    });
    fire({
      kind: "subagent-event",
      turnId: TURN,
      sequence: seq++,
      childSessionKey: "child-1",
      // The envelope just needs to be an object the reducer pushes verbatim.
      envelope: { foo: "bar" } as never,
    });
    fire({
      kind: "awaiting-approval",
      turnId: TURN,
      sequence: seq++,
      callId: "a1",
      plugin: "p",
      tool: "t",
      argsPreviewJson: "{}",
    });
    fire({
      kind: "turn-complete",
      turnId: TURN,
      sequence: seq++,
      usage: { inputTokens: 5 },
    });

    // Snapshot references AFTER the structures exist.
    const before = result.current.pendingMessage!;
    const beforeToolCalls = before.toolCalls;
    const beforeTool0 = before.toolCalls![0];
    const beforeSubagents = before.subagents;
    const beforeSub0 = before.subagents![0];
    const beforeSub0Events = before.subagents![0].events;
    const beforeApprovals = before.approvals;
    const beforeApproval0 = before.approvals![0];
    const beforeUsage = before.usage;
    const beforeContent = before.content;

    // Now fire a pure content delta. It touches ONLY `content`.
    fire({ kind: "text-delta", turnId: TURN, sequence: seq++, text: "hello" });

    const after = result.current.pendingMessage!;

    // content updated immutably: new top-level object, appended text.
    expect(after).not.toBe(before);
    expect(after.content).toBe(beforeContent + "hello");

    // The untouched branches must keep their references (no re-allocation).
    expect(after.toolCalls).toBe(beforeToolCalls);
    expect(after.toolCalls![0]).toBe(beforeTool0);
    expect(after.subagents).toBe(beforeSubagents);
    expect(after.subagents![0]).toBe(beforeSub0);
    expect(after.subagents![0].events).toBe(beforeSub0Events);
    expect(after.approvals).toBe(beforeApprovals);
    expect(after.approvals![0]).toBe(beforeApproval0);
    expect(after.usage).toBe(beforeUsage);
  });

  it("a tool-completed event clones toolCalls but leaves subagents untouched", async () => {
    const { result } = renderHook(
      () => useChatStream({ sessionKey: "perf-session-2", model: "m" }),
      { wrapper },
    );

    await act(async () => {
      void result.current.sendMessage("hi");
      await Promise.resolve();
    });

    const TURN = result.current.pendingMessage!.turnId ?? "t1";
    let seq = 0;
    fire({
      kind: "tool-running",
      turnId: TURN,
      sequence: seq++,
      callId: "c1",
      toolName: "bash",
      argsJson: "{}",
      startedAtMs: 1,
    });
    fire({
      kind: "subagent-spawned",
      turnId: TURN,
      sequence: seq++,
      childSessionKey: "child-1",
      depth: 1,
    });

    const before = result.current.pendingMessage!;
    const beforeToolCalls = before.toolCalls;
    const beforeTool0 = before.toolCalls![0];
    const beforeSubagents = before.subagents;
    const beforeSub0 = before.subagents![0];

    // tool-completed mutates the matching tool call → toolCalls branch must
    // be cloned (new array + new element), but subagents must NOT be touched.
    fire({
      kind: "tool-completed",
      turnId: TURN,
      sequence: seq++,
      callId: "c1",
      resultPreview: "done",
      durationMs: 10,
      isError: false,
    });

    const after = result.current.pendingMessage!;
    expect(after).not.toBe(before);
    // toolCalls cloned (immutable update for React).
    expect(after.toolCalls).not.toBe(beforeToolCalls);
    expect(after.toolCalls![0]).not.toBe(beforeTool0);
    expect(after.toolCalls![0].status).toBe("ok");
    expect(after.toolCalls![0].resultPreview).toBe("done");
    // subagents untouched → reference preserved.
    expect(after.subagents).toBe(beforeSubagents);
    expect(after.subagents![0]).toBe(beforeSub0);
  });
});
