"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { Download } from "@/components/icons";

import { cn } from "@/lib/utils";
import { ArtifactPanel } from "@/components/chat/artifact-panel";
import { Composer } from "@/components/chat/composer";
import type { MentionCandidate } from "@/components/chat/composer-mention-menu";
import { ChatEmptyState } from "@/components/chat/empty-state";
import { ConversationSearch } from "@/components/chat/conversation-search";
import { MessageList } from "@/components/chat/message-list";
import { PresenceOrb } from "@/components/ui/presence-orb";
import { AgentPicker } from "@/components/playground/agent-picker";
import { useChatStream } from "@/lib/chat/use-chat-stream";
import { modelSupportsReasoningEffort } from "@/lib/chat/reasoning-effort";
import type { ReasoningEffort } from "@/lib/api/chat";
import {
  deriveArtifactKind,
  deriveArtifactTitle,
  useArtifacts,
} from "@/lib/chat/artifacts";
import type { ChatMessage, ChatConversation } from "@/lib/chat/types";

interface ChatAreaProps {
  sessionKey: string;
  model: string;
  conversation?: ChatConversation | null;
  initialHistory?: ChatMessage[];
  agentId?: string;
  personaId?: string;
  personaLabel?: string;
  mentionCandidates?: MentionCandidate[];
  onOpenModelPicker?: () => void;
  onOpenPersonaPicker?: () => void;
  imageModelLabel?: string;
  onOpenImageModelPicker?: () => void;
  reasoningEffort?: ReasoningEffort;
  onReasoningEffortChange?: (effort: ReasoningEffort) => void;
  modelProvider?: string | null;
  modelTarget?: string | null;
  onAgentChange?: (agentId: string | null) => void;
  showActionTrace?: boolean;
  /** W5 — an older history page exists; show the "load earlier" pill. */
  hasEarlier?: boolean;
  loadingEarlier?: boolean;
  onLoadEarlier?: () => void;
}

function genSessionKey(): string {
  const r = Math.random().toString(36).slice(2, 10);
  return `corlinman:${Date.now().toString(36)}:${r}`;
}

export function modelAllowsXHighReasoningEffort(
  model: string,
  provider?: string | null,
  targetModel?: string | null,
): boolean {
  const normalizedProvider = provider?.trim().toLowerCase() ?? "";
  if (normalizedProvider.includes("codex")) return true;
  return (
    model.trim().toLowerCase().includes("codex") ||
    (targetModel?.trim().toLowerCase().includes("codex") ?? false)
  );
}

export function effectiveReasoningEffortForModel(
  model: string,
  reasoningEffort: ReasoningEffort,
  provider?: string | null,
  targetModel?: string | null,
): ReasoningEffort | undefined {
  const id = model.trim().toLowerCase();
  if (!id) return undefined;
  const targetId = targetModel?.trim().toLowerCase() ?? "";
  const normalized =
    !modelAllowsXHighReasoningEffort(id, provider, targetId) &&
    reasoningEffort === "xhigh"
      ? "high"
      : reasoningEffort;
  if (modelSupportsReasoningEffort(id)) return normalized;
  if (targetId && modelSupportsReasoningEffort(targetId)) return normalized;
  const normalizedProvider = provider?.trim().toLowerCase() ?? "";
  if (normalizedProvider.includes("codex")) return normalized;
  return undefined;
}

/** Role → human-readable Markdown heading label. */
function exportRoleLabel(
  role: ChatMessage["role"],
  t: (key: string) => string,
): string {
  switch (role) {
    case "user":
      return t("chat.roleYou");
    case "assistant":
      return t("chat.roleAssistant");
    default:
      return t("chat.roleSystem");
  }
}

/**
 * Serialize a settled transcript to a Markdown document. Pure + client-side:
 * a role heading, an ISO-ish timestamp, the message body, and — when present —
 * a compact summary line of the tool calls fired in that turn. The streaming
 * `pendingMessage` is intentionally excluded by the caller (it's not settled).
 */
export function exportTranscriptMarkdown(
  title: string,
  messages: ChatMessage[],
  t: (key: string) => string,
): string {
  const lines: string[] = [`# ${title || "Conversation"}`, ""];
  for (const m of messages) {
    const when = Number.isFinite(m.createdAt)
      ? new Date(m.createdAt).toISOString()
      : "";
    lines.push(`## ${exportRoleLabel(m.role, t)}${when ? ` — ${when}` : ""}`);
    lines.push("");
    if (m.content.trim()) {
      lines.push(m.content.trim());
      lines.push("");
    }
    if (m.toolCalls && m.toolCalls.length > 0) {
      const names = m.toolCalls.map((tc) => tc.toolName).join(", ");
      lines.push(`> ${t("chat.exportToolCalls")}: ${names}`);
      lines.push("");
    }
  }
  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trimEnd() + "\n";
}

/** Turn a conversation title into a filesystem-friendly `.md` filename. */
function exportFilename(title: string): string {
  const base =
    title
      .trim()
      .replace(/[\\/:*?"<>|]+/g, "-")
      .replace(/\s+/g, " ")
      .slice(0, 80)
      .trim() || "conversation";
  return `${base}.md`;
}

export function ChatArea({
  sessionKey,
  model,
  conversation,
  initialHistory,
  agentId,
  personaId,
  personaLabel,
  mentionCandidates,
  onOpenModelPicker,
  onOpenPersonaPicker,
  imageModelLabel,
  onOpenImageModelPicker,
  reasoningEffort = "medium",
  onReasoningEffortChange,
  modelProvider,
  modelTarget,
  onAgentChange,
  showActionTrace = true,
  hasEarlier,
  loadingEarlier,
  onLoadEarlier,
}: ChatAreaProps) {
  const router = useRouter();
  const { t } = useTranslation();
  const allowsCodexReasoning = modelAllowsXHighReasoningEffort(
    model,
    modelProvider,
    modelTarget,
  );
  const effectiveReasoningEffort = effectiveReasoningEffortForModel(
    model,
    reasoningEffort,
    modelProvider,
    modelTarget,
  );
  const normalizedReasoningEffort =
    !allowsCodexReasoning && reasoningEffort === "xhigh"
      ? "high"
      : reasoningEffort;
  const chat = useChatStream({
    sessionKey,
    model,
    reasoningEffort: effectiveReasoningEffort,
    agentId,
    personaId,
  });
  const arts = useArtifacts();
  const [reply, setReply] = React.useState<{
    authorLabel: string;
    preview: string;
  } | null>(null);

  // `len: -1` marks a provisional hydration: the session changed but the
  // transcript query hasn't resolved yet, so we cleared the previous
  // session's thread and are still waiting for real history. Any
  // resolved history (even an equal-length / empty one) replaces it.
  const hydratedRef = React.useRef<{ key: string; len: number } | null>(null);
  React.useEffect(() => {
    if (initialHistory === undefined) {
      // Transcript still loading. Pre-fix this path hydrated `[]` as if
      // it were real history and the ref guard then suppressed the real
      // transcript when it landed — returning to an old conversation
      // showed a blank thread until a manual refresh.
      if (hydratedRef.current?.key !== sessionKey) {
        chat.hydrate([]);
        hydratedRef.current = { key: sessionKey, len: -1 };
      }
      return;
    }
    // Never clobber an in-flight turn — the stream owns the thread
    // state; the post-turn render re-evaluates this effect anyway.
    if (chat.isStreaming) return;
    const desiredLen = initialHistory.length;
    if (
      hydratedRef.current &&
      hydratedRef.current.key === sessionKey &&
      hydratedRef.current.len === desiredLen
    ) {
      return;
    }
    chat.hydrate(initialHistory);
    hydratedRef.current = { key: sessionKey, len: desiredLen };
    // The turn the user navigated away from may still be generating
    // server-side (generation is not tied to the browser connection).
    // Reattach: rebuild the pending bubble from the journal backlog and
    // tail the live stream. No-op when nothing is in flight.
    void chat.resumeInFlight();
  }, [sessionKey, initialHistory, chat]);

  const handlePickSuggestion = React.useCallback(
    (text: string) => {
      chat.sendMessage(text).catch((err) => {
        console.warn("chat send failed", err);
      });
    },
    [chat],
  );

  const handleEdit = React.useCallback(
    (messageId: string, newContent: string) => {
      // Return the promise so the bubble can await it and only leave edit
      // mode on success. We still log, but re-throw so the bubble's failure
      // path keeps the draft in edit mode (no silent swallow).
      return chat.editAndRerun(messageId, newContent).catch((err) => {
        console.warn("chat edit-rerun failed", err);
        throw err;
      });
    },
    [chat],
  );

  const handleBranch = React.useCallback(
    (messageId: string) => {
      const slice = chat.sliceUntil(messageId);
      const newKey = genSessionKey();
      try {
        sessionStorage.setItem(
          `corlinman:chat:branch:${newKey}`,
          JSON.stringify(slice),
        );
      } catch {
        /* ignore */
      }
      router.push(`/chat?session=${encodeURIComponent(newKey)}&branched=1`);
    },
    [chat, router],
  );

  const handleReply = React.useCallback(
    (messageId: string) => {
      const all = chat.pendingMessage
        ? [...chat.messages, chat.pendingMessage]
        : chat.messages;
      const m = all.find((x) => x.id === messageId);
      if (!m) return;
      const preview = m.content.slice(0, 120);
      const authorLabel =
        m.role === "user"
          ? t("chat.roleYou")
          : m.role === "assistant"
            ? t("chat.roleAssistant")
            : t("chat.roleSystem");
      setReply({ authorLabel, preview });
    },
    [chat.messages, chat.pendingMessage, t],
  );

  const jumpToMessage = React.useCallback((messageId: string) => {
    const el = document.getElementById(`chat-msg-${messageId}`);
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, []);

  const handleOpenArtifact = React.useCallback(
    (language: string, source: string) => {
      const messageId =
        chat.pendingMessage?.id ??
        chat.messages[chat.messages.length - 1]?.id ??
        "unknown";
      const id = `${messageId}:${language || "txt"}:${source.length}`;
      arts.open({
        id,
        kind: deriveArtifactKind(language),
        title: deriveArtifactTitle(language, source),
        language,
        source,
        messageId,
      });
    },
    [arts, chat.messages, chat.pendingMessage],
  );

  const title =
    conversation?.title ??
    (chat.messages[0]?.role === "user"
      ? chat.messages[0].content.slice(0, 60)
      : t("chat.headerNewConversation"));

  const { inputTokens, outputTokens, costUsd } = chat.totals;
  const hasUsage = inputTokens + outputTokens > 0 || costUsd > 0;

  // W6 ⑤ — fully client-side conversation export. Serializes the settled
  // transcript (NOT the in-flight `pendingMessage`) to Markdown and triggers
  // a Blob download. No backend round-trip.
  const handleExport = React.useCallback(() => {
    const md = exportTranscriptMarkdown(title, chat.messages, t);
    try {
      const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = exportFilename(title);
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.warn("chat export failed", err);
    }
  }, [title, chat.messages, t]);

  const canExport = chat.messages.length > 0;

  return (
    <div className="flex h-full min-w-0 flex-1 gap-3 sm:gap-4">
      <section
        className={cn(
          "flex h-full min-w-0 flex-1 flex-col overflow-hidden",
          "rounded-sg-lg border border-sg-border bg-transparent",
        )}
        data-testid="chat-area"
      >
        <header className="c-appbar flex items-center justify-between px-4 py-2">
          <div className="flex min-w-0 items-center gap-2.5">
            {/* The app-bar pearl doubles as the typing indicator: it spins
                with full bloom while a turn streams, and rests idle after. */}
            <PresenceOrb size="sm" active={chat.isStreaming} />
            <div className="flex min-w-0 flex-col">
              <h1 className="truncate font-display text-[13px] font-medium text-sg-ink">{title}</h1>
              <p className="font-mono text-[10px] text-sg-ink-5">{sessionKey}</p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2 text-[11px] text-sg-ink-4">
            <button
              type="button"
              onClick={handleExport}
              disabled={!canExport}
              className={cn(
                "inline-flex min-h-6 items-center gap-1 rounded-sg-sm border border-sg-border bg-sg-inset px-1.5 py-0.5",
                "hover:bg-sg-inset-hover hover:text-sg-ink",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50",
                "disabled:cursor-not-allowed disabled:opacity-40",
              )}
              aria-label={t("chat.exportAriaLabel")}
              title={t("chat.exportAriaLabel")}
              data-testid="chat-export"
            >
              <Download className="h-3 w-3" aria-hidden="true" />
              <span className="hidden sm:inline">{t("chat.export")}</span>
            </button>
            {onAgentChange ? (
              <AgentPicker
                value={agentId ?? null}
                onChange={onAgentChange}
                className="hidden sm:flex"
              />
            ) : null}
            {hasUsage ? (
              <span
                className="inline-flex items-center gap-1 rounded-sg-sm border border-sg-border bg-sg-inset px-1.5 py-0.5 font-mono"
                data-testid="chat-totals"
                title={`${inputTokens} in · ${outputTokens} out · $${costUsd.toFixed(4)}`}
              >
                {inputTokens + outputTokens} tok · ${costUsd.toFixed(4)}
              </span>
            ) : null}
            <span className="inline-flex items-center gap-1 rounded-sg-sm border border-sg-border bg-sg-inset px-1.5 py-0.5">
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  // Static — the app-bar pearl owns the streaming motion.
                  chat.isStreaming ? "bg-sg-tint" : "bg-sg-ok",
                )}
                aria-hidden="true"
              />
              {chat.isStreaming ? t("chat.statusStreaming") : t("chat.statusIdle")}
            </span>
          </div>
        </header>

        <div className="relative flex-1 overflow-hidden">
          <ConversationSearch
            messages={chat.messages}
            onJump={jumpToMessage}
            bindHotkey
          />
          <MessageList
            messages={chat.messages}
            pendingMessage={chat.pendingMessage}
            onRegenerate={chat.retryLast}
            onApprove={chat.approve}
            onEdit={handleEdit}
            onBranch={handleBranch}
            onReply={handleReply}
            onOpenArtifact={handleOpenArtifact}
            showActionTrace={showActionTrace}
            emptyState={<ChatEmptyState onPick={handlePickSuggestion} />}
            hasEarlier={hasEarlier}
            loadingEarlier={loadingEarlier}
            onLoadEarlier={onLoadEarlier}
          />
        </div>

        <Composer
          isStreaming={chat.isStreaming}
          modelLabel={model}
          personaLabel={personaLabel}
          mentionCandidates={mentionCandidates}
          imageModelLabel={imageModelLabel}
          onOpenImageModelPicker={onOpenImageModelPicker}
          reasoningEffort={normalizedReasoningEffort}
          onReasoningEffortChange={onReasoningEffortChange}
          allowXHighReasoningEffort={allowsCodexReasoning}
          replyContext={reply}
          onClearReply={() => setReply(null)}
          onSend={(text, attachments) => {
            const finalText = reply
              ? `> ${reply.authorLabel}: ${reply.preview}\n\n${text}`
              : text;
            chat.sendMessage(finalText, attachments).catch((err) => {
              console.warn("chat send failed", err);
            });
            setReply(null);
          }}
          onStop={() => {
            chat.stop().catch((err) => {
              console.warn("chat stop failed", err);
            });
          }}
          onOpenModelPicker={onOpenModelPicker}
          onOpenPersonaPicker={onOpenPersonaPicker}
          onSlashClear={() => chat.hydrate([])}
        />
      </section>

      <ArtifactPanel
        artifacts={arts.artifacts}
        activeId={arts.activeId}
        open={arts.panelOpen}
        onClose={arts.close}
        onSelect={arts.select}
        onRemove={arts.remove}
      />
    </div>
  );
}
