/**
 * tool-widget smoke tests — Phase 4 W2.1.
 *
 * Two cases:
 *   1. Running tool renders the tool name + the "running" badge.
 *   2. Completed tool renders the "done" badge and (when expanded) the
 *      output via the generic renderer.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";

import { i18next, initI18n } from "@/lib/i18n";
import { ToolWidget } from "@/components/sessions/tool-widget";
import type { ToolPart } from "@/lib/sessions/store";

function Harness({ children }: { children: React.ReactNode }) {
  return <I18nextProvider i18n={i18next}>{children}</I18nextProvider>;
}

beforeEach(() => {
  initI18n();
  void i18next.changeLanguage("en");
});

afterEach(() => {
  cleanup();
});

describe("ToolWidget", () => {
  it("renders running state with the tool name and badge", () => {
    const part: ToolPart = {
      kind: "tool_use",
      block_id: "b1",
      tool_name: "Bash",
      input_json: '{"command":"ls -la"}',
      state: { kind: "running", startedAt: Date.now() - 1500 },
    };
    render(
      <Harness>
        <ToolWidget part={part} />
      </Harness>,
    );
    expect(screen.getByText("Bash")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it("renders completed state with the done badge", () => {
    const part: ToolPart = {
      kind: "tool_use",
      block_id: "b2",
      tool_name: "Read",
      input_json: '{"file_path":"/tmp/x.txt"}',
      state: {
        kind: "completed",
        startedAt: Date.now() - 2000,
        completedAt: Date.now(),
        isError: false,
        output: "hello",
      },
    };
    render(
      <Harness>
        <ToolWidget part={part} />
      </Harness>,
    );
    expect(screen.getByText("Read")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
  });
});
