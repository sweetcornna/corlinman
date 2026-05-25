/**
 * `<CopyUpgradeCommand>` tests (W2.1).
 *
 * Coverage:
 *   - Click the Copy button → `navigator.clipboard.writeText` is invoked
 *     with the exact `command` string.
 *
 * jsdom doesn't ship a real Clipboard API, so we install a stub via
 * `Object.defineProperty(navigator, "clipboard", …)` before rendering.
 * `react-i18next` is stubbed to pass keys through unchanged, and the
 * `sonner` toast is mocked to a noop so the test doesn't depend on a
 * `<Toaster>` being mounted.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
  },
}));

import { CopyUpgradeCommand } from "../copy-upgrade-command";

const writeTextMock = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  writeTextMock.mockClear();
  // Re-install the clipboard stub before every test; some tests may
  // overwrite it locally.
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      writeText: writeTextMock,
    },
  });
});

afterEach(() => cleanup());

describe("<CopyUpgradeCommand>", () => {
  it("copies the command to the clipboard on click", async () => {
    const command = "bash install.sh --upgrade --version v1.2.3";
    render(<CopyUpgradeCommand label="Native deploy" command={command} />);

    const button = screen.getByTestId("copy-upgrade-command-button");
    // The click handler is async (it awaits `writeText`); wrapping in
    // `act` flushes the resulting state update so React doesn't emit a
    // "not wrapped in act(...)" warning.
    await act(async () => {
      fireEvent.click(button);
    });

    expect(writeTextMock).toHaveBeenCalledTimes(1);
    expect(writeTextMock).toHaveBeenCalledWith(command);
  });

  it("renders the command verbatim inside a <pre> block", () => {
    const command = "docker compose pull && docker compose up -d";
    render(<CopyUpgradeCommand label="Docker" command={command} />);

    const pre = screen.getByTestId("copy-upgrade-command-pre");
    expect(pre.textContent).toBe(command);
  });
});
