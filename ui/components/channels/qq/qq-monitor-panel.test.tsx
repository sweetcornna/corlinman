import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { QqMonitorSpec } from "@/lib/api/qq-monitors";

// Return the raw key so assertions don't depend on the translated copy.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
    error: vi.fn(),
  },
}));

// Hoisted mocks so the vi.mock factory can reference them safely.
const {
  instanceMock,
  getMonitorsMock,
  putMonitorsMock,
  triggerMock,
  statusMock,
} = vi.hoisted(() => ({
  instanceMock: vi.fn(),
  getMonitorsMock: vi.fn(),
  putMonitorsMock: vi.fn(),
  triggerMock: vi.fn(),
  statusMock: vi.fn(),
}));

vi.mock("@/lib/api/qq-monitors", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/api/qq-monitors")>();
  return {
    ...actual,
    getQqDefaultInstanceId: (...a: unknown[]) => instanceMock(...a),
    getQqMonitors: (...a: unknown[]) => getMonitorsMock(...a),
    putQqMonitors: (...a: unknown[]) => putMonitorsMock(...a),
    triggerQqMonitor: (...a: unknown[]) => triggerMock(...a),
    getQqMonitorsStatus: (...a: unknown[]) => statusMock(...a),
  };
});

import { toast } from "sonner";
import { QqMonitorPanel } from "@/components/channels/qq/qq-monitor-panel";

function spec(over: Partial<QqMonitorSpec> = {}): QqMonitorSpec {
  return {
    id: "daily-digest",
    enabled: true,
    source_group: "123456",
    watch_user_ids: [],
    schedule_type: "daily",
    daily_time: "21:00",
    interval_minutes: null,
    timezone: "",
    window_minutes: 0,
    target_type: "group",
    target_id: "123456",
    style_extra: "",
    send_when_empty: false,
    ...over,
  };
}

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <QqMonitorPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  instanceMock.mockReset().mockResolvedValue("default");
  getMonitorsMock.mockReset().mockResolvedValue({
    monitors: [spec()],
    warnings: [],
    revision: "rev-1",
  });
  putMonitorsMock.mockReset().mockResolvedValue({
    monitors: [spec()],
    warnings: [],
    revision: "rev-2",
  });
  triggerMock.mockReset().mockResolvedValue({ status: "triggered" });
  statusMock.mockReset().mockResolvedValue({
    statuses: {
      "daily-digest": {
        last_run_ms: Date.now() - 120_000,
        last_ok: true,
        last_count: 3,
      },
    },
    counts: { "daily-digest": 5 },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("QqMonitorPanel", () => {
  it("loads and renders the rule list with status merged in", async () => {
    statusMock.mockResolvedValue({
      statuses: {
        "daily-digest": {
          last_run_ms: Date.now() - 120_000,
          last_ok: false,
          last_error: "delivery boom",
        },
      },
      counts: { "daily-digest": 5 },
    });
    renderPanel();

    const row = await screen.findByTestId("qq-monitor-row-daily-digest");
    expect(row).toBeInTheDocument();
    expect(row).toHaveTextContent("daily-digest");
    expect(row).toHaveTextContent("123456");
    // Monitors fetched for the resolved default instance.
    await waitFor(() =>
      expect(getMonitorsMock).toHaveBeenCalledWith("default"),
    );
    // last_ok === false surfaces last_error in the row.
    expect(await screen.findByText("delivery boom")).toBeInTheDocument();
  });

  it("adds a rule via the form and saves the whole list via PUT", async () => {
    renderPanel();
    await screen.findByTestId("qq-monitor-row-daily-digest");

    fireEvent.click(screen.getByTestId("qq-monitor-add"));
    fireEvent.change(screen.getByTestId("qq-monitor-form-id"), {
      target: { value: "night-watch" },
    });
    fireEvent.change(screen.getByTestId("qq-monitor-form-group"), {
      target: { value: "222333" },
    });
    fireEvent.change(screen.getByTestId("qq-monitor-form-target-id"), {
      target: { value: "444555" },
    });
    fireEvent.click(screen.getByTestId("qq-monitor-form-apply"));

    // The new rule lands in the local draft list (not yet saved).
    expect(
      await screen.findByTestId("qq-monitor-row-night-watch"),
    ).toBeInTheDocument();
    expect(putMonitorsMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("qq-monitor-save"));

    await waitFor(() => expect(putMonitorsMock).toHaveBeenCalledTimes(1));
    expect(putMonitorsMock).toHaveBeenCalledWith(
      "default",
      [
        expect.objectContaining({ id: "daily-digest" }),
        expect.objectContaining({
          id: "night-watch",
          enabled: true,
          source_group: "222333",
          watch_user_ids: [],
          schedule_type: "daily",
          daily_time: "09:00",
          interval_minutes: null,
          window_minutes: 0,
          target_type: "group",
          target_id: "444555",
          send_when_empty: false,
        }),
      ],
      "rev-1",
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("qqMonitor.saved"),
    );
  });

  it("explains itself instead of dying silently when saving with no diff", async () => {
    renderPanel();
    await screen.findByTestId("qq-monitor-row-daily-digest");

    fireEvent.click(screen.getByTestId("qq-monitor-save"));

    expect(toast.info).toHaveBeenCalledWith("channels.noChanges");
    expect(putMonitorsMock).not.toHaveBeenCalled();
  });

  it("fires the trigger endpoint from the row's test-send button", async () => {
    renderPanel();
    await screen.findByTestId("qq-monitor-row-daily-digest");

    fireEvent.click(screen.getByTestId("qq-monitor-trigger-daily-digest"));

    await waitFor(() =>
      expect(triggerMock).toHaveBeenCalledWith("default", "daily-digest"),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("qqMonitor.triggered"),
    );
  });

  it("delete asks for confirmation before dropping the row from the draft", async () => {
    renderPanel();
    await screen.findByTestId("qq-monitor-row-daily-digest");

    fireEvent.click(screen.getByTestId("qq-monitor-delete-daily-digest"));
    // Nothing changes until the operator confirms.
    expect(
      screen.getByTestId("qq-monitor-row-daily-digest"),
    ).toBeInTheDocument();

    const confirm = await screen.findByTestId(
      "qq-monitor-delete-confirm-confirm",
    );
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(
        screen.queryByTestId("qq-monitor-row-daily-digest"),
      ).not.toBeInTheDocument(),
    );
    // Deletion is draft-local — the PUT only happens on Save.
    expect(putMonitorsMock).not.toHaveBeenCalled();
  });
});
