/**
 * `<SubagentTree>` integration test — Phase 4 W3.2.
 *
 * Drives the real reducer with the exact envelope ordering an agent
 * emits in production:
 *
 *   1. parent BlockStart (tool_use)             — host for the child
 *   2. parent ToolStateRunning                  — tool is live
 *   3. SubagentSpawned                          — child session attaches
 *   4. SubagentEvent(BlockStart text)           — child opens a text block
 *   5. SubagentEvent(TextDelta "Hello world")   — child streams content
 *   6. SubagentEvent(BlockStop text)            — child closes the block
 *   7. SubagentCompleted                        — child wraps with summary
 *
 * Then mounts the `<TurnCard>`-equivalent (ToolWidget) for the parent
 * tool_use part and asserts:
 *   - the child agent id is rendered in the tree header
 *   - the status badge ends up "completed" (post-completed event)
 *   - the finish-reason + tool count footer renders
 *   - the child's "Hello world" text node renders inside the tree
 *
 * NOTE: completed subagent trees default-collapsed (see SubagentTree's
 * docstring — finished work shouldn't dominate the timeline), so the
 * body content (footer + child parts) lives behind the header toggle.
 * The test expands the tree before asserting on that body content,
 * mirroring how an operator drills into a finished child run.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";

import { i18next, initI18n } from "@/lib/i18n";
import {
  TimelineProvider,
  useTimeline,
  type ToolPart,
} from "@/lib/sessions/store";
import type { LiveEvent } from "@/lib/sessions/event-stream";
import { ToolWidget } from "@/components/sessions/tool-widget";
import { SubagentTree } from "@/components/sessions/subagent-tree";

function Harness({
  events,
  children,
}: {
  events: LiveEvent[];
  children: React.ReactNode;
}) {
  return (
    <I18nextProvider i18n={i18next}>
      <TimelineProvider>
        <Seed events={events} />
        {children}
      </TimelineProvider>
    </I18nextProvider>
  );
}

function Seed({ events }: { events: LiveEvent[] }) {
  const { dispatch } = useTimeline();
  React.useEffect(() => {
    dispatch({ type: "events", events });
  }, [events, dispatch]);
  return null;
}

/**
 * Pull the freshly-attached ToolPart from the store and render its
 * subagent sessions through `<ToolWidget>` so we exercise the real
 * mounting path (not a hand-rolled fixture).
 */
function ProbeToolWidget({
  turnId,
  blockId,
}: {
  turnId: string;
  blockId: string;
}) {
  const { state } = useTimeline();
  const turn = state.turns[turnId];
  if (!turn) return null;
  const part = turn.parts.find(
    (p) => p.kind === "tool_use" && p.block_id === blockId,
  ) as ToolPart | undefined;
  if (!part) return null;
  return <ToolWidget part={part} />;
}

beforeEach(() => {
  initI18n();
  void i18next.changeLanguage("en");
});

afterEach(() => {
  cleanup();
});

const T0 = 1_700_000_000_000;
const TURN_ID = "turn-sub";
const PARENT_TOOL_BLOCK = "tool-parent";
const CHILD_SESSION = "child-sess-1";
const CHILD_TEXT_BLOCK = "child-text-1";

const events: LiveEvent[] = [
  {
    turn_id: TURN_ID,
    sequence: 1,
    timestamp_ms: T0,
    event_type: "TurnStart",
    payload: {},
  },
  {
    turn_id: TURN_ID,
    sequence: 2,
    timestamp_ms: T0 + 100,
    event_type: "BlockStart",
    payload: {
      block_id: PARENT_TOOL_BLOCK,
      block_kind: "tool_use",
      tool_name: "subagent.spawn",
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 3,
    timestamp_ms: T0 + 200,
    event_type: "ToolStateRunning",
    payload: { block_id: PARENT_TOOL_BLOCK },
  },
  {
    turn_id: TURN_ID,
    sequence: 4,
    timestamp_ms: T0 + 300,
    event_type: "SubagentSpawned",
    payload: {
      parent_session_key: "parent-sess",
      child_session_key: CHILD_SESSION,
      child_agent_id: "researcher",
      depth: 1,
      prompt_preview: "Look up the W3.2 spec and summarise risks.",
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 5,
    timestamp_ms: T0 + 400,
    event_type: "SubagentEvent",
    payload: {
      child_session_key: CHILD_SESSION,
      envelope: {
        turn_id: "child-turn",
        sequence: 1,
        timestamp_ms: T0 + 400,
        event_type: "BlockStart",
        payload: { block_id: CHILD_TEXT_BLOCK, block_kind: "text" },
      },
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 6,
    timestamp_ms: T0 + 500,
    event_type: "SubagentEvent",
    payload: {
      child_session_key: CHILD_SESSION,
      envelope: {
        turn_id: "child-turn",
        sequence: 2,
        timestamp_ms: T0 + 500,
        event_type: "TextDelta",
        payload: { block_id: CHILD_TEXT_BLOCK, delta: "Hello world" },
      },
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 7,
    timestamp_ms: T0 + 600,
    event_type: "SubagentEvent",
    payload: {
      child_session_key: CHILD_SESSION,
      envelope: {
        turn_id: "child-turn",
        sequence: 3,
        timestamp_ms: T0 + 600,
        event_type: "BlockStop",
        payload: { block_id: CHILD_TEXT_BLOCK },
      },
    },
  },
  {
    turn_id: TURN_ID,
    sequence: 8,
    timestamp_ms: T0 + 700,
    event_type: "SubagentCompleted",
    payload: {
      child_session_key: CHILD_SESSION,
      finish_reason: "stop",
      tool_calls_made: 2,
      elapsed_ms: 400,
      summary: "Researched the spec successfully.",
    },
  },
];

describe("SubagentTree", () => {
  it("renders a nested subagent timeline under the spawning tool", () => {
    render(
      <Harness events={events}>
        <ProbeToolWidget turnId={TURN_ID} blockId={PARENT_TOOL_BLOCK} />
      </Harness>,
    );

    // The tree itself is mounted under the parent tool widget.
    const tree = screen.getByTestId("subagent-tree");
    expect(tree).toBeInTheDocument();
    expect(tree).toHaveAttribute("data-status", "complete");
    expect(tree).toHaveAttribute("data-depth", "1");

    // Header surfaces the child agent id + status badge — visible
    // whether the tree is expanded or collapsed.
    expect(screen.getByTestId("subagent-agent-id")).toHaveTextContent(
      "researcher",
    );
    const badge = screen.getByTestId("subagent-status-badge");
    expect(badge).toHaveTextContent(/completed/i);

    // A finished child run starts collapsed (its body shouldn't dominate
    // the timeline). Expand it via the header toggle to read the footer +
    // bubbled child parts.
    fireEvent.click(within(tree).getByRole("button"));

    // Footer reflects the completion payload.
    const footer = screen.getByTestId("subagent-footer");
    expect(footer).toHaveTextContent("stop");
    // tool_calls_made surfaces as "2 tools".
    expect(footer).toHaveTextContent(/2/);

    // The bubbled child text part is rendered inside the tree.
    expect(screen.getByText("Hello world")).toBeInTheDocument();
  });

  it("renders a standalone session fixture via the direct prop API", () => {
    // Verifies `<SubagentTree>` works as a pure presentational component
    // outside the reducer-driven path (useful for storybook / replay).
    const session = {
      kind: "subagent_session" as const,
      child_session_key: "manual-sess",
      child_agent_id: "manual-agent",
      prompt_preview: "manual prompt",
      depth: 2,
      status: "errored" as const,
      finish_reason: "error",
      tool_calls_made: 0,
      elapsed_ms: 12,
      summary: "boom",
      parts: [],
    };
    render(
      <I18nextProvider i18n={i18next}>
        <SubagentTree session={session} />
      </I18nextProvider>,
    );
    expect(screen.getByTestId("subagent-agent-id")).toHaveTextContent(
      "manual-agent",
    );
    expect(screen.getByTestId("subagent-status-badge")).toHaveTextContent(
      /errored/i,
    );
  });
});
