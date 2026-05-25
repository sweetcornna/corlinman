/**
 * CostFooter tests — Phase 4 W2.3.
 *
 * Three fixture states:
 *   1. normal     — non-zero cost, fully-billed, all pills render.
 *   2. empty      — zero turns / zero cost → footer hides entirely.
 *   3. unknown    — `cost_status_breakdown.unknown > 0` → `~` prefix + tooltip.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";

import { i18next, initI18n } from "@/lib/i18n";
import {
  CostFooter,
  type SessionCostResponse,
} from "@/components/sessions/cost-footer";

function Harness({ children }: { children: React.ReactNode }) {
  return <I18nextProvider i18n={i18next}>{children}</I18nextProvider>;
}

beforeEach(() => {
  initI18n();
  i18next.changeLanguage("en");
});

afterEach(() => {
  cleanup();
});

const NORMAL: SessionCostResponse = {
  session_key: "qq:1234",
  turn_count: 12,
  total_elapsed_ms: 145_000,
  total_cost_usd: 0.087,
  cost_status_breakdown: { estimated: 0, billed: 12, unknown: 0 },
  total_tool_calls: 47,
  last_turn_at_ms: Date.now() - 2 * 60_000,
  avg_turn_ms: 12_083,
};

const EMPTY: SessionCostResponse = {
  session_key: "qq:empty",
  turn_count: 0,
  total_elapsed_ms: 0,
  total_cost_usd: 0,
  cost_status_breakdown: { estimated: 0, billed: 0, unknown: 0 },
  total_tool_calls: 0,
  last_turn_at_ms: null,
  avg_turn_ms: 0,
};

const UNKNOWN: SessionCostResponse = {
  ...NORMAL,
  cost_status_breakdown: { estimated: 8, billed: 2, unknown: 2 },
};

describe("CostFooter", () => {
  it("renders all five pills for a normal session", async () => {
    const fetcher = vi.fn().mockResolvedValue(NORMAL);
    render(
      <Harness>
        <CostFooter sessionKey="qq:1234" fetcher={fetcher} />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("cost-footer")).toBeInTheDocument();
    });
    expect(screen.getByTestId("cost-footer-total")).toHaveTextContent("$0.0870");
    expect(screen.getByTestId("cost-footer-turns")).toHaveTextContent("12");
    expect(screen.getByTestId("cost-footer-tools")).toHaveTextContent("47");
    expect(screen.getByTestId("cost-footer-avg-turn")).toHaveTextContent("12.1s");
    expect(screen.getByTestId("cost-footer-last")).toBeInTheDocument();
  });

  it("hides itself when the session has zero turns and zero cost", async () => {
    const fetcher = vi.fn().mockResolvedValue(EMPTY);
    const { container } = render(
      <Harness>
        <CostFooter sessionKey="qq:empty" fetcher={fetcher} />
      </Harness>,
    );

    await waitFor(() => {
      expect(fetcher).toHaveBeenCalledWith("qq:empty");
    });
    expect(screen.queryByTestId("cost-footer")).not.toBeInTheDocument();
    expect(container.firstChild).toBeNull();
  });

  it("flags estimates with `~` prefix + tooltip when unknown > 0", async () => {
    const fetcher = vi.fn().mockResolvedValue(UNKNOWN);
    render(
      <Harness>
        <CostFooter sessionKey="qq:1234" fetcher={fetcher} />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("cost-footer")).toBeInTheDocument();
    });
    const totalPill = screen.getByTestId("cost-footer-total");
    expect(totalPill).toHaveTextContent("~$0.0870");
    expect(totalPill.getAttribute("title")).toMatch(/estimate only/i);
  });
});
