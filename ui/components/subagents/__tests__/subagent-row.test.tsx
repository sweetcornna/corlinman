/**
 * `<SubagentRow>` smoke tests — W2.2.
 *
 * Two scenarios:
 *   1. A `running` row renders the elapsed counter, the live state
 *      pill, and a clickable Kill button.
 *   2. A `succeeded` row hides the Kill button (the action cell shows
 *      a dash placeholder instead) and still renders an elapsed value.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";

import { i18next, initI18n } from "@/lib/i18n";
import { SubagentRow } from "@/components/subagents/subagent-row";
import type { SubagentStatusResponse } from "@/lib/api";

function Harness({ children }: { children: React.ReactNode }) {
  return (
    <I18nextProvider i18n={i18next}>
      <table>
        <tbody>{children}</tbody>
      </table>
    </I18nextProvider>
  );
}

beforeEach(() => {
  initI18n();
  void i18next.changeLanguage("en");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function makeRow(
  overrides: Partial<SubagentStatusResponse>,
): SubagentStatusResponse {
  return {
    request_id: "req_running_001",
    parent_session_key: "sess_abcdef0123456789",
    subagent_type: "research",
    description: "Investigate the new API surface",
    state: "running",
    started_at: Date.now() - 14_000,
    finished_at: null,
    child_session_key: "child_xyz_001",
    finish_reason: null,
    tool_calls_made: 6,
    elapsed_ms: 14_000,
    error: null,
    summary: "",
    ...overrides,
  };
}

describe("SubagentRow", () => {
  it("renders a running row with elapsed counter and an enabled Kill button", () => {
    const onSelect = vi.fn();
    const onKill = vi.fn();
    const row = makeRow({});
    // window.confirm → always-true so onKill fires.
    const confirmSpy = vi
      .spyOn(window, "confirm")
      .mockReturnValue(true);

    render(
      <Harness>
        <SubagentRow data={row} onSelect={onSelect} onKill={onKill} />
      </Harness>,
    );

    // Elapsed cell is mounted and non-empty (14s on the dot, but the
    // computation reads Date.now() at render time so we just assert
    // non-empty + numeric prefix).
    const elapsed = screen.getByTestId("subagent-elapsed");
    expect(elapsed.textContent ?? "").toMatch(/\d+s$/);

    // State pill renders the "Running" i18n string.
    expect(screen.getByTestId("subagent-state-pill")).toHaveTextContent(
      /running/i,
    );

    // Kill button is enabled and triggers the callback through the
    // window.confirm pre-check.
    const kill = screen.getByTestId("subagent-kill-button");
    expect(kill).not.toBeDisabled();
    fireEvent.click(kill);
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(onKill).toHaveBeenCalledWith("req_running_001");
    // Clicking the kill button must NOT bubble into the row's
    // onSelect handler — that would open the drawer mid-confirm.
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("hides the Kill button on a succeeded row", () => {
    const onSelect = vi.fn();
    const onKill = vi.fn();
    const row = makeRow({
      request_id: "req_done_001",
      state: "succeeded",
      started_at: Date.now() - 28_000,
      finished_at: Date.now(),
      elapsed_ms: 28_000,
      finish_reason: "stop",
      summary: "Wrote 3 paragraphs and stopped.",
    });

    render(
      <Harness>
        <SubagentRow data={row} onSelect={onSelect} onKill={onKill} />
      </Harness>,
    );

    expect(screen.queryByTestId("subagent-kill-button")).toBeNull();
    expect(screen.getByTestId("subagent-state-pill")).toHaveTextContent(
      /done|succeed/i,
    );
    // Elapsed counter still renders for terminal rows.
    expect(screen.getByTestId("subagent-elapsed").textContent ?? "").toMatch(
      /\d+/,
    );
  });
});
