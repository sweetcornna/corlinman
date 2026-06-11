import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Alert } from "./alert";

describe("Alert", () => {
  it("renders title and body with status role for warnings/dangers", () => {
    render(
      <Alert variant="danger" title="Offline">
        gateway unreachable
      </Alert>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-variant", "danger");
    expect(alert).toHaveTextContent("Offline");
    expect(alert).toHaveTextContent("gateway unreachable");
  });

  it("uses status role for informational variants", () => {
    render(<Alert variant="info">heads up</Alert>);
    expect(screen.getByRole("status")).toHaveTextContent("heads up");
  });

  it("uses token-based variant classes (no hardcoded palette colors)", () => {
    render(<Alert variant="warning">w</Alert>);
    const el = screen.getByRole("alert");
    expect(el.className).toContain("bg-sg-warn-soft");
    expect(el.className).toContain("border-sg-warn/30");
    expect(el.className).not.toMatch(/amber|red-\d|yellow-\d/);
  });

  it("allows hiding the icon", () => {
    const { container } = render(
      <Alert variant="success" icon={null}>
        ok
      </Alert>,
    );
    expect(container.querySelector("svg")).toBeNull();
  });
});
