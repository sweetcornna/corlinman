import * as React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18next, initI18n } from "@/lib/i18n";
import { LiveAgentsPanel } from "@/components/subagents/live-agents-panel";
import type { SubagentStatusResponse } from "@/lib/api";

function row(p: Partial<SubagentStatusResponse>): SubagentStatusResponse {
  return {
    request_id: "r",
    parent_session_key: "sess",
    subagent_type: "worker",
    description: null,
    state: "running",
    started_at: 1000,
    finished_at: null,
    child_session_key: null,
    finish_reason: null,
    tool_calls_made: 0,
    elapsed_ms: 0,
    error: null,
    summary: "",
    ...p,
  };
}

function renderPanel(rows: SubagentStatusResponse[], onKill = vi.fn(), onSelect = vi.fn()) {
  return render(
    <I18nextProvider i18n={i18next}>
      <LiveAgentsPanel rows={rows} onSelect={onSelect} onKill={onKill} />
    </I18nextProvider>,
  );
}

describe("LiveAgentsPanel", () => {
  beforeEach(() => {
    initI18n();
    void i18next.changeLanguage("en");
  });
  afterEach(cleanup);

  it("renders a card per agent with the live activity line", () => {
    renderPanel([
      row({
        request_id: "sess::child::0",
        subagent_type: "researcher",
        activity: "运行工具 web_search",
        source: "inline",
        depth: 0,
      }),
    ]);
    const cards = screen.getAllByTestId("live-agent-card");
    expect(cards).toHaveLength(1);
    expect(screen.getByText("researcher")).toBeInTheDocument();
    expect(screen.getByText("运行工具 web_search")).toBeInTheDocument();
  });

  it("nests workers under their supervisor by parent_session_key", () => {
    renderPanel([
      row({ request_id: "sess::child::0", subagent_type: "supervisor", source: "inline" }),
      row({
        request_id: "sess::child::0::child::0",
        parent_session_key: "sess::child::0",
        subagent_type: "worker-web",
        source: "inline",
      }),
    ]);
    // Both render; the child is wrapped in the nesting container.
    expect(screen.getAllByTestId("live-agent-card")).toHaveLength(2);
    expect(screen.getByText("supervisor")).toBeInTheDocument();
    expect(screen.getByText("worker-web")).toBeInTheDocument();
  });

  it("hides the kill button for inline agents but shows it for background", () => {
    renderPanel([
      row({ request_id: "bg", subagent_type: "bg-worker", source: "background", state: "running" }),
      row({ request_id: "sess::child::1", subagent_type: "inline-worker", source: "inline", state: "running" }),
    ]);
    const kills = screen.getAllByTestId("live-agent-kill");
    // Exactly one kill button — the background row's.
    expect(kills).toHaveLength(1);
  });

  it("fires onSelect when a card is activated (non-expandable)", () => {
    const onSelect = vi.fn();
    renderPanel([row({ request_id: "sess::child::2", source: "inline" })], vi.fn(), onSelect);
    fireEvent.click(screen.getByTestId("live-agent-card"));
    expect(onSelect).toHaveBeenCalledWith("sess::child::2");
  });

  it("expands inline detail on click when expandable (no onSelect)", () => {
    const onSelect = vi.fn();
    render(
      <I18nextProvider i18n={i18next}>
        <LiveAgentsPanel
          rows={[
            row({
              request_id: "sess::child::3",
              source: "inline",
              state: "running",
              description: "investigate GitHub Actions",
              activity: "运行工具 web_search",
            }),
          ]}
          onSelect={onSelect}
          onKill={vi.fn()}
          expandable
        />
      </I18nextProvider>,
    );
    // Collapsed: no detail panel, and clicking does NOT navigate (onSelect).
    expect(screen.queryByTestId("live-agent-detail")).toBeNull();
    fireEvent.click(screen.getByTestId("live-agent-card"));
    expect(onSelect).not.toHaveBeenCalled();
    const detail = screen.getByTestId("live-agent-detail");
    expect(detail).toBeInTheDocument();
    expect(detail).toHaveTextContent("investigate GitHub Actions");
    expect(detail).toHaveTextContent("web_search");
    // Toggle closed again.
    fireEvent.click(screen.getByTestId("live-agent-card"));
    expect(screen.queryByTestId("live-agent-detail")).toBeNull();
  });
});
