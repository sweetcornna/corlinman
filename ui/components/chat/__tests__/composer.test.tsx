import { afterEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

// Mock the file-upload client so the composer's addFiles flow runs
// against a controllable deferred instead of the network. Each test
// installs its own implementation via `uploadMock`.
const uploadMock = vi.fn();
vi.mock("@/lib/api/files", () => ({
  uploadChatFile: (file: File, onProgress?: (f: number) => void) =>
    uploadMock(file, onProgress),
}));

import { Composer } from "@/components/chat/composer";

function makeFile(name = "doc.pdf", type = "application/pdf"): File {
  return new File(["hello-bytes"], name, { type });
}

/** A promise plus its resolve/reject, for driving the upload mock. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

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
  afterEach(() => {
    uploadMock.mockReset();
  });

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

  it("toggles the emoji picker and inserts a glyph at the caret", () => {
    const { onSend } = renderComposer();
    const ta = screen.getByTestId("composer-textarea") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "hi" } });

    // Picker hidden until the emoji trigger is clicked.
    expect(screen.queryByTestId("emoji-picker")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("composer-emoji"));
    expect(screen.getByTestId("emoji-picker")).toBeInTheDocument();

    // Move caret to the end, then insert the first emoji cell.
    ta.setSelectionRange(2, 2);
    const firstEmoji = screen.getAllByTestId("emoji-item")[0];
    fireEvent.click(firstEmoji);

    // The textarea value now contains the original text plus a glyph; Enter sends it.
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledOnce();
    const [sentText] = onSend.mock.calls[0];
    expect(sentText.startsWith("hi")).toBe(true);
    expect(sentText.length).toBeGreaterThan(2);
  });

  it("emoji sticker entry opens the file input wiring", () => {
    renderComposer();
    fireEvent.click(screen.getByTestId("composer-emoji"));
    // The sticker cell is the last emoji-item (表情包 entry); clicking it
    // closes the picker (delegates to the existing file-input flow).
    const items = screen.getAllByTestId("emoji-item");
    fireEvent.click(items[items.length - 1]);
    expect(screen.queryByTestId("emoji-picker")).not.toBeInTheDocument();
  });

  it("uploads a picked file and fills remoteUrl/fileId on success", async () => {
    const def = deferred<{
      fileId: string;
      url: string;
      name: string;
      mime: string;
      size: number;
    }>();
    uploadMock.mockReturnValue(def.promise);

    const { onSend } = renderComposer();
    const input = screen.getByTestId("composer-file-input") as HTMLInputElement;
    const file = makeFile();
    fireEvent.change(input, { target: { files: [file] } });

    // The attachment appears immediately in the uploading state — send is
    // blocked until the upload settles.
    expect(uploadMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("composer-send")).toBeDisabled();

    // Resolve the upload; the attachment flips to a ready state and send unlocks.
    await act(async () => {
      def.resolve({
        fileId: "abc123",
        url: "/v1/files/abc123",
        name: "doc.pdf",
        mime: "application/pdf",
        size: file.size,
      });
      await def.promise;
    });

    await waitFor(() =>
      expect(screen.getByTestId("composer-send")).not.toBeDisabled(),
    );

    // Sending hands the resolved attachment (remoteUrl + fileId) to onSend.
    fireEvent.click(screen.getByTestId("composer-send"));
    expect(onSend).toHaveBeenCalledTimes(1);
    const [, atts] = onSend.mock.calls[0];
    expect(atts).toHaveLength(1);
    expect(atts[0]).toMatchObject({
      remoteUrl: "/v1/files/abc123",
      fileId: "abc123",
      uploading: false,
    });
    expect(atts[0].error).toBeUndefined();
  });

  it("shows an error state when an upload fails", async () => {
    const def = deferred<never>();
    uploadMock.mockReturnValue(def.promise);

    renderComposer();
    const input = screen.getByTestId("composer-file-input") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [makeFile()] } });

    await act(async () => {
      def.reject(new Error("boom"));
      await def.promise.catch(() => undefined);
    });

    // Localized failure text (zh-CN bundle in tests) surfaces on the chip,
    // and the attachment is no longer uploading.
    await screen.findByText("上传失败");
    // Send is no longer blocked by a stuck "uploading" attachment.
    await waitFor(() =>
      expect(screen.getByTestId("composer-send")).not.toBeDisabled(),
    );
  });

  it("blocks sending while an upload is still in flight", () => {
    // Never-resolving upload keeps the attachment in the uploading state.
    uploadMock.mockReturnValue(new Promise(() => {}));

    const { onSend } = renderComposer();
    const ta = screen.getByTestId("composer-textarea") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "with attachment" } });
    const input = screen.getByTestId("composer-file-input") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [makeFile()] } });

    // Even with text present, Enter must not send while uploading.
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByTestId("composer-send")).toBeDisabled();
  });
});
