/**
 * Outgoing-request half of the W3 attachment pipeline: convert a
 * `ChatMessage` (text + `ChatAttachment[]`) into the OpenAI
 * `string | ChatCompletionContentPart[]` content shape the gateway
 * accepts.
 *
 * The gateway resolves `/v1/files/{id}` references into provider-ready
 * bytes server-side, so the wire payload stays slim — no base64 here.
 * Attachments that never finished uploading (no `remoteUrl`/`fileId`)
 * or errored are dropped: a local `blob:` preview URL is meaningless to
 * the server.
 */

import type { ChatCompletionContentPart } from "@/lib/api/chat";
import type { ChatAttachment } from "@/lib/chat/types";

import { GATEWAY_BASE_URL } from "@/lib/api";

/** Server-usable reference for an attachment, or null when it has none. */
function attachmentRef(a: ChatAttachment): string | null {
  if (a.fileId) return `/v1/files/${a.fileId}`;
  if (a.remoteUrl) {
    // Strip a dev-time absolute gateway prefix back to the path form
    // the backend recognises.
    if (GATEWAY_BASE_URL && a.remoteUrl.startsWith(GATEWAY_BASE_URL)) {
      return a.remoteUrl.slice(GATEWAY_BASE_URL.length) || a.remoteUrl;
    }
    return a.remoteUrl;
  }
  return null;
}

/** Build the request `content` for one message. Returns the plain string
 *  when there is nothing attachable — keeping text-only turns identical
 *  to the pre-W3 wire format. */
export function buildMessageContent(
  content: string,
  attachments?: ChatAttachment[],
): string | ChatCompletionContentPart[] {
  const usable = (attachments ?? []).filter((a) => !a.error && !a.uploading);
  if (usable.length === 0) return content;

  const parts: ChatCompletionContentPart[] = [];
  if (content) parts.push({ type: "text", text: content });
  let attachable = 0;
  for (const a of usable) {
    const ref = attachmentRef(a);
    if (!ref) continue;
    attachable += 1;
    if (a.kind === "image") {
      parts.push({ type: "image_url", image_url: { url: ref } });
    } else {
      // Audio / video / documents all travel as generic file parts; the
      // gateway inlines the stored bytes and the agent forwards them to
      // providers that support the modality.
      parts.push({ type: "file", file: { file_id: ref, filename: a.name } });
    }
  }
  // A text-only parts array is just the string with extra ceremony —
  // keep the legacy wire shape unless something attachable made it in.
  return attachable > 0 ? parts : content;
}
