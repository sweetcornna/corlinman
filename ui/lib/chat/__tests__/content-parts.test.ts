import { describe, expect, it } from "vitest";

import { buildMessageContent } from "@/lib/chat/content-parts";
import type { ChatAttachment } from "@/lib/chat/types";

function att(overrides: Partial<ChatAttachment>): ChatAttachment {
  return {
    id: "a1",
    kind: "image",
    name: "pic.png",
    sizeBytes: 10,
    ...overrides,
  };
}

describe("buildMessageContent", () => {
  it("returns the plain string when there are no attachments", () => {
    expect(buildMessageContent("hello")).toBe("hello");
    expect(buildMessageContent("hello", [])).toBe("hello");
  });

  it("builds text + image_url parts from an uploaded image", () => {
    const parts = buildMessageContent("look", [
      att({ fileId: "f".repeat(26) }),
    ]);
    expect(parts).toEqual([
      { type: "text", text: "look" },
      {
        type: "image_url",
        image_url: { url: `/v1/files/${"f".repeat(26)}` },
      },
    ]);
  });

  it("maps non-image attachments to file parts with the filename", () => {
    const parts = buildMessageContent("read", [
      att({ kind: "document", name: "报告.pdf", fileId: "d".repeat(26) }),
    ]);
    expect(parts).toEqual([
      { type: "text", text: "read" },
      {
        type: "file",
        file: { file_id: `/v1/files/${"d".repeat(26)}`, filename: "报告.pdf" },
      },
    ]);
  });

  it("drops errored, still-uploading and never-uploaded attachments", () => {
    expect(
      buildMessageContent("hi", [
        att({ error: "failed" }),
        att({ uploading: true, fileId: "x".repeat(26) }),
        att({ previewUrl: "blob:local-only" }),
      ]),
    ).toBe("hi");
  });

  it("falls back to remoteUrl when fileId is absent", () => {
    const parts = buildMessageContent("see", [
      att({ remoteUrl: "/v1/files/" + "e".repeat(26) }),
    ]);
    expect(Array.isArray(parts)).toBe(true);
    expect((parts as Array<{ image_url?: { url: string } }>)[1].image_url).toEqual({
      url: "/v1/files/" + "e".repeat(26),
    });
  });

  it("omits the text part for an attachment-only message", () => {
    const parts = buildMessageContent("", [att({ fileId: "g".repeat(26) })]);
    expect(parts).toEqual([
      {
        type: "image_url",
        image_url: { url: `/v1/files/${"g".repeat(26)}` },
      },
    ]);
  });
});
