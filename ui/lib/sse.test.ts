/**
 * Tests for `openEventStream()` (TEST-004).
 *
 * The wrapper is used by chat completions, log stream, and pending-approval
 * notifications, so the things that must be true:
 *
 *   1. On transport error the underlying EventSource is closed and a new
 *      one is opened after a backoff drawn from `[2s, 4s, 8s, 30s, 30s…]`.
 *   2. A successful message resets the backoff index — so a connection
 *      that flaps and recovers does NOT escalate to the 30s cap.
 *   3. After the returned dispose fn is called, any pending reconnect
 *      timer is cleared and no further EventSource is opened (no zombie
 *      reconnect after unmount).
 *
 * We stub `global.EventSource` with a controllable mock so the test can
 * fire `onerror` / `onmessage` synchronously and step `setTimeout` via
 * `vi.advanceTimersByTime`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { openEventStream } from "./sse";

// --- mock EventSource ------------------------------------------------------
//
// jsdom doesn't ship a real EventSource so we install our own minimal stub
// on `globalThis.EventSource`. Each `new EventSource()` instance is pushed
// onto `instances` so the test can:
//   - assert how many open attempts happened (and at what URLs)
//   - drive `onerror` / `onmessage` directly
//   - verify `.close()` was called on the prior instance

type Listener = (ev: Event) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  readonly withCredentials: boolean;

  onerror: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  readyState = 0;
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

  /** Synthesises a named-event delivery (the path `openEventStream` uses
   *  via `addEventListener`). */
  emit(name: string, data: string, lastEventId = ""): void {
    const ev = new MessageEvent(name, { data, lastEventId });
    for (const cb of this.listeners.get(name) ?? []) cb(ev);
  }

  /** Triggers `onerror`. */
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

describe("openEventStream() — connection lifecycle", () => {
  it("opens an EventSource at GATEWAY_BASE_URL + path with credentials", () => {
    const close = openEventStream("/admin/logs/stream", {
      onMessage: () => {},
    });
    try {
      expect(MockEventSource.instances).toHaveLength(1);
      const es = MockEventSource.instances[0]!;
      // GATEWAY_BASE_URL defaults to "" in the test env (no env var),
      // so the full URL is just the path.
      expect(es.url).toBe("/admin/logs/stream");
      expect(es.withCredentials).toBe(true);
    } finally {
      close();
    }
  });

  it("parses JSON message frames and forwards them to onMessage", () => {
    const onMessage = vi.fn();
    const close = openEventStream<{ kind: string }>("/x", { onMessage });
    try {
      const es = MockEventSource.instances[0]!;
      es.emit("message", JSON.stringify({ kind: "hello" }), "id-7");

      expect(onMessage).toHaveBeenCalledTimes(1);
      expect(onMessage).toHaveBeenCalledWith({
        event: "message",
        data: { kind: "hello" },
        id: "id-7",
      });
    } finally {
      close();
    }
  });

  it("falls back to the raw string when payload is not JSON", () => {
    const onMessage = vi.fn();
    const close = openEventStream("/x", { onMessage });
    try {
      MockEventSource.instances[0]!.emit("message", "not-json", "id-1");
      expect(onMessage).toHaveBeenCalledWith({
        event: "message",
        data: "not-json",
        id: "id-1",
      });
    } finally {
      close();
    }
  });

  it("subscribes to custom named events when `events` is provided", () => {
    const onMessage = vi.fn();
    const close = openEventStream("/x", {
      onMessage,
      events: ["progress", "done"],
    });
    try {
      const es = MockEventSource.instances[0]!;
      es.emit("progress", JSON.stringify({ pct: 50 }));
      es.emit("done", JSON.stringify({ ok: true }));
      // The default "message" listener should NOT be wired when caller
      // explicitly listed events.
      es.emit("message", JSON.stringify({ ignored: true }));

      expect(onMessage).toHaveBeenCalledTimes(2);
      expect(onMessage.mock.calls[0]?.[0]).toMatchObject({
        event: "progress",
        data: { pct: 50 },
      });
      expect(onMessage.mock.calls[1]?.[0]).toMatchObject({
        event: "done",
        data: { ok: true },
      });
    } finally {
      close();
    }
  });
});

describe("openEventStream() — reconnect backoff", () => {
  it("follows the 2s / 4s / 8s / 30s / 30s (cap) schedule", () => {
    const onError = vi.fn();
    const close = openEventStream("/admin/logs/stream", {
      onMessage: () => {},
      onError,
    });
    try {
      // Helper: drive the current instance into error, then advance the
      // timer by `delay` and assert that a NEW instance opened.
      const failAndAdvance = (delay: number, expectedInstanceCount: number) => {
        const current =
          MockEventSource.instances[MockEventSource.instances.length - 1]!;
        current.fail();
        expect(current.closed).toBe(true);
        // Reconnect MUST wait the full delay — nothing should fire if we
        // step one tick short.
        vi.advanceTimersByTime(delay - 1);
        expect(MockEventSource.instances).toHaveLength(
          expectedInstanceCount - 1,
        );
        vi.advanceTimersByTime(1);
        expect(MockEventSource.instances).toHaveLength(expectedInstanceCount);
      };

      // Initial connect = instance #1.
      expect(MockEventSource.instances).toHaveLength(1);

      // Backoff schedule: 2s, 4s, 8s, 30s, then 30s cap forever.
      failAndAdvance(2_000, 2);
      failAndAdvance(4_000, 3);
      failAndAdvance(8_000, 4);
      failAndAdvance(30_000, 5);
      // Past the schedule end → must clamp to 30s, not crash on
      // an out-of-bounds array index.
      failAndAdvance(30_000, 6);
      failAndAdvance(30_000, 7);

      expect(onError).toHaveBeenCalledTimes(6);
    } finally {
      close();
    }
  });

  it("resets the backoff index after a successful message", () => {
    const close = openEventStream("/x", { onMessage: () => {} });
    try {
      // Instance #1: burn through two errors so the next delay would be 8s.
      MockEventSource.instances[0]!.fail();
      vi.advanceTimersByTime(2_000);
      MockEventSource.instances[1]!.fail();
      vi.advanceTimersByTime(4_000);
      // Instance #3 exists. Deliver a message — that should reset the
      // retry index back to 0, so the NEXT failure waits only 2s, not 8s.
      const es3 = MockEventSource.instances[2]!;
      es3.emit("message", JSON.stringify({ ok: 1 }));

      es3.fail();
      // Step 1999ms — must NOT have opened #4 yet.
      vi.advanceTimersByTime(1_999);
      expect(MockEventSource.instances).toHaveLength(3);
      // Tick to 2000ms → #4 opens. (If reset hadn't happened we'd still
      // be waiting on an 8s delay.)
      vi.advanceTimersByTime(1);
      expect(MockEventSource.instances).toHaveLength(4);
    } finally {
      close();
    }
  });
});

describe("openEventStream() — dispose semantics", () => {
  it("close() clears a pending reconnect timer (no zombie reconnect)", () => {
    const close = openEventStream("/x", { onMessage: () => {} });
    // Cause an error so a 2s reconnect timer is scheduled.
    MockEventSource.instances[0]!.fail();
    expect(MockEventSource.instances[0]!.closed).toBe(true);

    // Dispose BEFORE the timer fires.
    close();

    // Advance time WAY past every scheduled delay — there must be no
    // new EventSource. If the cleanup forgot to clear the timer we'd
    // see a second instance materialise here.
    vi.advanceTimersByTime(60_000);
    expect(MockEventSource.instances).toHaveLength(1);
  });

  it("close() closes the live underlying EventSource", () => {
    const close = openEventStream("/x", { onMessage: () => {} });
    expect(MockEventSource.instances[0]!.closed).toBe(false);
    close();
    expect(MockEventSource.instances[0]!.closed).toBe(true);
  });

  it("an error that fires *after* close() does not reopen the stream", () => {
    // Race: the user navigated away (calling close()) but a transport
    // error from the same tick still made it to `onerror`. The disposed
    // flag inside the closure must short-circuit before scheduling a
    // reconnect.
    const close = openEventStream("/x", { onMessage: () => {} });
    const es = MockEventSource.instances[0]!;
    close();
    es.fail();
    vi.advanceTimersByTime(60_000);
    expect(MockEventSource.instances).toHaveLength(1);
  });
});
