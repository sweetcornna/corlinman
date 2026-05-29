/**
 * Perf regression: PERF-012 — opening the chat model picker must NOT fan out
 * one `getProviderModels` HTTP probe per enabled provider all at once.
 *
 * The old picker built a `useQueries` over EVERY enabled provider with
 * `enabled: open`, so the moment the popover opened it fired N concurrent
 * probes (one round-trip per provider) in a single burst. With a dozen
 * providers configured that's a dozen simultaneous upstream catalog calls on
 * every open.
 *
 * Root-cause fix: probe sequentially (one in-flight at a time) — provider i's
 * query is gated until provider i-1 has settled. The flat option list still
 * fills in for every provider, just not in a single concurrent burst.
 *
 * This test mocks `getProviderModels` with controllable deferreds, counts the
 * MAX concurrent in-flight probes after open, and asserts it never exceeds 1
 * while still eventually probing every provider as each one resolves.
 */
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nextProvider } from "react-i18next";

import { initI18n } from "@/lib/i18n";

// --- module mock: count concurrency on getProviderModels -------------------
type Deferred = {
  resolve: (v: { models: { id: string }[] }) => void;
  promise: Promise<{ models: { id: string }[] }>;
};
function makeDeferred(): Deferred {
  let resolve!: (v: { models: { id: string }[] }) => void;
  const promise = new Promise<{ models: { id: string }[] }>((r) => {
    resolve = r;
  });
  return { resolve, promise };
}

// Per-call bookkeeping.
const probeCalls: string[] = [];
const pending = new Map<string, Deferred>();
let inFlight = 0;
let maxConcurrent = 0;

const PROVIDERS = [
  { name: "openai", kind: "openai", enabled: true, params: {} },
  { name: "anthropic", kind: "anthropic", enabled: true, params: {} },
  { name: "groq", kind: "openai_compatible", enabled: true, params: {} },
  { name: "mistral", kind: "openai_compatible", enabled: true, params: {} },
];

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchModels: vi.fn(async () => ({ default: "", aliases: {} })),
    fetchProviders: vi.fn(async () => PROVIDERS),
    getProviderModels: vi.fn((name: string) => {
      probeCalls.push(name);
      inFlight += 1;
      maxConcurrent = Math.max(maxConcurrent, inFlight);
      const d = makeDeferred();
      pending.set(name, d);
      return d.promise.finally(() => {
        inFlight -= 1;
      });
    }),
  };
});

import { ChatModelPicker } from "@/components/chat/chat-model-picker";

const i18n = initI18n();

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <I18nextProvider i18n={i18n}>{ui}</I18nextProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  probeCalls.length = 0;
  pending.clear();
  inFlight = 0;
  maxConcurrent = 0;
});

afterEach(() => {
  // Drain any outstanding deferreds.
  for (const d of pending.values()) d.resolve({ models: [] });
  pending.clear();
});

describe("ChatModelPicker — probe fan-out (PERF-012)", () => {
  it("does not schedule N concurrent provider probes on open", async () => {
    render(
      wrap(
        <ChatModelPicker
          open
          onClose={() => {}}
          kind="llm"
          current="gpt-5"
          onPick={() => {}}
        />,
      ),
    );

    // The picker is open; providers resolve and probing starts. Wait until
    // at least one probe has been issued.
    await waitFor(() => {
      expect(probeCalls.length).toBeGreaterThanOrEqual(1);
    });

    // CRITICAL: at no point may more than one probe be in flight at once.
    // (Old behavior: all 4 fire immediately → maxConcurrent === 4.)
    expect(maxConcurrent).toBeLessThanOrEqual(1);
    expect(inFlight).toBeLessThanOrEqual(1);

    // Resolve probes one at a time; each resolution should let the next
    // provider's probe start — proving every provider is still covered.
    for (let i = 0; i < PROVIDERS.length; i++) {
      // The i-th provider in order should be the one currently pending.
      await waitFor(() => {
        expect(probeCalls.length).toBeGreaterThanOrEqual(i + 1);
      });
      const name = probeCalls[i];
      const d = pending.get(name);
      expect(d, `probe ${name} should be pending`).toBeTruthy();
      await act(async () => {
        d!.resolve({ models: [{ id: `${name}-model` }] });
        await Promise.resolve();
      });
      // Still never more than one concurrent.
      expect(maxConcurrent).toBeLessThanOrEqual(1);
    }

    // Every enabled provider eventually got probed exactly once.
    await waitFor(() => {
      expect(new Set(probeCalls)).toEqual(
        new Set(PROVIDERS.map((p) => p.name)),
      );
    });
    expect(probeCalls).toHaveLength(PROVIDERS.length);
  });

  it("still renders probed models for the providers as they resolve", async () => {
    render(
      wrap(
        <ChatModelPicker
          open
          onClose={() => {}}
          kind="llm"
          current="gpt-5"
          onPick={() => {}}
        />,
      ),
    );

    await waitFor(() => expect(probeCalls.length).toBeGreaterThanOrEqual(1));
    // Resolve every provider in order.
    for (let i = 0; i < PROVIDERS.length; i++) {
      await waitFor(() =>
        expect(probeCalls.length).toBeGreaterThanOrEqual(i + 1),
      );
      const name = probeCalls[i];
      await act(async () => {
        pending.get(name)!.resolve({ models: [{ id: `${name}-model` }] });
        await Promise.resolve();
      });
    }

    // The flat list must surface at least the first provider's probed model.
    await waitFor(() => {
      expect(screen.getByText("openai-model")).toBeInTheDocument();
    });
  });
});
