/**
 * Chat API client — talks to the gateway via two complementary surfaces:
 *
 *   1. `POST /v1/chat/completions` — OpenAI-compatible streaming endpoint.
 *      Returns SSE deltas (`data: {choices: [{delta: {...}}]}`). We parse
 *      the raw fetch ReadableStream rather than using EventSource because
 *      EventSource is GET-only.
 *
 *   2. `/admin/sessions/*` admin surface — session list, history,
 *      metadata PATCH, cancel, approvals, live event subscription.
 *
 * Streaming token deltas + journal events are merged into a single ordered
 * `ChatEvent` stream by `@/lib/chat/event-merger` before they reach the UI.
 */

import {
  CorlinmanApiError,
  GATEWAY_BASE_URL,
  apiFetch,
} from "@/lib/api";
import type { ChatAttachment, ChatConversation } from "@/lib/chat/types";

/* ------------------------------------------------------------------ */
/*                       /v1/chat/completions                          */
/* ------------------------------------------------------------------ */

export interface ChatCompletionMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | ChatCompletionContentPart[];
  /** Tool result correlation. */
  tool_call_id?: string;
  /** Tool calls emitted by an assistant message in history. */
  tool_calls?: ChatCompletionToolCall[];
  /** Set on user messages so the agent knows which conversation thread to
   *  attach this turn to in the journal. */
  name?: string;
}

export interface ChatCompletionContentPart {
  type: "text" | "image_url" | "input_audio" | "file";
  text?: string;
  image_url?: { url: string };
  input_audio?: { data: string; format: string };
  file?: { file_id?: string; file_data?: string; filename?: string };
}

export interface ChatCompletionToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface ChatCompletionRequest {
  model: string;
  messages: ChatCompletionMessage[];
  stream: true;
  /**
   * Pinned conversation id. Sent at the top level (not inside
   * `metadata`) because the gateway's `ChatRequest` pydantic model
   * accepts it there; the metadata bag is `extra="allow"` and is
   * silently dropped on the way to the reasoning loop. Without this
   * field every web turn lands in the journal under an empty
   * session_key, which breaks the /admin/sessions sidebar.
   */
  session_key?: string;
  /** Hermes extension: agent / persona binding. The gateway reads
   *  these from the metadata bag. */
  metadata?: Record<string, string>;
  temperature?: number;
  max_tokens?: number;
  tools?: ChatCompletionToolDef[];
}

export interface ChatCompletionToolDef {
  type: "function";
  function: {
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
  };
}

/**
 * Raw SSE chunk from `/v1/chat/completions`. We only consume `delta` and
 * `finish_reason` — everything else (id, model, choices wrapper) is for
 * compatibility with OpenAI-flavoured clients.
 */
export interface ChatCompletionChunk {
  id?: string;
  object?: "chat.completion.chunk";
  created?: number;
  model?: string;
  choices?: Array<{
    index: number;
    delta: {
      role?: "assistant";
      content?: string;
      reasoning_content?: string;
      tool_calls?: Array<{
        index: number;
        id?: string;
        type?: "function";
        function?: { name?: string; arguments?: string };
      }>;
    };
    finish_reason?: string | null;
  }>;
  /** Hermes extension: surface the journal turn id so the UI can hook the
   *  live event stream, and agent-produced attachments (send_attachment /
   *  image_generate) registered into the gateway file store mid-turn. */
  corlinman?: {
    turn_id?: string;
    session_key?: string;
    attachment?: {
      kind?: string;
      url?: string;
      name?: string;
      mime?: string;
      size?: number;
      size_bytes?: number;
    };
  };
  /** Mid-stream failure payload. The gateway emits it alongside a
   *  `finish_reason: "error"` choice (W1a contract); older gateways sent
   *  it as a bare frame with no `choices` at all. Either way the merger
   *  must fold it into a `turn-errored` event — ignoring it left the
   *  pending bubble stuck "loading" forever. */
  error?: { code?: string; reason?: string; message?: string };
}

/** Milliseconds of total wire silence before we declare the stream dead.
 *  The gateway heartbeats every 10s even while a slow tool runs (W1a), so
 *  45s of *nothing* — not even a comment frame — means the backend or the
 *  path to it is gone and the turn would otherwise hang forever. */
const STREAM_STALL_TIMEOUT_MS = 45_000;

/** Stream chat completions; yields parsed chunks until the stream closes
 *  or the AbortSignal fires. */
export async function* streamChatCompletions(
  body: ChatCompletionRequest,
  signal?: AbortSignal,
): AsyncGenerator<ChatCompletionChunk, void, void> {
  const res = await fetch(`${GATEWAY_BASE_URL}/v1/chat/completions`, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    throw new CorlinmanApiError(
      text || `chat stream failed: ${res.status}`,
      res.status,
      res.headers.get("x-request-id") ?? undefined,
    );
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const parseFrame = (frame: string): ChatCompletionChunk[] => {
    const chunks: ChatCompletionChunk[] = [];
    for (const line of frame.split("\n")) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      try {
        chunks.push(JSON.parse(data) as ChatCompletionChunk);
      } catch {
        // Don't fail the turn over one mangled frame, but don't hide it
        // either — silent drops made truncated replies undebuggable.
        console.warn(
          "chat stream: dropped malformed SSE chunk",
          data.slice(0, 200),
        );
      }
    }
    return chunks;
  };

  try {
    while (true) {
      // Watchdog on the raw read: heartbeat comments reset it too (they
      // arrive as bytes), so it only fires when the wire is truly dead —
      // a crashed backend used to leave the turn loading forever.
      let stallTimer: ReturnType<typeof setTimeout> | undefined;
      let readResult: ReadableStreamReadResult<Uint8Array>;
      try {
        readResult = await Promise.race([
          reader.read(),
          new Promise<never>((_, reject) => {
            stallTimer = setTimeout(
              () =>
                reject(
                  new CorlinmanApiError(
                    `chat stream stalled: no data for ${
                      STREAM_STALL_TIMEOUT_MS / 1000
                    }s`,
                    0,
                  ),
                ),
              STREAM_STALL_TIMEOUT_MS,
            );
          }),
        ]);
      } finally {
        clearTimeout(stallTimer);
      }
      const { value, done } = readResult;
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line. Split on \n\n.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        yield* parseFrame(frame);
      }
    }
    // Flush the tail: a final frame without the trailing blank line is
    // still data (and the decoder may hold a partial UTF-8 sequence) —
    // dropping it silently lost the last chunk of some streams.
    buffer += decoder.decode();
    if (buffer.trim()) {
      for (const frame of buffer.split("\n\n")) {
        yield* parseFrame(frame);
      }
    }
  } finally {
    // Cancel (not just release) so a stall/abort exit actually tears the
    // HTTP body down instead of leaving the connection half-open.
    try {
      await reader.cancel();
    } catch {
      // already closed
    }
    try {
      reader.releaseLock();
    } catch {
      // already released
    }
  }
}

/* ------------------------------------------------------------------ */
/*                       /admin/sessions/*                            */
/* ------------------------------------------------------------------ */

export interface SessionMetadataPatch {
  title?: string | null;
  pinned?: boolean;
  archived?: boolean;
}

/** Mirrors `SessionSummaryOut` plus the Wave 1 metadata extension. */
export interface SessionSummaryWithMeta {
  session_key: string;
  last_message_at: number;
  last_seen_at_ms?: number;
  message_count: number;
  title?: string | null;
  pinned?: boolean;
  archived?: boolean;
}

export async function listChatSessions(): Promise<ChatConversation[]> {
  const out = await apiFetch<{ sessions: SessionSummaryWithMeta[] }>(
    "/admin/sessions",
  );
  return (out.sessions ?? []).map((s) => ({
    sessionKey: s.session_key,
    title: s.title ?? null,
    pinned: Boolean(s.pinned),
    archived: Boolean(s.archived),
    lastMessageAt: s.last_message_at,
    messageCount: s.message_count,
  }));
}

export async function patchChatSession(
  sessionKey: string,
  patch: SessionMetadataPatch,
): Promise<ChatConversation> {
  const out = await apiFetch<SessionSummaryWithMeta>(
    `/admin/sessions/${encodeURIComponent(sessionKey)}`,
    { method: "PATCH", body: patch },
  );
  return {
    sessionKey: out.session_key,
    title: out.title ?? null,
    pinned: Boolean(out.pinned),
    archived: Boolean(out.archived),
    lastMessageAt: out.last_message_at,
    messageCount: out.message_count,
  };
}

export async function deleteChatSession(sessionKey: string): Promise<void> {
  await apiFetch(`/admin/sessions/${encodeURIComponent(sessionKey)}`, {
    method: "DELETE",
  });
}

export interface CancelSessionResult {
  status: "cancelled" | "not_running" | "unknown_session";
  turn_id?: string;
}

export async function cancelChatSession(
  sessionKey: string,
): Promise<CancelSessionResult> {
  return apiFetch<CancelSessionResult>(
    `/admin/sessions/${encodeURIComponent(sessionKey)}/cancel`,
    { method: "POST", body: {} },
  );
}

/* ------------------------------------------------------------------ */
/*                         Approval API                               */
/* ------------------------------------------------------------------ */

export interface ApprovalRequest {
  approved: boolean;
  scope?: "once" | "session" | "always";
  deny_message?: string;
}

/** Mirrors the existing `/v1/chat/completions/{turn_id}/approve` route. */
export async function submitApproval(
  turnId: string,
  body: ApprovalRequest,
): Promise<void> {
  await apiFetch(
    `/v1/chat/completions/${encodeURIComponent(turnId)}/approve`,
    { method: "POST", body },
  );
}

/* ------------------------------------------------------------------ */
/*                          Attachments                               */
/* ------------------------------------------------------------------ */

const ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024;
const ATTACHMENT_ALLOWED_PREFIXES = ["image/", "audio/", "video/", "application/", "text/"];

export function validateAttachment(file: File): string | null {
  if (file.size <= 0) {
    return "attachment_empty_file";
  }
  if (file.size > ATTACHMENT_MAX_BYTES) {
    return `${file.name}: exceeds 50MB`;
  }
  if (!ATTACHMENT_ALLOWED_PREFIXES.some((p) => file.type.startsWith(p))) {
    return `${file.name}: mime ${file.type || "unknown"} not allowed`;
  }
  return null;
}

export function attachmentKindFromMime(mime: string): ChatAttachment["kind"] {
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("audio/")) return "audio";
  if (mime.startsWith("video/")) return "video";
  return "document";
}
