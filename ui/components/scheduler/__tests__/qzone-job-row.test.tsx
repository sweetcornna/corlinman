import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { QzoneJobRow } from "@/components/scheduler/qzone-job-row";
import type { SchedulerJobRow } from "@/lib/api/scheduler";

// Return the raw key so assertions don't depend on the translated copy.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

afterEach(cleanup);

function runtimeJob(over: Partial<SchedulerJobRow> = {}): SchedulerJobRow {
  return {
    name: "grantley.daily_qzone",
    cron: "0 9 * * *",
    timezone: null,
    action_kind: "run_tool",
    next_fire_at: null,
    last_status: null,
    enabled: true,
    persona_id: "grantley",
    source: "runtime",
    ...over,
  };
}

function configJob(over: Partial<SchedulerJobRow> = {}): SchedulerJobRow {
  return runtimeJob({ source: "config", ...over });
}

interface Handlers {
  onTrigger?: (n: string) => void;
  onEdit?: (n: string) => void;
  onToggleEnabled?: (n: string) => void;
  onDelete?: (n: string) => void;
}

function renderRow(job: SchedulerJobRow, handlers: Handlers = {}) {
  const onTrigger = handlers.onTrigger ?? vi.fn();
  const onEdit = handlers.onEdit ?? vi.fn();
  const onToggleEnabled = handlers.onToggleEnabled ?? vi.fn();
  const onDelete = handlers.onDelete ?? vi.fn();
  render(
    <table>
      <tbody>
        <QzoneJobRow
          job={job}
          onTrigger={onTrigger}
          onEdit={onEdit}
          onToggleEnabled={onToggleEnabled}
          onDelete={onDelete}
        />
      </tbody>
    </table>,
  );
  return { onTrigger, onEdit, onToggleEnabled, onDelete };
}

describe("QzoneJobRow", () => {
  it("fires each of the four action callbacks with the job name", () => {
    const job = runtimeJob();
    const { onTrigger, onEdit, onToggleEnabled, onDelete } = renderRow(job);

    fireEvent.click(screen.getByTestId(`qzone-job-trigger-${job.name}`));
    fireEvent.click(screen.getByTestId(`qzone-job-edit-${job.name}`));
    fireEvent.click(screen.getByTestId(`qzone-job-toggle-${job.name}`));
    fireEvent.click(screen.getByTestId(`qzone-job-delete-${job.name}`));

    expect(onTrigger).toHaveBeenCalledWith(job.name);
    expect(onEdit).toHaveBeenCalledWith(job.name);
    expect(onToggleEnabled).toHaveBeenCalledWith(job.name);
    expect(onDelete).toHaveBeenCalledWith(job.name);
  });

  it("disables edit / toggle / delete for config jobs but keeps run-now enabled", () => {
    const job = configJob();
    renderRow(job);

    expect(screen.getByTestId(`qzone-job-trigger-${job.name}`)).not.toBeDisabled();
    expect(screen.getByTestId(`qzone-job-edit-${job.name}`)).toBeDisabled();
    expect(screen.getByTestId(`qzone-job-toggle-${job.name}`)).toBeDisabled();
    expect(screen.getByTestId(`qzone-job-delete-${job.name}`)).toBeDisabled();
  });

  it("renders the QZone permalink and OK badge after a successful run", () => {
    const job = runtimeJob({
      last_run_at_ms: 1_700_000_000_000,
      last_run_ok: true,
      last_qzone_url: "https://user.qzone.qq.com/1/2",
    });
    renderRow(job);

    const link = screen.getByRole("link", {
      name: /schedulerQzone\.row\.viewQzone/,
    });
    expect(link).toHaveAttribute("href", "https://user.qzone.qq.com/1/2");
  });

  it("shows the paused badge and a resume toggle when disabled", () => {
    const job = runtimeJob({ enabled: false });
    renderRow(job);
    // toggle exists + enabled for a runtime row regardless of paused state
    expect(screen.getByTestId(`qzone-job-toggle-${job.name}`)).not.toBeDisabled();
    expect(screen.getByText("schedulerQzone.row.paused")).toBeInTheDocument();
  });
});
