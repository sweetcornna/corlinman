/**
 * `<InstallProgressModal>` tests (W2.2).
 *
 * Coverage:
 *   - Mounts → calls `postHubInstall(slug)`; the returned request_id is
 *     handed to `streamHubInstallEvents`.
 *   - SSE frames advance the 3-stage progress bar:
 *       download.started → extract.started → installed
 *   - Terminal `installed` frame surfaces the success toast + invalidates
 *     the `["skills"]` query cache.
 *
 * `streamHubInstallEvents` is mocked as a synchronous wrapper that captures
 * the `onMessage` callback so the test can drive the frame sequence by
 * hand without spinning up a real EventSource.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
  },
}));

vi.mock("@/lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    postHubInstall: vi.fn(),
    streamHubInstallEvents: vi.fn(),
  };
});

import { toast } from "sonner";
import { postHubInstall, streamHubInstallEvents } from "@/lib/api";
import type { HubInstallStatusOut } from "@/lib/api";
import { InstallProgressModal } from "../install-progress-modal";

const mockedInstall = vi.mocked(postHubInstall);
const mockedStream = vi.mocked(streamHubInstallEvents);

/** Capture the onMessage cb so the test can drive frames into the modal. */
function makeMockEventSource() {
  const handlers: Record<string, ((ev: Event) => void)[]> = {};
  const es = {
    readyState: 1,
    addEventListener: vi.fn((name: string, cb: (ev: Event) => void) => {
      (handlers[name] = handlers[name] ?? []).push(cb);
    }),
    removeEventListener: vi.fn(),
    close: vi.fn(),
  } as unknown as EventSource;
  return { es, handlers };
}

function renderModal() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  // Pre-seed a `["skills"]` query so we can assert it gets invalidated.
  qc.setQueryData(["skills"], { sentinel: true });
  const utils = render(
    <QueryClientProvider client={qc}>
      <InstallProgressModal
        open
        onOpenChange={() => {}}
        slug="web-search"
        name="Web Search"
      />
    </QueryClientProvider>,
  );
  return { ...utils, qc };
}

beforeEach(() => {
  mockedInstall.mockReset();
  mockedStream.mockReset();
  vi.mocked(toast.success).mockReset();
  vi.mocked(toast.error).mockReset();
});

afterEach(() => cleanup());

function frame(
  state: HubInstallStatusOut["state"],
  phase: string,
  extra: Partial<HubInstallStatusOut> = {},
): HubInstallStatusOut {
  return {
    request_id: "rq-1",
    slug: "web-search",
    version: "1.0.0",
    profile: "default",
    state,
    phase,
    ...extra,
  };
}

describe("<InstallProgressModal>", () => {
  it("posts the install, opens the SSE, and walks the 3-stage progress to success", async () => {
    mockedInstall.mockResolvedValue({ request_id: "rq-1" });

    let captured:
      | ((frame: HubInstallStatusOut) => void)
      | null = null;
    const { es } = makeMockEventSource();
    mockedStream.mockImplementation((_, onMessage) => {
      captured = onMessage;
      return es;
    });

    const { qc } = renderModal();

    // Wait for the POST + SSE wiring.
    await waitFor(() => expect(mockedInstall).toHaveBeenCalledTimes(1));
    expect(mockedInstall.mock.calls[0]?.[0]?.slug).toBe("web-search");
    await waitFor(() => expect(mockedStream).toHaveBeenCalled());
    expect(mockedStream.mock.calls[0]?.[0]).toBe("rq-1");

    // Drive a download.started frame.
    await act(async () => {
      captured?.(frame("running", "download.started"));
    });
    expect(
      screen
        .getByTestId("install-phase-download.started")
        .getAttribute("data-state"),
    ).toBe("current");

    // Drive an extract.started frame.
    await act(async () => {
      captured?.(frame("running", "extract.started"));
    });
    expect(
      screen
        .getByTestId("install-phase-extract.started")
        .getAttribute("data-state"),
    ).toBe("current");
    expect(
      screen
        .getByTestId("install-phase-download.started")
        .getAttribute("data-state"),
    ).toBe("past");

    // Drive the terminal installed frame.
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    await act(async () => {
      captured?.(
        frame("installed", "installed", { name: "web-search" }),
      );
    });

    // Toast + invalidation + close.
    expect(vi.mocked(toast.success)).toHaveBeenCalled();
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["skills"] });
    // The stream is closed by the modal once we hit terminal state.
    expect((es.close as ReturnType<typeof vi.fn>)).toHaveBeenCalled();
    // The final phase pill should now show as past.
    expect(
      screen
        .getByTestId("install-phase-installed")
        .getAttribute("data-state"),
    ).toBe("past");
  });

  it("surfaces the failure path with retry visible", async () => {
    mockedInstall.mockResolvedValue({ request_id: "rq-1" });

    let captured:
      | ((frame: HubInstallStatusOut) => void)
      | null = null;
    const { es } = makeMockEventSource();
    mockedStream.mockImplementation((_, onMessage) => {
      captured = onMessage;
      return es;
    });

    renderModal();
    await waitFor(() => expect(mockedStream).toHaveBeenCalled());

    await act(async () => {
      captured?.(
        frame("failed", "extract.started", { error: "boom" }),
      );
    });

    expect(screen.getByTestId("install-progress-error")).toBeInTheDocument();
    expect(screen.getByTestId("install-progress-retry")).toBeInTheDocument();

    // Retry → re-POSTs the install.
    mockedInstall.mockResolvedValueOnce({ request_id: "rq-2" });
    fireEvent.click(screen.getByTestId("install-progress-retry"));
    await waitFor(() => expect(mockedInstall).toHaveBeenCalledTimes(2));
  });
});
