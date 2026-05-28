import { describe, expect, it } from "vitest";

import {
  EventDedupSet,
  chunkToChatEvents,
  liveEventToChatEvent,
} from "@/lib/chat/event-merger";
import type { LiveEvent } from "@/lib/sessions/event-stream";
import type { ChatCompletionChunk } from "@/lib/api/chat";

const TURN = "turn_test";

describe("chunkToChatEvents", () => {
  it("emits text-delta from content", () => {
    const chunk: ChatCompletionChunk = {
      choices: [{ index: 0, delta: { content: "hello" } }],
    };
    const events = chunkToChatEvents(chunk, TURN);
    expect(events).toEqual([
      { kind: "text-delta", turnId: TURN, sequence: -1, text: "hello" },
    ]);
  });

  it("emits reasoning-delta from reasoning_content", () => {
    const chunk: ChatCompletionChunk = {
      choices: [{ index: 0, delta: { reasoning_content: "thinking" } }],
    };
    const events = chunkToChatEvents(chunk, TURN);
    expect(events[0].kind).toBe("reasoning-delta");
  });

  it("emits tool-input-delta for streamed args", () => {
    const chunk: ChatCompletionChunk = {
      choices: [
        {
          index: 0,
          delta: {
            tool_calls: [
              { index: 0, id: "c1", function: { arguments: '{"a":1}' } },
            ],
          },
        },
      ],
    };
    const events = chunkToChatEvents(chunk, TURN);
    expect(events[0]).toMatchObject({
      kind: "tool-input-delta",
      callId: "c1",
      delta: '{"a":1}',
    });
  });

  it("emits turn-complete on finish_reason", () => {
    const chunk: ChatCompletionChunk = {
      choices: [
        { index: 0, delta: {}, finish_reason: "stop" },
      ],
    };
    const events = chunkToChatEvents(chunk, TURN);
    expect(events).toEqual([
      {
        kind: "turn-complete",
        turnId: TURN,
        sequence: -1,
        usage: { finishReason: "stop" },
      },
    ]);
  });

  it("prefers corlinman.turn_id over fallback", () => {
    const chunk: ChatCompletionChunk = {
      corlinman: { turn_id: "real_turn" },
      choices: [{ index: 0, delta: { content: "x" } }],
    };
    const events = chunkToChatEvents(chunk, "fallback");
    expect(events[0].turnId).toBe("real_turn");
  });
});

describe("liveEventToChatEvent", () => {
  function ev<P>(type: string, payload: P, sequence = 1): LiveEvent {
    return {
      turn_id: TURN,
      sequence,
      timestamp_ms: 1000,
      // LiveEventType is a string union — cast to any so tests can exercise
      // the AwaitingApproval fall-through path even though it's not in the
      // current LiveEventType union.
      event_type: type as never,
      payload: payload as never,
    };
  }

  it("maps ToolStateRunning → tool-running", () => {
    const out = liveEventToChatEvent(
      ev("ToolStateRunning", {
        call_id: "c1",
        tool_name: "read_file",
        args_json: '{"path":"a.txt"}',
        started_at_ms: 1234,
      }),
    );
    expect(out).toMatchObject({
      kind: "tool-running",
      callId: "c1",
      toolName: "read_file",
    });
  });

  it("maps ToolStateCompleted with error flag", () => {
    const out = liveEventToChatEvent(
      ev("ToolStateCompleted", {
        call_id: "c1",
        result_summary: "boom",
        duration_ms: 12,
        is_error: true,
      }),
    );
    expect(out).toMatchObject({
      kind: "tool-completed",
      callId: "c1",
      isError: true,
    });
  });

  it("maps SubagentSpawned/Event/Completed", () => {
    expect(
      liveEventToChatEvent(
        ev("SubagentSpawned", { child_session_key: "k", depth: 1 }),
      )?.kind,
    ).toBe("subagent-spawned");
    expect(
      liveEventToChatEvent(
        ev("SubagentEvent", {
          child_session_key: "k",
          envelope: ev("TextDelta", { text: "x" }),
        }),
      )?.kind,
    ).toBe("subagent-event");
    expect(
      liveEventToChatEvent(
        ev("SubagentCompleted", {
          child_session_key: "k",
          finish_reason: "stop",
          tool_calls_made: 1,
          elapsed_ms: 100,
        }),
      )?.kind,
    ).toBe("subagent-completed");
  });

  it("maps AwaitingApproval (fall-through case)", () => {
    const out = liveEventToChatEvent(
      ev("AwaitingApproval", {
        call_id: "c1",
        plugin: "p",
        tool: "t",
        args_preview_json: "{}",
      }),
    );
    expect(out).toMatchObject({
      kind: "awaiting-approval",
      callId: "c1",
      plugin: "p",
      tool: "t",
    });
  });

  it("ignores noise event types", () => {
    expect(liveEventToChatEvent(ev("BlockStart", {}))).toBeNull();
    expect(liveEventToChatEvent(ev("BlockStop", {}))).toBeNull();
    expect(liveEventToChatEvent(ev("ToolStateHeartbeat", { call_id: "c", elapsed_ms: 100 }))).toBeNull();
  });
});

describe("EventDedupSet", () => {
  it("dedupes by turn_id + sequence", () => {
    const set = new EventDedupSet();
    expect(set.shouldEmit("t1", 1)).toBe(true);
    expect(set.shouldEmit("t1", 1)).toBe(false);
    expect(set.shouldEmit("t1", 2)).toBe(true);
    expect(set.shouldEmit("t2", 1)).toBe(true);
  });

  it("always emits synthetic events (sequence < 0)", () => {
    const set = new EventDedupSet();
    expect(set.shouldEmit("t1", -1)).toBe(true);
    expect(set.shouldEmit("t1", -1)).toBe(true);
  });

  it("reset clears state", () => {
    const set = new EventDedupSet();
    set.shouldEmit("t1", 1);
    set.reset();
    expect(set.shouldEmit("t1", 1)).toBe(true);
  });
});
