/**
 * `<TurnSummaryCard>` tests — Phase 4 W2.2.
 *
 * Two fixture turns:
 *   1. completed — emerald badge, cost + tokens + finish reason present.
 *   2. errored   — red badge, error message rendered, fields that are
 *                  missing fall back to em-dash (never zero).
 *
 * The card reads the Turn from `useTimeline()`, so each test seeds the
 * store via the same `events` dispatch the live view uses. This keeps
 * the tests honest — the store is *not* mocked, just driven.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";

import { i18next, initI18n } from "@/lib/i18n";
import {
  TimelineProvider,
  useTimeline,
} from "@/lib/sessions/store";
import type { LiveEvent } from "@/lib/sessions/event-stream";
import { TurnSummaryCard } from "@/components/sessions/turn-summary-card";

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

beforeEach(() => {
  initI18n();
  void i18next.changeLanguage("en");
});

afterEach(() => {
  cleanup();
});

const T0 = 1_700_000_000_000;
const COMPLETED_TURN_ID = "turn-aaa";
const ERRORED_TURN_ID = "turn-bbb";

const completedEvents: LiveEvent[] = [
  {
    turn_id: COMPLETED_TURN_ID,
    sequence: 1,
    timestamp_ms: T0,
    event_type: "TurnStart",
    payload: { user_text: "Hello world" },
  },
  {
    turn_id: COMPLETED_TURN_ID,
    sequence: 2,
    timestamp_ms: T0 + 500,
    event_type: "BlockStart",
    payload: { block_id: "b1", block_kind: "tool_use", tool_name: "Bash" },
  },
  {
    turn_id: COMPLETED_TURN_ID,
    sequence: 3,
    timestamp_ms: T0 + 6_000,
    event_type: "TurnComplete",
    payload: {
      usage: { input_tokens: 120, output_tokens: 45 },
      cost_usd: 0.0042,
      finish_reason: "stop",
    },
  },
];

const erroredEvents: LiveEvent[] = [
  {
    turn_id: ERRORED_TURN_ID,
    sequence: 1,
    timestamp_ms: T0,
    event_type: "TurnStart",
    payload: { user_text: "" },
  },
  {
    turn_id: ERRORED_TURN_ID,
    sequence: 2,
    timestamp_ms: T0 + 2_500,
    event_type: "TurnErrored",
    payload: { error_message: "upstream 502" },
  },
];

describe("TurnSummaryCard", () => {
  it("renders the completed badge and full field set", () => {
    render(
      <Harness events={completedEvents}>
        <TurnSummaryCard
          turnId={COMPLETED_TURN_ID}
          userInput="Hello world"
          finishReason="stop"
        />
      </Harness>,
    );

    const badge = screen.getByTestId("turn-summary-badge");
    expect(badge).toHaveAttribute("data-status", "complete");
    expect(badge).toHaveTextContent(/complete/i);

    expect(screen.getByText(/User input/i)).toBeInTheDocument();
    expect(screen.getByTestId("turn-summary-user-input")).toHaveTextContent(
      "Hello world",
    );
    // 120 + 45 input/output tokens.
    expect(screen.getByText("165")).toBeInTheDocument();
    expect(screen.getByText(/stop/)).toBeInTheDocument();
    // $0.0042 formatted by formatCost.
    expect(screen.getByText(/\$0\.0042/)).toBeInTheDocument();
    // 1 tool block.
    expect(screen.getByText(/Tool calls/i)).toBeInTheDocument();
  });

  it("renders the errored badge and dashes for missing fields", () => {
    render(
      <Harness events={erroredEvents}>
        <TurnSummaryCard
          turnId={ERRORED_TURN_ID}
          userInput={null}
          finishReason={null}
        />
      </Harness>,
    );

    const badge = screen.getByTestId("turn-summary-badge");
    expect(badge).toHaveAttribute("data-status", "errored");
    expect(badge).toHaveTextContent(/errored/i);

    // User-input preview is hidden when blank.
    expect(screen.queryByTestId("turn-summary-user-input")).not.toBeInTheDocument();
    // Error message rendered.
    expect(screen.getByTestId("turn-summary-error")).toHaveTextContent(
      "upstream 502",
    );
    // Missing tokens + cost + finish reason fall back to em-dash.
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(3);
  });
});
