import { describe, expect, it, afterEach, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { ScanLoginDialog } from "./ScanLoginDialog";

/**
 * ScanLoginDialog now embeds NapCat's first-party WebUI (reverse-proxied
 * at `/webui`) in an iframe — the relay-based QR flow was removed. These
 * tests just assert the iframe is mounted only while the dialog is open.
 */
describe("ScanLoginDialog", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  function stubDiagnostics() {
    const fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          mode: "external",
          url: "http://user-napcat:6099",
          url_source: "config",
          managed: false,
          auth_configured: true,
          credential: "ok",
          qrcode_api: "ok",
          onebot_config_api: "ok",
          issues: [],
          actions: [],
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetch);
    return fetch;
  }

  it("embeds the NapCat WebUI iframe when open", async () => {
    stubDiagnostics();
    render(<ScanLoginDialog open onOpenChange={() => {}} />);
    const frame = screen.getByTestId("qq-napcat-webui");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame.getAttribute("src")).toBe("/webui");
    await screen.findByTestId("qq-napcat-diagnostics-mode");
  });

  it("fetches and renders NapCat diagnostics when opened", async () => {
    const fetch = stubDiagnostics();
    render(<ScanLoginDialog open onOpenChange={() => {}} />);

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/admin/channels/qq/napcat/diagnostics",
        expect.objectContaining({ credentials: "include" }),
      );
    });
    expect(screen.getByTestId("qq-napcat-diagnostics-mode")).toHaveTextContent(
      "external",
    );
    expect(screen.getByTestId("qq-napcat-diagnostics-qrcode")).toHaveTextContent(
      "ok",
    );
  });

  it("does not mount the iframe while closed", () => {
    render(<ScanLoginDialog open={false} onOpenChange={() => {}} />);
    expect(screen.queryByTestId("qq-napcat-webui")).toBeNull();
  });
});
