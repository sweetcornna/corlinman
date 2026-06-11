import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { LogRow } from "./log-row";

afterEach(() => cleanup());

describe("LogRow", () => {
  it("renders the four core columns and severity pill", () => {
    render(
      <LogRow
        ts="14:02:18"
        severity="warn"
        subsystem="approval"
        message="held 4.3s"
        duration="—"
      />,
    );
    expect(screen.getByText("14:02:18")).toBeInTheDocument();
    expect(screen.getByText("warn")).toBeInTheDocument();
    expect(screen.getByText("approval")).toBeInTheDocument();
    expect(screen.getByText("held 4.3s")).toBeInTheDocument();
  });

  it("marks the selected row with data-selected", () => {
    render(
      <LogRow
        ts="x"
        severity="err"
        subsystem="gw"
        message="m"
        selected
        data-testid="row"
      />,
    );
    expect(screen.getByTestId("row")).toHaveAttribute("data-selected", "true");
  });

  it("flags just-now rows", () => {
    render(
      <LogRow
        ts="x"
        severity="info"
        subsystem="gw"
        message="m"
        justNow
        data-testid="row"
      />,
    );
    expect(screen.getByTestId("row")).toHaveAttribute("data-just-now", "true");
  });

  it("fires onClick", () => {
    const onClick = vi.fn();
    render(
      <LogRow
        ts="x"
        severity="ok"
        subsystem="gw"
        message="m"
        onClick={onClick}
        data-testid="row"
      />,
    );
    fireEvent.click(screen.getByTestId("row"));
    expect(onClick).toHaveBeenCalled();
  });

  it("comfortable variant omits the severity pill (replaced by status dot)", () => {
    const { container } = render(
      <LogRow
        ts="x"
        severity="warn"
        subsystem="gw"
        message="m"
        variant="comfortable"
      />,
    );
    // The pill only renders in `dense`. A status dot appears in comfortable.
    expect(container.querySelector(".bg-sg-warn")).not.toBeNull();
    expect(container.textContent).not.toContain("warn"); // label not rendered
  });

  it("constrains long subsystem names so they cannot overlap the message", () => {
    render(
      <LogRow
        ts="01:49:55"
        severity="info"
        subsystem="corlinman.gateway.admin"
        message="admin.logs.subscribed (receiver_count=1)"
        variant="comfortable"
      />,
    );

    const subsystem = screen.getByText("corlinman.gateway.admin");
    const message = screen.getByText("admin.logs.subscribed (receiver_count=1)");

    expect(subsystem).toHaveClass("min-w-0");
    expect(subsystem).toHaveClass("truncate");
    expect(message).toHaveClass("min-w-0");
    expect(message).toHaveClass("truncate");
  });
});
