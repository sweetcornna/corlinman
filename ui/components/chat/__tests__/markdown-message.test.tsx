import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MarkdownMessage } from "../markdown-message";

describe("MarkdownMessage (Spatial Glass pipeline)", () => {
  it("renders GFM tables", () => {
    render(
      <MarkdownMessage content={"| a | b |\n| --- | --- |\n| 1 | 2 |"} />,
    );
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("renders GFM strikethrough", () => {
    render(<MarkdownMessage content={"~~gone~~"} />);
    expect(screen.getByText("gone").tagName.toLowerCase()).toBe("del");
  });

  it("renders images with a zoomable lightbox", () => {
    render(<MarkdownMessage content={"![pic](https://example.com/a.png)"} />);
    const img = screen.getByTestId("md-image");
    expect(img).toHaveAttribute("src", "https://example.com/a.png");

    fireEvent.click(img.closest("button")!);
    expect(screen.getByTestId("md-image-lightbox")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("md-image-lightbox")).not.toBeInTheDocument();
  });

  it("shows the streaming caret and plain (unhighlighted) code while streaming", () => {
    render(
      <MarkdownMessage streaming content={"```ts\nconst x = 1;\n```"} />,
    );
    expect(screen.getByTestId("md-cursor")).toBeInTheDocument();
    const block = screen.getByTestId("md-codeblock");
    expect(block.querySelector("pre code")).toHaveTextContent("const x = 1;");
  });

  it("keeps the artifact CTA for artifact languages", () => {
    render(
      <MarkdownMessage
        content={"```html\n<h1>hi</h1>\n```"}
        onOpenArtifact={() => {}}
      />,
    );
    expect(screen.getByTestId("md-codeblock-open-artifact")).toBeInTheDocument();
  });

  it("sanitizes script tags out of markdown", () => {
    const { container } = render(
      <MarkdownMessage content={"<script>window.x=1</script>\n\nsafe"} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("safe")).toBeInTheDocument();
  });
});
