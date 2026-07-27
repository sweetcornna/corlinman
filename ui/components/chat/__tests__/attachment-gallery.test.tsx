import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { AttachmentGallery } from "@/components/chat/attachment-gallery";
import type { ChatAttachment } from "@/lib/chat/types";

function att(overrides: Partial<ChatAttachment>): ChatAttachment {
  return {
    id: "a1",
    kind: "document",
    name: "report.pdf",
    sizeBytes: 2048,
    ...overrides,
  };
}

describe("AttachmentGallery", () => {
  it("renders nothing when there are no attachments", () => {
    const { container } = render(<AttachmentGallery attachments={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders an image attachment as a clickable thumb that opens a lightbox", () => {
    render(
      <AttachmentGallery
        attachments={[
          att({
            id: "img1",
            kind: "image",
            name: "shot.png",
            remoteUrl: "/v1/files/img1",
          }),
        ]}
      />,
    );
    const thumb = screen.getByTestId("attachment-thumb");
    expect(thumb).toBeInTheDocument();
    // No file card for an image.
    expect(screen.queryByTestId("attachment-file-card")).not.toBeInTheDocument();

    // Clicking opens the fullscreen lightbox.
    fireEvent.click(thumb);
    expect(screen.getByTestId("attachment-lightbox")).toBeInTheDocument();
  });

  it("renders a non-image attachment as a file card with name + size", () => {
    render(
      <AttachmentGallery
        attachments={[
          att({ name: "spec.pdf", sizeBytes: 2048, remoteUrl: "/v1/files/a1" }),
        ]}
      />,
    );
    const card = screen.getByTestId("attachment-file-card");
    expect(card).toHaveTextContent("spec.pdf");
    // 2048 bytes → "2.0KB" from formatBytes.
    expect(card).toHaveTextContent("2.0KB");
  });

  it("adds a download link pointing at remoteUrl with the download attr", () => {
    render(
      <AttachmentGallery
        attachments={[
          att({ name: "spec.pdf", remoteUrl: "/v1/files/a1" }),
        ]}
      />,
    );
    const link = screen.getByTestId("attachment-download") as HTMLAnchorElement;
    expect(link).toHaveAttribute("href", "/v1/files/a1");
    expect(link).toHaveAttribute("download", "spec.pdf");
  });

  it("omits the download link when there is no remoteUrl", () => {
    render(
      <AttachmentGallery attachments={[att({ name: "spec.pdf" })]} />,
    );
    expect(screen.getByTestId("attachment-file-card")).toBeInTheDocument();
    expect(screen.queryByTestId("attachment-download")).not.toBeInTheDocument();
  });
});

describe("AttachmentGallery — audio", () => {
  it("renders an audio attachment with a real player, not a static icon", () => {
    render(
      <AttachmentGallery
        attachments={[
          att({
            id: "v1",
            kind: "audio",
            name: "reply.mp3",
            remoteUrl: "/v1/files/v1",
          }),
        ]}
      />,
    );
    const player = screen.getByTestId("attachment-audio-player");
    expect(player).toBeInTheDocument();
    expect(player).toHaveAttribute("src", "/v1/files/v1");
    expect(player).toHaveAttribute("controls");
    // It must NOT fall through to the generic file-chip branch.
    expect(screen.queryByTestId("attachment-file-card")).not.toBeInTheDocument();
  });

  it("still offers a download link next to the player", () => {
    render(
      <AttachmentGallery
        attachments={[
          att({ id: "v1", kind: "audio", name: "reply.mp3", remoteUrl: "/v1/files/v1" }),
        ]}
      />,
    );
    expect(screen.getByTestId("attachment-download")).toHaveAttribute(
      "href",
      "/v1/files/v1",
    );
  });

  it("falls back to a file chip when the audio has no fetchable url", () => {
    render(
      <AttachmentGallery
        attachments={[att({ id: "v1", kind: "audio", name: "reply.mp3" })]}
      />,
    );
    expect(screen.queryByTestId("attachment-audio-player")).not.toBeInTheDocument();
    expect(screen.getByTestId("attachment-file-card")).toBeInTheDocument();
  });

  it("mixes audio, images and documents without losing any of them", () => {
    render(
      <AttachmentGallery
        attachments={[
          att({ id: "i1", kind: "image", name: "s.png", remoteUrl: "/v1/files/i1" }),
          att({ id: "v1", kind: "audio", name: "r.mp3", remoteUrl: "/v1/files/v1" }),
          att({ id: "d1", kind: "document", name: "d.pdf", remoteUrl: "/v1/files/d1" }),
        ]}
      />,
    );
    expect(screen.getByTestId("attachment-thumb")).toBeInTheDocument();
    expect(screen.getByTestId("attachment-audio-player")).toBeInTheDocument();
    expect(screen.getByTestId("attachment-file-card")).toBeInTheDocument();
  });
});
