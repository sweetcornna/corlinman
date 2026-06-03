/**
 * `<SkillDrawer>` tests (W2.4 wire-up).
 *
 * The drawer was rewritten from the dead-code prototype (mock `Skill`
 * type, read-only) into the live editable surface keyed on
 * `InstalledSkillRow`. These tests lock:
 *
 *   1. Focus-trap + Esc-to-close still come through the shared `<Drawer>`
 *      (radix) primitive.
 *   2. The form seeds from the row's editor fields.
 *   3. Save is disabled until a field is edited (dirty diff).
 *   4. Save emits ONLY the changed fields to `onSave(name, patch)`.
 *
 * react-i18next is stubbed to pass keys (+ interpolation args) through
 * verbatim so the test doesn't depend on the locale strings landing.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => {
      if (opts && typeof opts === "object") {
        const parts: string[] = [key];
        for (const [k, v] of Object.entries(opts)) {
          parts.push(`${k}=${String(v)}`);
        }
        return parts.join("|");
      }
      return key;
    },
  }),
}));

import { SkillDrawer } from "./skill-drawer";
import type { InstalledSkillRow } from "@/lib/api";

const SAMPLE: InstalledSkillRow = {
  name: "web_search",
  description: "Query the web.",
  version: "1.0.0",
  state: "active",
  origin: "user",
  pinned: false,
  use_count: 0,
  last_used_at: null,
  created_at: null,
  body_markdown: "Live web search.",
  when_to_use: "when the user asks to search",
  allowed_tools: ["web_search.query", "web_search.fetch_page"],
  disable_model_invocation: false,
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SkillDrawer focus trap", () => {
  it("keeps focus within the drawer across 10 Tab presses", () => {
    render(
      <SkillDrawer skill={SAMPLE} open onOpenChange={vi.fn()} onSave={vi.fn()} />,
    );

    const dialog = screen.getByRole("dialog");
    for (let i = 0; i < 10; i++) {
      fireEvent.keyDown(document.activeElement || document.body, {
        key: "Tab",
      });
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it("fires onOpenChange(false) on Escape", () => {
    const onOpenChange = vi.fn();
    render(
      <SkillDrawer
        skill={SAMPLE}
        open
        onOpenChange={onOpenChange}
        onSave={vi.fn()}
      />,
    );
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

describe("SkillDrawer editing", () => {
  it("seeds the form fields from the row", () => {
    render(
      <SkillDrawer skill={SAMPLE} open onOpenChange={vi.fn()} onSave={vi.fn()} />,
    );

    expect(
      (screen.getByTestId("skill-edit-description") as HTMLTextAreaElement)
        .value,
    ).toBe("Query the web.");
    expect(
      (screen.getByTestId("skill-edit-when-to-use") as HTMLTextAreaElement)
        .value,
    ).toBe("when the user asks to search");
    expect(
      (screen.getByTestId("skill-edit-allowed-tools") as HTMLTextAreaElement)
        .value,
    ).toBe("web_search.query\nweb_search.fetch_page");
    expect(
      (screen.getByTestId("skill-edit-body") as HTMLTextAreaElement).value,
    ).toBe("Live web search.");
    expect(
      (
        screen.getByTestId(
          "skill-edit-disable-invocation",
        ) as HTMLInputElement
      ).checked,
    ).toBe(false);
  });

  it("disables Save until a field changes", () => {
    render(
      <SkillDrawer skill={SAMPLE} open onOpenChange={vi.fn()} onSave={vi.fn()} />,
    );
    expect(screen.getByTestId("skill-edit-save")).toBeDisabled();

    fireEvent.change(screen.getByTestId("skill-edit-description"), {
      target: { value: "Query the web (edited)." },
    });
    expect(screen.getByTestId("skill-edit-save")).not.toBeDisabled();
  });

  it("emits ONLY the changed fields on Save", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <SkillDrawer skill={SAMPLE} open onOpenChange={vi.fn()} onSave={onSave} />,
    );

    fireEvent.change(screen.getByTestId("skill-edit-description"), {
      target: { value: "New summary." },
    });
    fireEvent.click(screen.getByTestId("skill-edit-disable-invocation"));

    fireEvent.click(screen.getByTestId("skill-edit-save"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith("web_search", {
      description: "New summary.",
      disable_model_invocation: true,
    });
  });

  it("parses the allowed-tools textarea into a clean list on Save", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <SkillDrawer skill={SAMPLE} open onOpenChange={vi.fn()} onSave={onSave} />,
    );

    fireEvent.change(screen.getByTestId("skill-edit-allowed-tools"), {
      target: { value: "  alpha.read \n\n beta.write \n" },
    });
    fireEvent.click(screen.getByTestId("skill-edit-save"));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith("web_search", {
      allowed_tools: ["alpha.read", "beta.write"],
    });
  });

  it("disables the form + footer while saving", () => {
    render(
      <SkillDrawer
        skill={SAMPLE}
        open
        onOpenChange={vi.fn()}
        onSave={vi.fn()}
        saving
      />,
    );
    expect(screen.getByTestId("skill-edit-description")).toBeDisabled();
    expect(screen.getByTestId("skill-edit-save")).toBeDisabled();
    expect(screen.getByTestId("skill-edit-cancel")).toBeDisabled();
  });
});
