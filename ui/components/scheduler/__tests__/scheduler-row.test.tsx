/**
 * Scheduler runtime-job UI wiring (pause/resume + enabled-aware status).
 *
 * Covers the gap-fill that wired the previously-dead Pause/Resume buttons:
 *   - `deriveStatus` honours the explicit `enabled` flag on runtime rows
 *     (so a resumed runtime job reads "enabled" before `next_fire_at`
 *     is published, and a paused one reads "paused").
 *   - `<SchedulerRow>` enables the Pause/Resume toggle for runtime jobs
 *     and fires `onPause` / `onResume`.
 *   - The toggle stays disabled for config-derived rows (no `enabled`
 *     gate to flip — those are edited in corlinman.toml).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { deriveStatus } from "@/components/scheduler/scheduler-util";
import { SchedulerRow } from "@/components/scheduler/scheduler-row";
import type { SchedulerJob } from "@/lib/api";

afterEach(cleanup);

function runtimeJob(over: Partial<SchedulerJob> = {}): SchedulerJob {
  return {
    name: "rt.daily",
    cron: "0 9 * * *",
    timezone: "Asia/Shanghai",
    action_kind: "run_tool",
    next_fire_at: null,
    last_status: null,
    action_type: "qzone.daily_publish",
    enabled: true,
    source: "runtime",
    ...over,
  };
}

function configJob(over: Partial<SchedulerJob> = {}): SchedulerJob {
  return {
    name: "system.update_check",
    cron: "0 0 */6 * * *",
    timezone: null,
    action_kind: "run_tool",
    next_fire_at: null,
    last_status: null,
    source: "config",
    ...over,
  };
}

describe("deriveStatus", () => {
  it("reads enabled=true runtime job as enabled even without next_fire_at", () => {
    expect(deriveStatus(runtimeJob({ enabled: true, next_fire_at: null }))).toBe(
      "enabled",
    );
  });

  it("reads enabled=false runtime job as paused", () => {
    expect(deriveStatus(runtimeJob({ enabled: false }))).toBe("paused");
  });

  it("still flags a failed last_status as errored regardless of enabled", () => {
    expect(deriveStatus(runtimeJob({ enabled: true, last_status: "error" }))).toBe(
      "errored",
    );
  });

  it("falls back to next_fire_at for config rows with no enabled flag", () => {
    expect(deriveStatus(configJob({ next_fire_at: null }))).toBe("paused");
    expect(
      deriveStatus(configJob({ next_fire_at: "2999-01-01T00:00:00Z" })),
    ).toBe("enabled");
  });
});

describe("<SchedulerRow> pause/resume", () => {
  it("fires onPause for an enabled runtime job", () => {
    const onPause = vi.fn();
    const onResume = vi.fn();
    render(
      <SchedulerRow
        job={runtimeJob({ enabled: true })}
        status="enabled"
        now={Date.now()}
        onSelect={vi.fn()}
        onTrigger={vi.fn()}
        onPause={onPause}
        onResume={onResume}
      />,
    );
    const toggle = screen.getByTestId("scheduler-toggle-rt.daily");
    expect(toggle).not.toBeDisabled();
    fireEvent.click(toggle);
    expect(onPause).toHaveBeenCalledWith("rt.daily");
    expect(onResume).not.toHaveBeenCalled();
  });

  it("fires onResume for a paused runtime job", () => {
    const onResume = vi.fn();
    render(
      <SchedulerRow
        job={runtimeJob({ enabled: false })}
        status="paused"
        now={Date.now()}
        onSelect={vi.fn()}
        onTrigger={vi.fn()}
        onPause={vi.fn()}
        onResume={onResume}
      />,
    );
    const toggle = screen.getByTestId("scheduler-toggle-rt.daily");
    expect(toggle).not.toBeDisabled();
    fireEvent.click(toggle);
    expect(onResume).toHaveBeenCalledWith("rt.daily");
  });

  it("disables the toggle for config-derived rows", () => {
    const onPause = vi.fn();
    render(
      <SchedulerRow
        job={configJob()}
        status="enabled"
        now={Date.now()}
        onSelect={vi.fn()}
        onTrigger={vi.fn()}
        onPause={onPause}
        onResume={vi.fn()}
      />,
    );
    const toggle = screen.getByTestId("scheduler-toggle-system.update_check");
    expect(toggle).toBeDisabled();
    fireEvent.click(toggle);
    expect(onPause).not.toHaveBeenCalled();
  });

  it("disables the toggle while a pause/resume is in flight", () => {
    const onPause = vi.fn();
    render(
      <SchedulerRow
        job={runtimeJob({ enabled: true })}
        status="enabled"
        now={Date.now()}
        pausing
        onSelect={vi.fn()}
        onTrigger={vi.fn()}
        onPause={onPause}
        onResume={vi.fn()}
      />,
    );
    const toggle = screen.getByTestId("scheduler-toggle-rt.daily");
    expect(toggle).toBeDisabled();
  });
});
