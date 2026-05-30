import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";

import { i18next, initI18n } from "@/lib/i18n";
import { StatusCardClient } from "./status-card-client";

vi.mock("next/navigation", () => ({
  usePathname: () => "/status/share-token-123",
  useSearchParams: () => new URLSearchParams(),
}));

const statusBody = {
  session_key: "sess-public",
  status: "in_progress",
  started_at_ms: 1_700_000_000_000,
  last_activity_at_ms: 1_700_000_000_250,
  current_step: {
    kind: "tool",
    turn_id: "turn-1",
    call_id: "call-search",
    name: "search_docs",
    event_type: "ToolStateRunning",
  },
  turns: [
    {
      turn_id: "turn-1",
      status: "in_progress",
      started_at_ms: 1_700_000_000_000,
      ended_at_ms: null,
      tool_call_count: 1,
      elapsed_ms: null,
      user_text_preview: "build a public status page",
    },
  ],
};

const eventsBody = {
  session_key: "sess-public",
  events: [
    {
      turn_id: "turn-1",
      session_key: "sess-public",
      sequence: 0,
      timestamp_ms: 1_700_000_000_000,
      event_type: "BlockStart",
      payload: {
        index: 0,
        block_type: "tool_use",
        tool_name: "search_docs",
        tool_call_id: "call-search",
      },
    },
    {
      turn_id: "turn-1",
      session_key: "sess-public",
      sequence: 1,
      timestamp_ms: 1_700_000_000_010,
      event_type: "ToolInputDelta",
      payload: {
        index: 0,
        partial_json: '{"query":"status card"}',
      },
    },
    {
      turn_id: "turn-1",
      session_key: "sess-public",
      sequence: 2,
      timestamp_ms: 1_700_000_000_020,
      event_type: "ToolStateRunning",
      payload: {
        tool_call_id: "call-search",
        tool_name: "search_docs",
        args_json: '{"query":"status card"}',
        started_at_ms: 1_700_000_000_020,
      },
    },
    {
      turn_id: "turn-1",
      session_key: "sess-public",
      sequence: 3,
      timestamp_ms: 1_700_000_000_050,
      event_type: "SubagentSpawned",
      payload: {
        parent_session_key: "sess-public",
        child_session_key: "child-1",
        child_agent_id: "researcher",
        depth: 1,
        prompt_preview: "inspect status UI reuse",
      },
    },
  ],
  next_cursor: null,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function Harness({ children }: { children: React.ReactNode }) {
  return <I18nextProvider i18n={i18next}>{children}</I18nextProvider>;
}

describe("public status page", () => {
  beforeEach(() => {
    initI18n();
    void i18next.changeLanguage("en");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL | Request) => {
        const raw = typeof url === "string" ? url : url.toString();
        if (raw.includes("/status/share-token-123/events")) {
          return jsonResponse(eventsBody);
        }
        if (raw.includes("/status/share-token-123?format=json")) {
          return jsonResponse(statusBody);
        }
        return jsonResponse({ error: "unexpected path", raw }, 404);
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("loads token-scoped status and renders a read-only trajectory", async () => {
    render(
      <Harness>
        <StatusCardClient initialToken="share-token-123" />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("public-status-page")).toBeInTheDocument();
    });

    expect(screen.getByText("sess-public")).toBeInTheDocument();
    expect(screen.getByText(/in progress/i)).toBeInTheDocument();
    expect(screen.getAllByText("search_docs").length).toBeGreaterThan(0);
    expect(screen.getByTestId("event-timeline-body")).toHaveAttribute(
      "data-mode",
      "replay",
    );
    const toolWidget = await screen.findByTestId("tool-widget");
    expect(toolWidget).toHaveAttribute(
      "data-tool-name",
      "search_docs",
    );
    expect(await screen.findByTestId("subagent-tree")).toHaveTextContent("researcher");
    expect(screen.getByTestId("public-status-work-cards")).toBeInTheDocument();
    expect(screen.getByTestId("tool-call-card")).toHaveAttribute(
      "data-tool-name",
      "search_docs",
    );
    expect(screen.getByTestId("subagent-card")).toHaveTextContent("researcher");

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map(
      (call) => String(call[0]),
    );
    expect(calls).toContain("/status/share-token-123?format=json");
    expect(calls).toContain("/status/share-token-123/events");

    expect(screen.queryByRole("button", { name: /kill/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /admin/i })).not.toBeInTheDocument();
  });
});
