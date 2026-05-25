/**
 * timelineReducer smoke tests — Phase 4 W2.1.
 *
 *   1. TurnStart → BlockStart(text) → TextDelta → BlockStop → TurnComplete
 *      yields one turn with one done text part and status=complete.
 *   2. A duplicate-sequence event is ignored (no double text accumulation).
 */

import { describe, expect, it } from "vitest";

import {
  initialTimelineState,
  timelineReducer,
  type TimelineAction,
} from "@/lib/sessions/store";
import type { LiveEvent } from "@/lib/sessions/event-stream";

function ev(
  partial: Omit<LiveEvent, "timestamp_ms"> & { timestamp_ms?: number },
): LiveEvent {
  return { timestamp_ms: 1, ...partial } as LiveEvent;
}

describe("timelineReducer", () => {
  it("assembles a text turn from streamed deltas", () => {
    const events: LiveEvent[] = [
      ev({ turn_id: "t1", sequence: 1, event_type: "TurnStart", payload: {} }),
      ev({
        turn_id: "t1",
        sequence: 2,
        event_type: "BlockStart",
        payload: { block_id: "b1", block_kind: "text" },
      }),
      ev({
        turn_id: "t1",
        sequence: 3,
        event_type: "TextDelta",
        payload: { block_id: "b1", delta: "hello " },
      }),
      ev({
        turn_id: "t1",
        sequence: 4,
        event_type: "TextDelta",
        payload: { block_id: "b1", delta: "world" },
      }),
      ev({
        turn_id: "t1",
        sequence: 5,
        event_type: "BlockStop",
        payload: { block_id: "b1" },
      }),
      ev({
        turn_id: "t1",
        sequence: 6,
        event_type: "TurnComplete",
        payload: { cost_usd: 0.0012 },
      }),
    ];
    const action: TimelineAction = { type: "events", events };
    const state = timelineReducer(initialTimelineState, action);

    expect(state.turnOrder).toEqual(["t1"]);
    const turn = state.turns["t1"]!;
    expect(turn.status).toBe("complete");
    expect(turn.parts).toHaveLength(1);
    const [part] = turn.parts;
    expect(part).toMatchObject({ kind: "text", text: "hello world", done: true });
    expect(turn.costUsd).toBe(0.0012);
  });

  it("ignores stale duplicate-sequence events", () => {
    const setup: LiveEvent[] = [
      ev({ turn_id: "t1", sequence: 1, event_type: "TurnStart", payload: {} }),
      ev({
        turn_id: "t1",
        sequence: 2,
        event_type: "BlockStart",
        payload: { block_id: "b1", block_kind: "text" },
      }),
      ev({
        turn_id: "t1",
        sequence: 3,
        event_type: "TextDelta",
        payload: { block_id: "b1", delta: "abc" },
      }),
    ];
    let state = timelineReducer(initialTimelineState, { type: "events", events: setup });
    // Replay sequence=3 — should be a no-op (stale).
    state = timelineReducer(state, {
      type: "events",
      events: [
        ev({
          turn_id: "t1",
          sequence: 3,
          event_type: "TextDelta",
          payload: { block_id: "b1", delta: "abc" },
        }),
      ],
    });
    const turn = state.turns["t1"]!;
    expect(turn.parts).toHaveLength(1);
    expect((turn.parts[0] as { text: string }).text).toBe("abc");
  });
});
