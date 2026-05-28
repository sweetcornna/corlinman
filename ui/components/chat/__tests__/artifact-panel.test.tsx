import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ArtifactPanel } from "@/components/chat/artifact-panel";
import type { Artifact } from "@/lib/chat/artifacts";

function mk(overrides: Partial<Artifact> = {}): Artifact {
  return {
    id: "art_1",
    kind: "code",
    title: "py: hello",
    language: "py",
    source: "print('hi')",
    messageId: "msg_1",
    ...overrides,
  };
}

describe("ArtifactPanel", () => {
  it("returns nothing when closed or empty", () => {
    const { container, rerender } = render(
      <ArtifactPanel
        artifacts={[]}
        activeId={null}
        open={false}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
    rerender(
      <ArtifactPanel
        artifacts={[]}
        activeId={null}
        open={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders source view by default for non-previewable kinds", () => {
    render(
      <ArtifactPanel
        artifacts={[mk()]}
        activeId="art_1"
        open={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByTestId("artifact-panel")).toBeInTheDocument();
    expect(screen.getByTestId("artifact-body").getAttribute("data-view")).toBe(
      "source",
    );
  });

  it("renders iframe preview for html kind", () => {
    render(
      <ArtifactPanel
        artifacts={[mk({ id: "h", kind: "html", language: "html", source: "<h1>hi</h1>" })]}
        activeId="h"
        open={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByTestId("artifact-iframe-html")).toBeInTheDocument();
  });

  it("renders svg inline for svg kind", () => {
    render(
      <ArtifactPanel
        artifacts={[mk({ id: "s", kind: "svg", language: "svg", source: "<svg/>" })]}
        activeId="s"
        open={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByTestId("artifact-svg")).toBeInTheDocument();
  });

  it("close button fires onClose", () => {
    const onClose = vi.fn();
    render(
      <ArtifactPanel
        artifacts={[mk()]}
        activeId="art_1"
        open={true}
        onClose={onClose}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByLabelText("关闭工件面板"));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("tab click selects another artifact", () => {
    const onSelect = vi.fn();
    render(
      <ArtifactPanel
        artifacts={[
          mk({ id: "a", language: "py" }),
          mk({ id: "b", language: "go", title: "go" }),
        ]}
        activeId="a"
        open={true}
        onClose={vi.fn()}
        onSelect={onSelect}
        onRemove={vi.fn()}
      />,
    );
    const tabs = screen.getAllByTestId("artifact-tab");
    fireEvent.click(tabs[1]);
    expect(onSelect).toHaveBeenCalledWith("b");
  });
});
