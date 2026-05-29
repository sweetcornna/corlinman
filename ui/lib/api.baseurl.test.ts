/**
 * Gateway-base-URL prefix test for `streamUpgradeEvents` (G11).
 *
 * The upgrade-progress SSE factory builds an `EventSource` URL directly. When
 * the UI is served from a different origin than the gateway, the deployer sets
 * `NEXT_PUBLIC_GATEWAY_URL`; this URL MUST be prefixed the same way the sibling
 * SSE factories (`streamSubagentEvents`, `streamHubInstallEvents`) do —
 * otherwise the stream points at the UI origin and never connects.
 *
 * `GATEWAY_BASE_URL` is captured from `process.env` at module-load time, so we
 * stub the env var and re-import the module fresh.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const GATEWAY = "https://gw.example.test";

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly url: string;
  readonly withCredentials: boolean;
  closed = false;
  constructor(url: string, init?: EventSourceInit) {
    this.url = url;
    this.withCredentials = !!init?.withCredentials;
    MockEventSource.instances.push(this);
  }
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): void {
    this.closed = true;
  }
}

const realEventSource = (globalThis as { EventSource?: unknown }).EventSource;

beforeEach(() => {
  MockEventSource.instances = [];
  (globalThis as { EventSource: unknown }).EventSource = MockEventSource;
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_GATEWAY_URL", GATEWAY);
});

afterEach(() => {
  vi.unstubAllEnvs();
  if (realEventSource === undefined) {
    delete (globalThis as { EventSource?: unknown }).EventSource;
  } else {
    (globalThis as { EventSource: unknown }).EventSource = realEventSource;
  }
});

describe("streamUpgradeEvents — GATEWAY_BASE_URL prefix", () => {
  it("opens the EventSource at GATEWAY_BASE_URL + path with credentials", async () => {
    const { streamUpgradeEvents } = await import("@/lib/api");

    const es = streamUpgradeEvents("req-abc");

    expect(MockEventSource.instances).toHaveLength(1);
    const inst = MockEventSource.instances[0]!;
    expect(inst.url).toBe(`${GATEWAY}/admin/system/upgrade/req-abc/events`);
    expect(inst.url.startsWith(GATEWAY)).toBe(true);
    expect(inst.withCredentials).toBe(true);
    (es as unknown as MockEventSource).close();
  });

  it("keeps the prefix when a last_event_id query is appended", async () => {
    const { streamUpgradeEvents } = await import("@/lib/api");

    streamUpgradeEvents("req-abc", { lastEventId: "42" });

    const inst = MockEventSource.instances[0]!;
    expect(inst.url).toBe(
      `${GATEWAY}/admin/system/upgrade/req-abc/events?last_event_id=42`,
    );
    expect(inst.url.startsWith(GATEWAY)).toBe(true);
  });
});
