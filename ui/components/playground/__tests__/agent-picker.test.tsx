/**
 * W2.3 smoke for <AgentPicker>.
 *
 * Validates:
 *   1. Default state shows "Auto-route"; clicking an agent item fires
 *      ``onChange`` with that agent's name and closes the popover.
 *   2. The search input filters the list by name (and description).
 *
 * The component fetches via `listAgents()` (→ ``GET /admin/agents``)
 * through the shared `apiFetch` wrapper; we stub `fetch` globally so
 * the wrapper resolves with a deterministic agent list. Because
 * `apiFetch` reads `res.headers.get` and `res.status` in addition to
 * `res.ok` + `res.json()`, the stub surfaces all four.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nextProvider } from "react-i18next";

import { AgentPicker } from "../agent-picker";
import { initI18n } from "@/lib/i18n";

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

const AGENTS = [
  {
    name: "researcher",
    file_path: "/data/agents/researcher.yaml",
    bytes: 200,
    last_modified: "2026-05-25T00:00:00Z",
    source: "built-in" as const,
    description: "Finds papers and surfaces citations.",
  },
  {
    name: "editor",
    file_path: "/data/agents/editor.yaml",
    bytes: 180,
    last_modified: "2026-05-25T00:00:00Z",
    source: "user" as const,
    description: "Tightens prose and fixes typos.",
  },
];

function respond(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.includes("/admin/agents")) {
        return respond(AGENTS);
      }
      return {
        ok: false,
        status: 404,
        headers: { get: () => null },
        json: async () => ({}),
        text: async () => "",
      } as unknown as Response;
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("<AgentPicker>", () => {
  it("default state shows auto-route; picking an agent fires onChange with the name", async () => {
    const onChange = vi.fn();
    render(wrap(<AgentPicker value={null} onChange={onChange} />));

    // Default trigger label is the auto-route key (when the locale
    // doesn't define the key yet, react-i18next returns the key — that
    // still satisfies the "shows auto-route" contract because the key
    // string itself reads ``playground.agentPicker.triggerAuto``).
    const trigger = screen.getByTestId("agent-picker-trigger");
    expect(trigger.textContent ?? "").toMatch(/auto|triggerAuto/i);

    // Open the popover.
    await act(async () => {
      fireEvent.click(trigger);
    });

    // Wait for the agent list to resolve and render.
    await waitFor(() => {
      expect(
        screen.queryByTestId("agent-picker-item-researcher"),
      ).toBeInTheDocument();
      expect(
        screen.queryByTestId("agent-picker-item-editor"),
      ).toBeInTheDocument();
    });

    // Click an agent.
    await act(async () => {
      fireEvent.click(screen.getByTestId("agent-picker-item-researcher"));
    });

    expect(onChange).toHaveBeenCalledWith("researcher");
    // Popover closes on pick.
    expect(screen.queryByTestId("agent-picker-popover")).toBeNull();
  });

  it("search input filters the list by name", async () => {
    render(wrap(<AgentPicker value={null} onChange={vi.fn()} />));

    await act(async () => {
      fireEvent.click(screen.getByTestId("agent-picker-trigger"));
    });

    await waitFor(() => {
      expect(
        screen.queryByTestId("agent-picker-item-researcher"),
      ).toBeInTheDocument();
    });

    // Type a query that only matches one agent.
    const search = screen.getByTestId("agent-picker-search");
    await act(async () => {
      fireEvent.change(search, { target: { value: "edit" } });
    });

    await waitFor(() => {
      expect(screen.queryByTestId("agent-picker-item-editor")).toBeInTheDocument();
      expect(
        screen.queryByTestId("agent-picker-item-researcher"),
      ).toBeNull();
    });
  });
});
