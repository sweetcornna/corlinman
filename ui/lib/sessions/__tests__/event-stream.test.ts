/**
 * Tests for `openLiveEventStream()` (TEST-005).
 *
 * The wrapper is consumed by 6 files (event-timeline, message-bubble,
 * subagent-detail-drawer, store.ts, api.ts re-export) and is the
 * transport for the live timeline. The behaviours that matter:
 *
 *   1. URL composition: encodes the session key and appends
 *      `?last_event_id=…` only when we have a prior id (no naked `?`).
 *   2. Resume: after a frame is delivered the most recent id (either the
 *      transport `lastEventId` or the composite `turn_id:sequence`) is
 *      attached to the *next* reconnect URL.
 *   3. Frame parsing: valid JSON is forwarded to `onEvent`; malformed
 *      frames are dropped silently (no throw — the gateway occasionally
 *      sends heartbeats and proxies have been known to mangle bodies).
 *   4. Reconnect backoff: 3s / 6s / 12s / 30s, then 30s cap, with
 *      retry index reset on a successful frame.
 *   5. close() cancels any pending reconnect timer.
 *
 * Uses the same mock-EventSource + fake-timers harness as `sse.test.ts`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { openLiveEventStream, type LiveEvent } from "../event-stream";

// --- mock EventSource ------------------------------------------------------

type Listener = (ev: Event) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  readonly withCredentials: boolean;

  onerror: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  closed = false;

  private readonly listeners = new Map<string, Listener[]>();

  constructor(url: string, init?: EventSourceInit) {
    this.url = url;
    this.withCredentials = !!init?.withCredentials;
    MockEventSource.instances.push(this);
  }

  addEventListener(name: string, cb: Listener): void {
    const arr = this.listeners.get(name) ?? [];
    arr.push(cb);
    this.listeners.set(name, arr);
  }

  removeEventListener(name: string, cb: Listener): void {
    const arr = this.listeners.get(name);
    if (!arr) return;
    this.listeners.set(
      name,
      arr.filter((fn) => fn !== cb),
    );
  }

  close(): void {
    this.closed = true;
  }

  /** Calls `onmessage` (the live-stream client doesn't use named
   *  listeners — it sets `es.onmessage` directly). */
  message(data: string, lastEventId = ""): void {
    this.onmessage?.(new MessageEvent("message", { data, lastEventId }));
  }

  fail(): void {
    this.onerror?.(new Event("error"));
  }
}

const realEventSource = (globalThis as { EventSource?: unknown }).EventSource;

beforeEach(() => {
  MockEventSource.instances = [];
  (globalThis as { EventSource: unknown }).EventSource = MockEventSource;
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  if (realEventSource === undefined) {
    delete (globalThis as { EventSource?: unknown }).EventSource;
  } else {
    (globalThis as { EventSource: unknown }).EventSource = realEventSource;
  }
});

// --- helpers ---------------------------------------------------------------

function liveEvent(
  turn_id: string,
  sequence: number,
  payload: unknown = {},
): LiveEvent {
  return {
    turn_id,
    sequence,
    timestamp_ms: 1,
    event_type: "TextDelta",
    payload,
  };
}

// --------------------------------------------------------------------------

describe("openLiveEventStream() — URL composition", () => {
  it("opens the live endpoint for the encoded session key on first connect", () => {
    const close = openLiveEventStream("sess/with spaces", { onEvent: () => {} });
    try {
      expect(MockEventSource.instances).toHaveLength(1);
      const es = MockEventSource.instances[0]!;
      // Path: /admin/sessions/{encoded}/events/live with NO query string
      // because no `initialLastEventId` and no prior frame.
      expect(es.url).toBe(
        "/admin/sessions/sess%2Fwith%20spaces/events/live",
      );
      expect(es.withCredentials).toBe(true);
    } finally {
      close();
    }
  });

  it("appends ?last_event_id=… when `initialLastEventId` is provided", () => {
    const close = openLiveEventStream("s1", {
      onEvent: () => {},
      initialLastEventId: "t1:7",
    });
    try {
      const es = MockEventSource.instances[0]!;
      expect(es.url).toBe(
        "/admin/sessions/s1/events/live?last_event_id=t1%3A7",
      );
    } finally {
      close();
    }
  });

  it("URL-encodes ids that contain reserved characters", () => {
    const close = openLiveEventStream("s1", {
      onEvent: () => {},
      initialLastEventId: "turn-α&β:42",
    });
    try {
      const url = MockEventSource.instances[0]!.url;
      // URLSearchParams encodes both `&` (would otherwise terminate the
      // pair) and `:` (reserved per RFC 3986 query subdelims).
      expect(url).toContain("last_event_id=");
      expect(url).not.toMatch(/last_event_id=turn-α&β/);
      expect(url).toContain("%26"); // encoded &
      expect(url).toContain("%3A"); // encoded :
    } finally {
      close();
    }
  });
});

describe("openLiveEventStream() — frame handling", () => {
  it("parses a well-formed frame and forwards it to onEvent", () => {
    const onEvent = vi.fn();
    const close = openLiveEventStream("s1", { onEvent });
    try {
      const frame = liveEvent("t1", 1, { delta: "hi" });
      MockEventSource.instances[0]!.message(JSON.stringify(frame));

      expect(onEvent).toHaveBeenCalledTimes(1);
      expect(onEvent).toHaveBeenCalledWith(frame);
    } finally {
      close();
    }
  });

  it("silently drops malformed JSON frames (does not throw, does not call onEvent)", () => {
    const onEvent = vi.fn();
    const close = openLiveEventStream("s1", { onEvent });
    try {
      const es = MockEventSource.instances[0]!;
      // Should not raise.
      expect(() => es.message("not-json")).not.toThrow();
      expect(() => es.message("{partial:")).not.toThrow();
      expect(onEvent).not.toHaveBeenCalled();
    } finally {
      close();
    }
  });

  it("prefers the transport `lastEventId` over the composite when reconnecting", () => {
    const close = openLiveEventStream("s1", { onEvent: () => {} });
    try {
      const es1 = MockEventSource.instances[0]!;
      // Transport-supplied id wins.
      es1.message(JSON.stringify(liveEvent("t1", 9)), "transport-id-42");

      // Force a reconnect.
      es1.fail();
      vi.advanceTimersByTime(3_000);

      const es2 = MockEventSource.instances[1]!;
      expect(es2.url).toBe(
        "/admin/sessions/s1/events/live?last_event_id=transport-id-42",
      );
    } finally {
      close();
    }
  });

  it("falls back to `${turn_id}:${sequence}` when the transport id is empty", () => {
    const close = openLiveEventStream("s1", { onEvent: () => {} });
    try {
      const es1 = MockEventSource.instances[0]!;
      // Empty string lastEventId → fall back to composite.
      es1.message(JSON.stringify(liveEvent("turn-7", 12)), "");

      es1.fail();
      vi.advanceTimersByTime(3_000);

      const es2 = MockEventSource.instances[1]!;
      expect(es2.url).toBe(
        "/admin/sessions/s1/events/live?last_event_id=turn-7%3A12",
      );
    } finally {
      close();
    }
  });
});

describe("openLiveEventStream() — reconnect backoff", () => {
  it("follows the 3s / 6s / 12s / 30s / 30s (cap) schedule", () => {
    const onError = vi.fn();
    const close = openLiveEventStream("s1", { onEvent: () => {}, onError });
    try {
      const failAndAdvance = (delay: number, expectedInstanceCount: number) => {
        const current =
          MockEventSource.instances[MockEventSource.instances.length - 1]!;
        current.fail();
        expect(current.closed).toBe(true);
        vi.advanceTimersByTime(delay - 1);
        expect(MockEventSource.instances).toHaveLength(
          expectedInstanceCount - 1,
        );
        vi.advanceTimersByTime(1);
        expect(MockEventSource.instances).toHaveLength(expectedInstanceCount);
      };

      expect(MockEventSource.instances).toHaveLength(1);

      failAndAdvance(3_000, 2);
      failAndAdvance(6_000, 3);
      failAndAdvance(12_000, 4);
      failAndAdvance(30_000, 5);
      // Beyond the schedule → clamp to 30s, not crash.
      failAndAdvance(30_000, 6);

      expect(onError).toHaveBeenCalledTimes(5);
    } finally {
      close();
    }
  });

  it("resets the retry index after a successful frame", () => {
    const close = openLiveEventStream("s1", { onEvent: () => {} });
    try {
      // Burn the first two slots of the schedule.
      MockEventSource.instances[0]!.fail();
      vi.advanceTimersByTime(3_000);
      MockEventSource.instances[1]!.fail();
      vi.advanceTimersByTime(6_000);

      const es3 = MockEventSource.instances[2]!;
      es3.message(JSON.stringify(liveEvent("t1", 1)));

      // Without reset the next delay would be 12s; with reset it's 3s.
      es3.fail();
      vi.advanceTimersByTime(2_999);
      expect(MockEventSource.instances).toHaveLength(3);
      vi.advanceTimersByTime(1);
      expect(MockEventSource.instances).toHaveLength(4);
    } finally {
      close();
    }
  });
});

describe("openLiveEventStream() — dispose semantics", () => {
  it("close() clears the pending reconnect timer", () => {
    const close = openLiveEventStream("s1", { onEvent: () => {} });
    MockEventSource.instances[0]!.fail();
    close();
    vi.advanceTimersByTime(60_000);
    expect(MockEventSource.instances).toHaveLength(1);
  });

  it("close() closes the live underlying EventSource", () => {
    const close = openLiveEventStream("s1", { onEvent: () => {} });
    expect(MockEventSource.instances[0]!.closed).toBe(false);
    close();
    expect(MockEventSource.instances[0]!.closed).toBe(true);
  });

  it("an error that fires *after* close() does not reopen the stream", () => {
    const close = openLiveEventStream("s1", { onEvent: () => {} });
    const es = MockEventSource.instances[0]!;
    close();
    es.fail();
    vi.advanceTimersByTime(60_000);
    expect(MockEventSource.instances).toHaveLength(1);
  });
});
