/**
 * `<InstalledList>` tests (W2.1).
 *
 * Coverage:
 *   - Renders one card per row with the right `data-origin` badge attr
 *     (bundled / user / hub).
 *   - Bundled rows: delete button is disabled + carries the tooltip
 *     i18n key.
 *   - Pin button toggles `pinned` via the onPin callback.
 *   - Filter chips narrow rows (asserted via search-derived filter pass).
 *   - Empty state when 0 rows, distinct empty state when filter narrows
 *     to 0 rows.
 *
 * react-i18next is stubbed to pass keys through verbatim so the test
 * doesn't depend on the W2.3 string landing — we assert against the
 * key namespace W2.3 must provide.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

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

import {
  InstalledList,
  filterRows,
  parseOrigin,
} from "../installed-list";
import type { InstalledSkillRow } from "@/lib/api";

function row(overrides: Partial<InstalledSkillRow> = {}): InstalledSkillRow {
  return {
    name: "web_search",
    description: "Query the web.",
    version: "1.0.0",
    state: "active",
    origin: "user",
    pinned: false,
    use_count: 0,
    last_used_at: null,
    created_at: null,
    body_markdown: "",
    when_to_use: null,
    allowed_tools: [],
    disable_model_invocation: false,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe("parseOrigin", () => {
  it("recognises bundled origin", () => {
    const badge = parseOrigin("bundled");
    expect(badge.kind).toBe("bundled");
    expect(badge.version).toBeNull();
  });

  it("recognises user origin", () => {
    expect(parseOrigin("user").kind).toBe("user");
  });

  it("parses hub:<slug>@<ver> origin and extracts the version", () => {
    const badge = parseOrigin("hub:web-search@1.2.3");
    expect(badge.kind).toBe("hub");
    expect(badge.version).toBe("1.2.3");
  });

  it("defaults unknown origins to user", () => {
    expect(parseOrigin("???").kind).toBe("user");
  });
});

describe("filterRows", () => {
  const rows: InstalledSkillRow[] = [
    row({ name: "alpha", origin: "bundled" }),
    row({ name: "beta", origin: "user" }),
    row({ name: "gamma", origin: "hub:gamma@1.0.0" }),
    row({ name: "delta", origin: "user", pinned: true }),
  ];

  it("returns all rows for `all` filter and empty search", () => {
    expect(filterRows(rows, "", "all")).toHaveLength(4);
  });

  it("narrows to bundled / user / hub buckets", () => {
    expect(filterRows(rows, "", "bundled").map((r) => r.name)).toEqual([
      "alpha",
    ]);
    expect(filterRows(rows, "", "user").map((r) => r.name)).toEqual([
      "beta",
      "delta",
    ]);
    expect(filterRows(rows, "", "hub").map((r) => r.name)).toEqual([
      "gamma",
    ]);
  });

  it("narrows to pinned rows", () => {
    expect(filterRows(rows, "", "pinned").map((r) => r.name)).toEqual([
      "delta",
    ]);
  });

  it("narrows by case-insensitive search across name and origin", () => {
    expect(filterRows(rows, "GAMMA", "all").map((r) => r.name)).toEqual([
      "gamma",
    ]);
    expect(filterRows(rows, "bundled", "all").map((r) => r.name)).toEqual([
      "alpha",
    ]);
  });
});

describe("<InstalledList /> rendering", () => {
  it("renders an empty state when there are no rows at all", () => {
    render(
      <InstalledList
        rows={[]}
        onPin={vi.fn()}
        onDelete={vi.fn()}
        search=""
        filter="all"
      />,
    );
    expect(screen.getByTestId("installed-list-empty")).toBeInTheDocument();
    // The "no rows at all" path renders the `emptyTitle` key.
    expect(
      screen.getByText("skills.installed.emptyTitle"),
    ).toBeInTheDocument();
  });

  it("renders a distinct empty state when the filter narrows rows to zero", () => {
    render(
      <InstalledList
        rows={[row({ name: "alpha", origin: "user" })]}
        onPin={vi.fn()}
        onDelete={vi.fn()}
        search=""
        filter="bundled"
      />,
    );
    expect(screen.getByTestId("installed-list-empty")).toBeInTheDocument();
    // The "filter cleared all rows" path renders `emptyFilteredTitle`.
    expect(
      screen.getByText("skills.installed.emptyFilteredTitle"),
    ).toBeInTheDocument();
  });

  it("renders one card per row with the correct origin badge", () => {
    const rows = [
      row({ name: "alpha", origin: "bundled" }),
      row({ name: "beta", origin: "user" }),
      row({ name: "gamma", origin: "hub:gamma@1.0.0" }),
    ];
    render(
      <InstalledList
        rows={rows}
        onPin={vi.fn()}
        onDelete={vi.fn()}
        search=""
        filter="all"
      />,
    );

    expect(screen.getByTestId("installed-card-alpha")).toHaveAttribute(
      "data-origin",
      "bundled",
    );
    expect(screen.getByTestId("installed-card-beta")).toHaveAttribute(
      "data-origin",
      "user",
    );
    expect(screen.getByTestId("installed-card-gamma")).toHaveAttribute(
      "data-origin",
      "hub",
    );

    // Sanity check: every card surfaces a badge pill.
    expect(screen.getByTestId("origin-badge-bundled")).toBeInTheDocument();
    expect(screen.getByTestId("origin-badge-user")).toBeInTheDocument();
    expect(screen.getByTestId("origin-badge-hub")).toBeInTheDocument();
  });

  it("disables the delete button for bundled rows + carries the tooltip key", () => {
    render(
      <InstalledList
        rows={[row({ name: "alpha", origin: "bundled" })]}
        onPin={vi.fn()}
        onDelete={vi.fn()}
        search=""
        filter="all"
      />,
    );

    const disabled = screen.getByTestId("installed-delete-disabled-alpha");
    expect(disabled).toBeDisabled();
    // The tooltip is mirrored on both the `title` and `aria-label` so
    // either is fine to assert against; we read the aria-label.
    expect(disabled.getAttribute("aria-label")).toContain(
      "skills.installed.bundledTooltip",
    );
    // Active delete button should NOT have rendered for this bundled row.
    expect(
      screen.queryByTestId("installed-delete-alpha"),
    ).not.toBeInTheDocument();
  });

  it("renders an enabled delete button for non-bundled rows", () => {
    render(
      <InstalledList
        rows={[row({ name: "beta", origin: "user" })]}
        onPin={vi.fn()}
        onDelete={vi.fn()}
        search=""
        filter="all"
      />,
    );
    const btn = screen.getByTestId("installed-delete-beta");
    expect(btn).not.toBeDisabled();
  });

  it("calls onPin with the inverted pinned state when the pin button is clicked", () => {
    const onPin = vi.fn();
    const r = row({ name: "alpha", origin: "user", pinned: false });
    render(
      <InstalledList
        rows={[r]}
        onPin={onPin}
        onDelete={vi.fn()}
        search=""
        filter="all"
      />,
    );

    fireEvent.click(screen.getByTestId("installed-pin-alpha"));
    expect(onPin).toHaveBeenCalledTimes(1);
    expect(onPin).toHaveBeenCalledWith(r, true);
  });

  it("calls onDelete when the (non-bundled) delete button is clicked", () => {
    const onDelete = vi.fn();
    const r = row({ name: "beta", origin: "user" });
    render(
      <InstalledList
        rows={[r]}
        onPin={vi.fn()}
        onDelete={onDelete}
        search=""
        filter="all"
      />,
    );

    fireEvent.click(screen.getByTestId("installed-delete-beta"));
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(onDelete).toHaveBeenCalledWith(r);
  });

  it("narrows visible cards when the filter prop is set", () => {
    const rows = [
      row({ name: "alpha", origin: "bundled" }),
      row({ name: "beta", origin: "user" }),
      row({ name: "gamma", origin: "hub:gamma@1.0.0" }),
    ];
    render(
      <InstalledList
        rows={rows}
        onPin={vi.fn()}
        onDelete={vi.fn()}
        search=""
        filter="hub"
      />,
    );

    expect(screen.queryByTestId("installed-card-alpha")).not.toBeInTheDocument();
    expect(screen.queryByTestId("installed-card-beta")).not.toBeInTheDocument();
    expect(screen.getByTestId("installed-card-gamma")).toBeInTheDocument();
  });

  it("disables the pin button when the row's name is in pinBusy", () => {
    render(
      <InstalledList
        rows={[row({ name: "alpha", origin: "user" })]}
        onPin={vi.fn()}
        onDelete={vi.fn()}
        search=""
        filter="all"
        pinBusy={new Set(["alpha"])}
      />,
    );
    expect(screen.getByTestId("installed-pin-alpha")).toBeDisabled();
  });
});
