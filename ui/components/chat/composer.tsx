"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  Bot,
  Image as ImageIcon,
  Paperclip,
  Send,
  Sparkles,
  Square,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { ChatAttachment } from "@/lib/chat/types";
import {
  attachmentKindFromMime,
  fileToDataUrl,
  validateAttachment,
} from "@/lib/api/chat";
import { ComposerAttachments } from "@/components/chat/composer-attachments";
import {
  ComposerMentionMenu,
  detectMentionQuery,
  type MentionCandidate,
} from "@/components/chat/composer-mention-menu";
import {
  ComposerSlashMenu,
  type SlashCommand,
} from "@/components/chat/composer-slash-menu";

interface ComposerProps {
  isStreaming: boolean;
  modelLabel: string;
  personaLabel?: string;
  onSend: (text: string, attachments: ChatAttachment[]) => void;
  onStop: () => void;
  onOpenModelPicker?: () => void;
  onOpenPersonaPicker?: () => void;
  /** Optional image-model pill — when both `imageModelLabel` and
   *  `onOpenImageModelPicker` are provided, a second pill appears next
   *  to the LLM model pill. */
  imageModelLabel?: string;
  onOpenImageModelPicker?: () => void;
  extraSlashCommands?: SlashCommand[];
  onSlashClear?: () => void;
  placeholder?: string;
  mentionCandidates?: MentionCandidate[];
  replyContext?: { authorLabel: string; preview: string } | null;
  onClearReply?: () => void;
}

const MAX_TEXTAREA_PX = 220;

export function Composer({
  isStreaming,
  modelLabel,
  personaLabel,
  onSend,
  onStop,
  onOpenModelPicker,
  onOpenPersonaPicker,
  imageModelLabel,
  onOpenImageModelPicker,
  extraSlashCommands,
  onSlashClear,
  placeholder,
  mentionCandidates,
  replyContext,
  onClearReply,
}: ComposerProps) {
  const { t } = useTranslation();
  const [text, setText] = React.useState("");
  const [caret, setCaret] = React.useState(0);
  const [attachments, setAttachments] = React.useState<ChatAttachment[]>([]);
  const [isDraggingOver, setIsDraggingOver] = React.useState(false);
  const taRef = React.useRef<HTMLTextAreaElement | null>(null);
  const fileRef = React.useRef<HTMLInputElement | null>(null);

  React.useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, MAX_TEXTAREA_PX) + "px";
  }, [text]);

  const slashOpen = text.startsWith("/");
  const slashQuery = slashOpen ? text.slice(1) : "";

  const mention = React.useMemo(
    () =>
      mentionCandidates && mentionCandidates.length > 0
        ? detectMentionQuery(text, caret)
        : null,
    [text, caret, mentionCandidates],
  );

  const builtinSlashCommands = React.useMemo<SlashCommand[]>(
    () => [
      {
        id: "clear",
        label: t("chat.slashClear"),
        description: t("chat.slashClearDesc"),
        run: () => {
          setAttachments([]);
          return "";
        },
      },
      {
        id: "reset",
        label: t("chat.slashReset"),
        description: t("chat.slashResetDesc"),
        run: () => {
          onSlashClear?.();
          return "";
        },
      },
      {
        id: "model",
        label: t("chat.slashModel"),
        argHint: t("chat.slashArgPlaceholder"),
        description: t("chat.slashModelDesc"),
        run: () => {
          onOpenModelPicker?.();
          return "";
        },
      },
      {
        id: "persona",
        label: t("chat.slashPersona"),
        argHint: t("chat.slashArgPlaceholder"),
        description: t("chat.slashPersonaDesc"),
        run: () => {
          onOpenPersonaPicker?.();
          return "";
        },
      },
    ],
    [t, onSlashClear, onOpenModelPicker, onOpenPersonaPicker],
  );

  const allSlashCommands = React.useMemo(
    () => [...builtinSlashCommands, ...(extraSlashCommands ?? [])],
    [builtinSlashCommands, extraSlashCommands],
  );

  const addFiles = React.useCallback(async (files: FileList | File[]) => {
    const items = Array.from(files);
    const next: ChatAttachment[] = [];
    for (const file of items) {
      const err = validateAttachment(file);
      const id = `att_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
      const kind = attachmentKindFromMime(file.type);
      const att: ChatAttachment = {
        id,
        kind,
        name: file.name,
        mime: file.type,
        sizeBytes: file.size,
        uploading: !err,
        error: err ?? undefined,
      };
      if (kind === "image" && !err) {
        try {
          att.previewUrl = URL.createObjectURL(file);
        } catch {
          // ignore
        }
      }
      next.push(att);
      if (!err && kind === "image" && file.size < 1024 * 1024) {
        try {
          att.remoteUrl = await fileToDataUrl(file);
          att.uploading = false;
        } catch {
          att.uploading = false;
          att.error = "preview failed";
        }
      } else {
        att.uploading = false;
      }
    }
    setAttachments((prev) => [...prev, ...next]);
  }, []);

  const handleSend = React.useCallback(() => {
    const v = text.trim();
    if (!v && attachments.length === 0) return;
    if (isStreaming) return;
    onSend(v, attachments);
    setText("");
    setAttachments([]);
  }, [text, attachments, isStreaming, onSend]);

  const handleKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (slashOpen) return;
      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend, slashOpen],
  );

  const handlePaste = React.useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const files: File[] = [];
      for (const it of Array.from(e.clipboardData.items)) {
        if (it.kind === "file") {
          const f = it.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length > 0) {
        e.preventDefault();
        void addFiles(files);
      }
    },
    [addFiles],
  );

  const handleDrop = React.useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDraggingOver(false);
      if (e.dataTransfer.files.length > 0) {
        void addFiles(e.dataTransfer.files);
      }
    },
    [addFiles],
  );

  const handlePickSlash = React.useCallback((cmd: SlashCommand) => {
    const replacement = cmd.run();
    setText(typeof replacement === "string" ? replacement : "");
    taRef.current?.focus();
  }, []);

  const handlePickMention = React.useCallback(
    (c: MentionCandidate) => {
      if (!mention) return;
      const before = text.slice(0, mention.start);
      const after = text.slice(mention.end);
      const insert = `@${c.name} `;
      const next = `${before}${insert}${after}`;
      setText(next);
      const newCaret = before.length + insert.length;
      window.requestAnimationFrame(() => {
        const el = taRef.current;
        if (el) {
          el.focus();
          el.setSelectionRange(newCaret, newCaret);
          setCaret(newCaret);
        }
      });
    },
    [text, mention],
  );

  return (
    <div
      className={cn(
        "relative border-t border-tp-glass-edge bg-tp-glass-inner/40",
        isDraggingOver && "ring-2 ring-tp-amber",
      )}
      onDragEnter={(e) => {
        e.preventDefault();
        setIsDraggingOver(true);
      }}
      onDragOver={(e) => {
        e.preventDefault();
      }}
      onDragLeave={() => setIsDraggingOver(false)}
      onDrop={handleDrop}
      data-testid="composer"
    >
      {replyContext ? (
        <div
          className="flex items-center gap-2 border-b border-tp-glass-edge bg-tp-glass-inner/60 px-3 py-1.5 text-[11px]"
          data-testid="composer-reply"
        >
          <span className="rounded bg-tp-amber/20 px-1 py-0 font-mono text-tp-ink">
            ↩ {replyContext.authorLabel}
          </span>
          <span className="flex-1 truncate text-tp-ink-2">
            {replyContext.preview}
          </span>
          <button
            type="button"
            onClick={onClearReply}
            className="rounded p-1 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink"
            aria-label={t("chat.composerReplyClear")}
            data-testid="composer-reply-clear"
          >
            ×
          </button>
        </div>
      ) : null}

      <ComposerAttachments
        attachments={attachments}
        onRemove={(id) =>
          setAttachments((prev) => prev.filter((a) => a.id !== id))
        }
      />

      <div className="relative mx-auto flex max-w-3xl items-end gap-2 px-3 pt-2 pb-2">
        <input
          ref={fileRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            if (e.target.files) void addFiles(e.target.files);
            e.target.value = "";
          }}
          data-testid="composer-file-input"
        />

        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="rounded p-1.5 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink"
          aria-label={t("chat.composerAttach")}
          data-testid="composer-attach"
        >
          <Paperclip className="h-4 w-4" aria-hidden="true" />
        </button>

        <div className="relative flex-1">
          {slashOpen ? (
            <ComposerSlashMenu
              query={slashQuery}
              commands={allSlashCommands}
              onPick={handlePickSlash}
              onClose={() => setText("")}
            />
          ) : null}
          {!slashOpen && mention && mentionCandidates ? (
            <ComposerMentionMenu
              query={mention.query}
              candidates={mentionCandidates}
              onPick={handlePickMention}
              onClose={() => {
                setText(text + " ");
              }}
            />
          ) : null}
          <textarea
            ref={taRef}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setCaret(e.target.selectionStart ?? e.target.value.length);
            }}
            onKeyUp={(e) =>
              setCaret(
                (e.currentTarget.selectionStart ??
                  e.currentTarget.value.length) as number,
              )
            }
            onClick={(e) =>
              setCaret(
                (e.currentTarget.selectionStart ??
                  e.currentTarget.value.length) as number,
              )
            }
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            rows={1}
            placeholder={placeholder ?? t("chat.composerPlaceholder")}
            className={cn(
              "w-full resize-none rounded-md border border-tp-glass-edge bg-tp-glass-inner",
              "px-3 py-2 text-[13px] text-tp-ink placeholder:text-tp-ink-3",
              "focus:border-tp-amber focus:outline-none",
            )}
            data-testid="composer-textarea"
          />
        </div>

        <div className="flex flex-col gap-1 self-end">
          <button
            type="button"
            onClick={onOpenModelPicker}
            className="inline-flex items-center gap-1 rounded border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-0.5 text-[10px] text-tp-ink-2 hover:text-tp-ink"
            data-testid="composer-model"
            aria-label={t("chat.composerModelAriaLabel")}
          >
            <Bot className="h-3 w-3" aria-hidden="true" />
            {modelLabel}
          </button>
          {personaLabel ? (
            <button
              type="button"
              onClick={onOpenPersonaPicker}
              className="inline-flex items-center gap-1 rounded border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-0.5 text-[10px] text-tp-ink-2 hover:text-tp-ink"
              data-testid="composer-persona"
              aria-label={t("chat.composerPersonaAriaLabel")}
            >
              <Sparkles className="h-3 w-3" aria-hidden="true" />
              {personaLabel}
            </button>
          ) : null}
          {imageModelLabel && onOpenImageModelPicker ? (
            <button
              type="button"
              onClick={onOpenImageModelPicker}
              className="inline-flex items-center gap-1 rounded border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-0.5 text-[10px] text-tp-ink-2 hover:text-tp-ink"
              data-testid="composer-image-model"
              aria-label={t("chat.modelPicker.titleImage")}
            >
              <ImageIcon className="h-3 w-3" aria-hidden="true" />
              {imageModelLabel}
            </button>
          ) : null}
        </div>

        {isStreaming ? (
          <button
            type="button"
            onClick={onStop}
            className={cn(
              "inline-flex items-center gap-1 rounded-md border px-3 py-2 text-[12px]",
              "border-tp-err/40 bg-tp-err/10 text-tp-ink hover:bg-tp-err/20",
            )}
            data-testid="composer-stop"
            aria-label={t("chat.composerStopAriaLabel")}
          >
            <Square className="h-3.5 w-3.5" aria-hidden="true" />
            {t("chat.composerStop")}
          </button>
        ) : (
          <button
            type="button"
            onClick={handleSend}
            disabled={!text.trim() && attachments.length === 0}
            className={cn(
              "inline-flex items-center gap-1 rounded-md border px-3 py-2 text-[12px]",
              "border-tp-amber/60 bg-tp-amber/20 text-tp-ink hover:bg-tp-amber/30",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
            data-testid="composer-send"
            aria-label={t("chat.composerSendAriaLabel")}
          >
            <Send className="h-3.5 w-3.5" aria-hidden="true" />
            {t("chat.composerSend")}
          </button>
        )}
      </div>
    </div>
  );
}
