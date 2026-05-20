import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
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

// IMPORTANT: hoist-friendly mock of the api module before importing the
// component under test. vitest runs vi.mock() before ESM imports.
vi.mock("@/lib/api", () => ({
  requestQqQrcode: vi.fn(),
  fetchQqQrcodeStatus: vi.fn(),
  fetchQqAccounts: vi.fn(),
  qqQuickLogin: vi.fn(),
}));

vi.mock("@/components/ui/countdown-ring", () => ({
  CountdownRing: ({ label }: { label?: string }) => (
    <span data-testid="mock-countdown-ring" aria-label={label} />
  ),
}));

import {
  fetchQqAccounts,
  fetchQqQrcodeStatus,
  qqQuickLogin,
  requestQqQrcode,
} from "@/lib/api";
import { ScanLoginDialog } from "./ScanLoginDialog";

const mockedRequestQrcode = vi.mocked(requestQqQrcode);
const mockedStatus = vi.mocked(fetchQqQrcodeStatus);
const mockedAccounts = vi.mocked(fetchQqAccounts);
const mockedQuick = vi.mocked(qqQuickLogin);

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ScanLoginDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("renders the QR code when the gateway returns image_base64", async () => {
    mockedRequestQrcode.mockResolvedValue({
      token: "tok-1",
      image_base64: "iVBORw0KGgo_stub",
      qrcode_url: null,
      expires_at: Date.now() + 60_000,
    });
    mockedStatus.mockResolvedValue({ status: "waiting" });
    mockedAccounts.mockResolvedValue({ accounts: [] });

    renderWithClient(<ScanLoginDialog open onOpenChange={() => {}} />);

    const img = await screen.findByTestId("qq-qrcode");
    expect(img.tagName).toBe("IMG");
    expect((img as HTMLImageElement).src).toContain("iVBORw0KGgo_stub");
    expect(mockedRequestQrcode).toHaveBeenCalledOnce();
  });

  it("shows an error message when qrcode request fails", async () => {
    mockedRequestQrcode.mockRejectedValue(new Error("napcat down"));
    mockedAccounts.mockResolvedValue({ accounts: [] });

    renderWithClient(<ScanLoginDialog open onOpenChange={() => {}} />);

    const err = await screen.findByTestId("qq-login-error");
    expect(err.textContent).toContain("napcat down");
  });

  it("closes via onOpenChange once status becomes confirmed", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout"] });
    const onOpenChange = vi.fn();

    mockedRequestQrcode.mockResolvedValue({
      token: "tok-2",
      image_base64: "abc",
      qrcode_url: null,
      expires_at: Date.now() + 60_000,
    });
    mockedStatus.mockResolvedValue({
      status: "confirmed",
      account: {
        uin: "123",
        nickname: "Tester",
        last_login_at: Date.now(),
      },
    });
    mockedAccounts.mockResolvedValue({ accounts: [] });

    renderWithClient(<ScanLoginDialog open onOpenChange={onOpenChange} />);

    // Wait for the QR + first poll to resolve.
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedRequestQrcode).toHaveBeenCalled();
    // Advance past the 2s poll interval.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_100);
    });
    // Now flush the confirmed-close delay (1.5s).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_600);
    });

    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("renders the quick-login list and calls qqQuickLogin on click", async () => {
    mockedRequestQrcode.mockResolvedValue({
      token: "tok-3",
      image_base64: "abc",
      qrcode_url: null,
      expires_at: Date.now() + 60_000,
    });
    mockedStatus.mockResolvedValue({ status: "waiting" });
    mockedAccounts.mockResolvedValue({
      accounts: [
        {
          uin: "42",
          nickname: "Old",
          last_login_at: 1,
        },
      ],
    });
    mockedQuick.mockResolvedValue({
      status: "confirmed",
      account: { uin: "42", nickname: "Old", last_login_at: 2 },
    });

    renderWithClient(<ScanLoginDialog open onOpenChange={() => {}} />);

    const btn = await screen.findByTestId("qq-quick-login-42");
    fireEvent.click(btn);

    await waitFor(() => expect(mockedQuick).toHaveBeenCalledWith("42"));
  });
});
