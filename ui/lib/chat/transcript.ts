/**
 * Journal transcript → `ChatMessage[]` mapping for `/chat` history resume.
 *
 * Extracted from `app/(admin)/chat/page.tsx` so the turn-grouping and
 * id-stability contracts below are unit-testable without mounting the page.
 */

import { i18next } from "@/lib/i18n";
import { GATEWAY_BASE_URL } from "@/lib/api";
import type { TranscriptMessage } from "@/lib/api/sessions";
import type {
  ChatAttachment,
  ChatMessage,
  ToolCallState,
} from "@/lib/chat/types";

/** Map journal TranscriptMessage[] → ChatMessage[] so existing sessions
 *  (telegram / qq / scheduled) resume cleanly in /chat. Assistant rows
 *  with `tool_calls` rehydrate into ToolCallState[] so the bubble can
 *  render the historical tool invocations + their results (paired by
 *  the replay endpoint) — without this, tool-only assistant turns
 *  render as empty bubbles.
 *
 *  Consecutive assistant rows are merged into ONE ChatMessage: the journal
 *  writes a row per reasoning round, so a single agent turn with N tool
 *  rounds replays as N sparse bubbles — unlike live streaming, which keeps
 *  the whole turn in one bubble. Merging restores the live look (one bubble,
 *  tool trace stacked behind the collapse toggle). */
export function transcriptToChatMessages(
  transcript: TranscriptMessage[],
  sessionKey: string,
): ChatMessage[] {
  // Ids carry the session so two sessions with look-alike transcripts
  // can't collide on React keys while <ChatArea> stays mounted across
  // `sessionKey` changes (stale-DOM reuse on switch).
  const sid = sessionKey.replace(/[^a-zA-Z0-9]/g, "").slice(-12) || "s";
  // Maps the row at absolute index `i`. Identity must be deterministic
  // across reloads AND stable when an older page is PREPENDED (W5 "load
  // earlier"): index from the END of the list, so existing messages keep
  // their ids as the list grows upward. (The old
  // `hist_${i}_${Date.now()-fallback}` baked a load-time timestamp in,
  // so every refetch re-keyed the whole list and React rebuilt the DOM,
  // losing scroll position.)
  const mapRow = (m: TranscriptMessage, i: number): ChatMessage => {
    const rid = transcript.length - i;
    const created = Number.isFinite(Date.parse(m.ts))
      ? Date.parse(m.ts)
      : Date.now() - (transcript.length - i) * 1000;
    const rawTcs = m.tool_calls ?? [];
    const toolCalls: ToolCallState[] = rawTcs.map((tc, j) => ({
      callId: tc.id?.trim() ? tc.id : `hist_${sid}_r${rid}_${j}`,
      toolName: tc.function?.name ?? i18next.t("chat.unknownToolName"),
      argsJson: tc.function?.arguments ?? "{}",
      status: tc.result !== undefined ? "settled" : "ok",
      resultPreview: tc.result,
    }));
    // W3 — journaled attachment metadata → renderable cards. Relative
    // /v1/files urls get the gateway prefix so dev (separate origins)
    // and prod (same origin, empty prefix) both resolve.
    const attachments: ChatAttachment[] = (m.attachments ?? []).map(
      (a, k) => {
        const size = Number(a.size ?? a.size_bytes ?? 0);
        return {
          id: `hist_${sid}_r${rid}_att_${k}`,
          kind:
            a.kind === "image" || a.kind === "audio" || a.kind === "video"
              ? a.kind
              : "document",
          name: a.name || a.url?.split("/").pop() || "attachment",
          mime: a.mime,
          sizeBytes: Number.isFinite(size) && size > 0 ? size : 0,
          remoteUrl: a.url
            ? a.url.startsWith("/")
              ? `${GATEWAY_BASE_URL}${a.url}`
              : a.url
            : undefined,
        };
      },
    );
    return {
      id: `hist_${sid}_r${rid}_${m.role}`,
      role: m.role,
      content: m.content,
      createdAt: created,
      toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
      attachments: attachments.length > 0 ? attachments : undefined,
    };
  };

  // Merge runs of consecutive assistant rows. The merged message inherits
  // id/createdAt from the run's FIRST row — that row's rid is end-anchored
  // like every other row's, so prepending an older page cannot re-key the
  // groups already on screen (only rows ABOVE them gain new ids). Tool-call
  // and attachment fallback ids stay per-row (they embed the source row's
  // rid), so merging never collides two rows' call ids.
  const out: ChatMessage[] = [];
  for (let i = 0; i < transcript.length; i += 1) {
    const first = mapRow(transcript[i], i);
    if (transcript[i].role !== "assistant") {
      out.push(first);
      continue;
    }
    const contents: string[] = first.content.trim() ? [first.content] : [];
    let toolCalls = first.toolCalls ?? [];
    let attachments = first.attachments ?? [];
    while (
      i + 1 < transcript.length &&
      transcript[i + 1].role === "assistant"
    ) {
      i += 1;
      const next = mapRow(transcript[i], i);
      if (next.content.trim()) contents.push(next.content);
      if (next.toolCalls) toolCalls = toolCalls.concat(next.toolCalls);
      if (next.attachments) attachments = attachments.concat(next.attachments);
    }
    out.push({
      ...first,
      content: contents.join("\n\n"),
      toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
      attachments: attachments.length > 0 ? attachments : undefined,
    });
  }
  return out;
}
