import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { SessionRow } from "@/components/sessions/session-row";
import type { SessionSummary } from "@/lib/api/sessions";

vi.mock("@/components/sessions/session-cost-cells", () => ({
  SessionCostCells: () => <td data-testid="cost-cells-stub" />,
}));

const SESSION: SessionSummary = {
  session_key: "telegram:42",
  last_message_at: Date.now(),
  message_count: 5,
};

function renderRow() {
  const onReplay = vi.fn();
  const onDelete = vi.fn();
  render(
    <table>
      <tbody>
        <SessionRow
          session={SESSION}
          onReplay={onReplay}
          onDelete={onDelete}
        />
      </tbody>
    </table>,
  );
  return { onReplay, onDelete };
}

describe("SessionRow", () => {
  it("renders a Continue link pointing at /chat/{sessionKey}", () => {
    renderRow();
    // Button asChild + Link → the test id is forwarded onto the <a>.
    const link = screen.getByTestId(`session-continue-${SESSION.session_key}`);
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("href")).toBe(
      `/chat?session=${encodeURIComponent(SESSION.session_key)}`,
    );
  });

  it("still wires Replay and Delete actions", () => {
    const { onReplay, onDelete } = renderRow();
    fireEvent.click(screen.getByTestId(`session-replay-${SESSION.session_key}`));
    expect(onReplay).toHaveBeenCalledWith(SESSION);
    fireEvent.click(screen.getByTestId(`session-delete-${SESSION.session_key}`));
    expect(onDelete).toHaveBeenCalledWith(SESSION);
  });
});
