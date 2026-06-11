import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MarkdownMessage } from "../markdown-message";

describe("MarkdownMessage (Spatial Glass pipeline)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

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

  it("does not close the lightbox when the image itself is clicked", () => {
    render(<MarkdownMessage content={"![pic](https://example.com/a.png)"} />);
    fireEvent.click(screen.getByTestId("md-image").closest("button")!);
    expect(screen.getByTestId("md-image-lightbox")).toBeInTheDocument();

    // Clicking the zoomed image must keep the dialog open (only the
    // backdrop / close button / Esc dismiss it).
    fireEvent.click(screen.getByTestId("md-image-lightbox-img"));
    expect(screen.getByTestId("md-image-lightbox")).toBeInTheDocument();

    // The backdrop still closes it.
    fireEvent.click(screen.getByTestId("md-image-lightbox"));
    expect(screen.queryByTestId("md-image-lightbox")).not.toBeInTheDocument();
  });

  it("focuses the close button on open and returns focus to the trigger on close", () => {
    render(<MarkdownMessage content={"![pic](https://example.com/a.png)"} />);
    const trigger = screen.getByTestId("md-image").closest("button")!;
    fireEvent.click(trigger);

    const closeBtn = screen.getByRole("button", { name: /关闭图片预览|close image preview/i });
    expect(closeBtn).toHaveFocus();

    fireEvent.click(closeBtn);
    expect(screen.queryByTestId("md-image-lightbox")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("renders inline and block math as KaTeX nodes", () => {
    const { container } = render(
      <MarkdownMessage content={"Inline $x^2$ and block:\n\n$$\\int_0^1 x\\,dx$$"} />,
    );
    // rehype-katex emits a `.katex` wrapper for each rendered expression.
    const katex = container.querySelectorAll(".katex");
    expect(katex.length).toBeGreaterThanOrEqual(2);
    // The superscript exponent should make it into the output.
    expect(container.textContent).toContain("2");
  });

  it("still strips dangerous HTML even with KaTeX enabled", () => {
    const { container } = render(
      <MarkdownMessage content={"<img src=x onerror=alert(1)>\n\n$x$ safe"} />,
    );
    // sanitize runs before katex: the inline-event handler must never survive
    // (react-markdown drops the raw <img> entirely, so there is no onerror
    // attribute anywhere in the rendered tree).
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.innerHTML).not.toContain("onerror");
    // …while math still renders.
    expect(container.querySelector(".katex")).not.toBeNull();
  });

  it("does not flash 'copied' or throw when the clipboard write fails", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    render(<MarkdownMessage content={"```ts\nconst x = 1;\n```"} />);
    const copyBtn = screen.getByRole("button", { name: /复制代码|copy code/i });
    // Must not throw synchronously.
    expect(() => fireEvent.click(copyBtn)).not.toThrow();

    await waitFor(() => expect(warn).toHaveBeenCalled());
    // The success label ("已复制" / "Copied") must never appear.
    expect(screen.queryByText(/已复制|^Copied$/)).not.toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith("const x = 1;");
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
