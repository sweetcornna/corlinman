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
  /** Hermes-specific metadata. The gateway pulls `session_key` /
   *  `persona_id` / `agent_id` out of here before delegating to the
   *  reasoning loop. */
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
   *  live event stream. */
  corlinman?: { turn_id?: string; session_key?: string };
}

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

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line. Split on \n\n.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (!data || data === "[DONE]") continue;
          try {
            yield JSON.parse(data) as ChatCompletionChunk;
          } catch {
            // ignore malformed chunk
          }
        }
      }
    }
  } finally {
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
  if (file.size > ATTACHMENT_MAX_BYTES) {
    return `${file.name}: exceeds 50MB`;
  }
  if (!ATTACHMENT_ALLOWED_PREFIXES.some((p) => file.type.startsWith(p))) {
    return `${file.name}: mime ${file.type || "unknown"} not allowed`;
  }
  return null;
}

/**
 * Reads a file into a data URL. Used for small image inlining when we have
 * no separate upload endpoint; the gateway's chat completions API accepts
 * data URLs in content parts.
 */
export function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

export function attachmentKindFromMime(mime: string): ChatAttachment["kind"] {
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("audio/")) return "audio";
  if (mime.startsWith("video/")) return "video";
  return "document";
}
