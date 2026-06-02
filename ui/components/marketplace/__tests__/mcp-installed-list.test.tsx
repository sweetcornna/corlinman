/**
 * `<McpInstalledList>` tests — the reconfigure flow.
 *
 * Coverage:
 *   - A Reconfigure button renders per row and opens an edit dialog.
 *   - Submitting the dialog PUTs `reconfigureMcpServer` with ONLY the
 *     fields the operator filled (absent keys are not forwarded), and the
 *     env textarea is parsed into a `KEY=value` map.
 *   - Version is pre-filled and only forwarded when changed.
 *
 * react-i18next is stubbed to pass keys through verbatim; the api functions
 * are mocked so no real network is hit.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) =>
      vars ? `${key}:${JSON.stringify(vars)}` : key,
  }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listMcpServers: vi.fn(),
    reconfigureMcpServer: vi.fn(),
    enableMcpServer: vi.fn(),
    disableMcpServer: vi.fn(),
    restartMcpServer: vi.fn(),
    deleteMcpServer: vi.fn(),
  };
});

import {
  listMcpServers,
  reconfigureMcpServer,
  type InstalledMcpServer,
} from "@/lib/api";
import { McpInstalledList } from "../mcp-installed-list";

const mockedList = vi.mocked(listMcpServers);
const mockedReconfigure = vi.mocked(reconfigureMcpServer);

function makeRow(overrides: Partial<InstalledMcpServer> = {}): InstalledMcpServer {
  return {
    name: "github",
    source: "github",
    version: "1.0.0",
    enabled: true,
    transport: "stdio",
    status: "ready",
    tools: 3,
    error: null,
    installed_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

function renderList() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <McpInstalledList />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedList.mockReset();
  mockedReconfigure.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("<McpInstalledList> reconfigure", () => {
  it("opens the reconfigure dialog and PUTs only the filled fields", async () => {
    mockedList.mockResolvedValue([makeRow()]);
    mockedReconfigure.mockResolvedValue(makeRow({ version: "2.0.0" }));

    renderList();

    const reconfigureBtn = await screen.findByTestId("mcp-reconfigure-github");
    fireEvent.click(reconfigureBtn);

    // Dialog renders with version pre-filled.
    const versionInput = (await screen.findByTestId(
      "mcp-reconfigure-version",
    )) as HTMLInputElement;
    expect(versionInput.value).toBe("1.0.0");

    // Fill only the env textarea + bump the version.
    fireEvent.change(screen.getByTestId("mcp-reconfigure-env"), {
      target: { value: "GITHUB_TOKEN=tok_new\nGITHUB_ORG=acme" },
    });
    fireEvent.change(versionInput, { target: { value: "2.0.0" } });

    fireEvent.click(screen.getByTestId("mcp-reconfigure-submit"));

    await waitFor(() => expect(mockedReconfigure).toHaveBeenCalledTimes(1));
    expect(mockedReconfigure.mock.calls[0]?.[0]).toBe("github");
    // command/args/url were left blank → NOT forwarded; env parsed to a map.
    expect(mockedReconfigure.mock.calls[0]?.[1]).toEqual({
      env: { GITHUB_TOKEN: "tok_new", GITHUB_ORG: "acme" },
      version: "2.0.0",
    });
  });

  it("does not forward version when it is left unchanged", async () => {
    mockedList.mockResolvedValue([makeRow()]);
    mockedReconfigure.mockResolvedValue(makeRow());

    renderList();

    fireEvent.click(await screen.findByTestId("mcp-reconfigure-github"));
    await screen.findByTestId("mcp-reconfigure-command");

    // Only change the command; leave the pre-filled version as-is.
    fireEvent.change(screen.getByTestId("mcp-reconfigure-command"), {
      target: { value: "gh-mcp-v2" },
    });
    fireEvent.click(screen.getByTestId("mcp-reconfigure-submit"));

    await waitFor(() => expect(mockedReconfigure).toHaveBeenCalledTimes(1));
    expect(mockedReconfigure.mock.calls[0]?.[1]).toEqual({
      command: "gh-mcp-v2",
    });
  });
});
