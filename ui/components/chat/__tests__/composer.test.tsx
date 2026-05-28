import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { Composer } from "@/components/chat/composer";

function renderComposer(overrides: Partial<React.ComponentProps<typeof Composer>> = {}) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  render(
    <Composer
      isStreaming={false}
      modelLabel="gpt-4o"
      onSend={onSend}
      onStop={onStop}
      {...overrides}
    />,
  );
  return { onSend, onStop };
}

describe("Composer", () => {
  it("sends on Enter, inserts newline on Shift+Enter", () => {
    const { onSend } = renderComposer();
    const ta = screen.getByTestId("composer-textarea") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "hello" } });
    // Shift+Enter should NOT submit
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
    // Plain Enter should submit
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello", []);
  });

  it("Send button disabled when empty and no attachments", () => {
    renderComposer();
    expect(screen.getByTestId("composer-send")).toBeDisabled();
  });

  it("swaps Send for Stop while streaming and fires onStop on click", () => {
    const { onStop } = renderComposer({ isStreaming: true });
    expect(screen.queryByTestId("composer-send")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("composer-stop"));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it("opens the slash menu when input begins with /", () => {
    renderComposer();
    const ta = screen.getByTestId("composer-textarea") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "/cle" } });
    expect(screen.getByTestId("slash-menu")).toBeInTheDocument();
    // /clear should be in the list
    expect(screen.getByTestId("slash-menu")).toHaveTextContent("/clear");
  });

  it("does not send when text is only whitespace", () => {
    const { onSend } = renderComposer();
    const ta = screen.getByTestId("composer-textarea") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "   " } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("opens model picker when the model pill is clicked", () => {
    const onOpenModelPicker = vi.fn();
    renderComposer({ onOpenModelPicker });
    fireEvent.click(screen.getByTestId("composer-model"));
    expect(onOpenModelPicker).toHaveBeenCalledOnce();
  });
});
