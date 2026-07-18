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

import type { SchedulerJobRow } from "@/lib/api/scheduler";
import type { Persona } from "@/lib/api/personas";

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

// Hoisted mocks so the vi.mock factories can reference them safely.
const {
  createMock,
  patchMock,
  deleteMock,
  triggerMock,
  fetchJobsMock,
  pauseMock,
  resumeMock,
  fetchPersonasMock,
  listAssetsMock,
} = vi.hoisted(() => ({
  createMock: vi.fn(),
  patchMock: vi.fn(),
  deleteMock: vi.fn(),
  triggerMock: vi.fn(),
  fetchJobsMock: vi.fn(),
  pauseMock: vi.fn(),
  resumeMock: vi.fn(),
  fetchPersonasMock: vi.fn(),
  listAssetsMock: vi.fn(),
}));

vi.mock("@/lib/api/scheduler", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/scheduler")>();
  return {
    ...actual,
    fetchSchedulerJobsTyped: (...a: unknown[]) => fetchJobsMock(...a),
    createSchedulerJob: (...a: unknown[]) => createMock(...a),
    patchSchedulerJob: (...a: unknown[]) => patchMock(...a),
    deleteSchedulerJob: (...a: unknown[]) => deleteMock(...a),
    triggerSchedulerJobTyped: (...a: unknown[]) => triggerMock(...a),
  };
});

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    pauseSchedulerJob: (...a: unknown[]) => pauseMock(...a),
    resumeSchedulerJob: (...a: unknown[]) => resumeMock(...a),
  };
});

vi.mock("@/lib/api/personas", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/personas")>();
  return {
    ...actual,
    fetchPersonas: (...a: unknown[]) => fetchPersonasMock(...a),
    listAssets: (...a: unknown[]) => listAssetsMock(...a),
  };
});

import QzoneSchedulerPage from "@/app/(admin)/scheduler/qzone/page";

function runtimeJob(over: Partial<SchedulerJobRow> = {}): SchedulerJobRow {
  return {
    name: "grantley.daily_qzone",
    cron: "0 21 * * *",
    timezone: null,
    action_kind: "run_tool",
    action_type: "qzone.daily_publish",
    next_fire_at: null,
    last_status: null,
    enabled: true,
    persona_id: "grantley",
    prompt_template: "hello",
    source: "runtime",
    ...over,
  };
}

function persona(over: Partial<Persona> = {}): Persona {
  return {
    id: "grantley",
    display_name: "Grantley",
    short_summary: "",
    system_prompt: "",
    is_builtin: false,
    created_at_ms: 0,
    updated_at_ms: 0,
    avatar_url: null,
    model_bindings: {
      text: { provider: null, model: null },
      image: { provider: null, model: null },
      voice: { provider: null, model: null },
    },
    ...over,
  };
}

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <QzoneSchedulerPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  createMock.mockReset().mockResolvedValue(runtimeJob());
  patchMock.mockReset().mockResolvedValue(runtimeJob());
  deleteMock
    .mockReset()
    .mockResolvedValue({ ok: true, deleted: "grantley.daily_qzone" });
  triggerMock.mockReset().mockResolvedValue({ ok: true });
  fetchJobsMock.mockReset().mockResolvedValue([]);
  pauseMock.mockReset().mockResolvedValue(runtimeJob({ enabled: false }));
  resumeMock.mockReset().mockResolvedValue(runtimeJob({ enabled: true }));
  fetchPersonasMock.mockReset().mockResolvedValue([persona()]);
  listAssetsMock.mockReset().mockResolvedValue([]);
});

afterEach(cleanup);

describe("QzoneSchedulerPage", () => {
  it("edit flow backfills the form and saves via PATCH (not POST)", async () => {
    fetchJobsMock.mockResolvedValue([runtimeJob()]);
    renderPage();

    // Row shows up once the jobs query resolves.
    const editBtn = await screen.findByTestId(
      "qzone-job-edit-grantley.daily_qzone",
    );
    fireEvent.click(editBtn);

    // parseCron("0 21 * * *") → daily @ 21:00 → time input backfilled.
    await waitFor(() =>
      expect(screen.getByTestId("qzone-schedule-time")).toHaveValue("21:00"),
    );
    // Persona is locked while editing.
    expect(screen.getByRole("combobox")).toBeDisabled();
    // Cancel-edit affordance appears.
    expect(screen.getByTestId("qzone-cancel-edit")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("qzone-job-save"));

    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    expect(patchMock).toHaveBeenCalledWith(
      "grantley.daily_qzone",
      expect.objectContaining({
        cron: "0 21 * * *",
        action_type: "qzone.daily_publish",
        persona_id: "grantley",
        prompt_template: "hello",
        image_ref_labels: [],
        jitter_minutes: 0,
      }),
    );
    expect(createMock).not.toHaveBeenCalled();
  });

  it("new flow saves via POST (not PATCH)", async () => {
    fetchJobsMock.mockResolvedValue([]);
    renderPage();

    // Wait for the persona option to render before selecting it.
    await screen.findByRole("option", { name: "Grantley (grantley)" });
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "grantley" },
    });

    fireEvent.click(screen.getByTestId("qzone-job-save"));

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    expect(createMock).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "grantley.daily_qzone",
        cron: "0 9 * * *",
        action_type: "qzone.daily_publish",
        persona_id: "grantley",
      }),
    );
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("delete goes through the confirm dialog before calling the API", async () => {
    fetchJobsMock.mockResolvedValue([runtimeJob()]);
    renderPage();

    const deleteBtn = await screen.findByTestId(
      "qzone-job-delete-grantley.daily_qzone",
    );
    fireEvent.click(deleteBtn);
    // Nothing fires until the operator confirms.
    expect(deleteMock).not.toHaveBeenCalled();

    const confirm = await screen.findByTestId(
      "qzone-job-delete-confirm-confirm",
    );
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(deleteMock).toHaveBeenCalledWith("grantley.daily_qzone"),
    );
  });

  it("toggling an enabled job routes to pause", async () => {
    fetchJobsMock.mockResolvedValue([runtimeJob({ enabled: true })]);
    renderPage();

    const toggle = await screen.findByTestId(
      "qzone-job-toggle-grantley.daily_qzone",
    );
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(pauseMock).toHaveBeenCalledWith("grantley.daily_qzone"),
    );
    expect(resumeMock).not.toHaveBeenCalled();
  });

  it("toggling a paused job routes to resume", async () => {
    fetchJobsMock.mockResolvedValue([runtimeJob({ enabled: false })]);
    renderPage();

    const toggle = await screen.findByTestId(
      "qzone-job-toggle-grantley.daily_qzone",
    );
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(resumeMock).toHaveBeenCalledWith("grantley.daily_qzone"),
    );
    expect(pauseMock).not.toHaveBeenCalled();
  });

  it("disables the save button when the schedule can't compose a cron", async () => {
    fetchJobsMock.mockResolvedValue([]);
    renderPage();

    await screen.findByRole("option", { name: "Grantley (grantley)" });
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "grantley" },
    });

    // Save is enabled with the default daily schedule…
    expect(screen.getByTestId("qzone-job-save")).not.toBeDisabled();

    // …switch to weekly with no weekday selected → composeCron === null.
    fireEvent.click(screen.getByText("schedulerQzone.schedule.modeWeekly"));

    await waitFor(() =>
      expect(screen.getByTestId("qzone-job-save")).toBeDisabled(),
    );
  });
});
