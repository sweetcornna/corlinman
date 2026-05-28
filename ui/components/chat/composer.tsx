"use client";

/**
 * Chat composer.
 *
 * Layout:
 *
 *   ┌───────────────────────────────────────────────────────┐
 *   │ [attachment chips row]                                │
 *   │ [auto-grow textarea ........................]         │
 *   │ [📎 attach] [model picker] [persona]   [Stop/Send →]  │
 *   └───────────────────────────────────────────────────────┘
 *
 * Key affordances:
 *
 *   - Enter sends; Shift+Enter inserts newline
 *   - Paste image / drag-drop file → adds to attachments
 *   - `/` at the start opens the slash-command menu (built-ins:
 *     /clear, /model, /persona, /reset)
 *   - "Stop" replaces "Send" while streaming and aborts the run
 *   - Model + persona pickers shown as compact pills (controlled by parent)
 */

import * as React from "react";
import {
  Bot,
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
  /** Extra slash commands beyond the built-ins (Wave 2: per-skill). */
  extraSlashCommands?: SlashCommand[];
  onSlashClear?: () => void;
  placeholder?: string;
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
  extraSlashCommands,
  onSlashClear,
  placeholder,
}: ComposerProps) {
  const [text, setText] = React.useState("");
  const [attachments, setAttachments] = React.useState<ChatAttachment[]>([]);
  const [isDraggingOver, setIsDraggingOver] = React.useState(false);
  const taRef = React.useRef<HTMLTextAreaElement | null>(null);
  const fileRef = React.useRef<HTMLInputElement | null>(null);

  // ── auto-grow textarea ────────────────────────────────────────────
  React.useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, MAX_TEXTAREA_PX) + "px";
  }, [text]);

  // ── slash menu ────────────────────────────────────────────────────
  const slashOpen = text.startsWith("/");
  const slashQuery = slashOpen ? text.slice(1) : "";

  const builtinSlashCommands = React.useMemo<SlashCommand[]>(
    () => [
      {
        id: "clear",
        label: "Clear the current draft",
        description: "Clear composer text and attachments",
        run: () => {
          setAttachments([]);
          return "";
        },
      },
      {
        id: "reset",
        label: "Reset conversation",
        description: "Clear the visible thread (does not delete on server)",
        run: () => {
          onSlashClear?.();
          return "";
        },
      },
      {
        id: "model",
        label: "Change model",
        argHint: "<name>",
        description: "Open the model picker",
        run: () => {
          onOpenModelPicker?.();
          return "";
        },
      },
      {
        id: "persona",
        label: "Change persona",
        argHint: "<name>",
        description: "Open the persona picker",
        run: () => {
          onOpenPersonaPicker?.();
          return "";
        },
      },
    ],
    [onSlashClear, onOpenModelPicker, onOpenPersonaPicker],
  );

  const allSlashCommands = React.useMemo(
    () => [...builtinSlashCommands, ...(extraSlashCommands ?? [])],
    [builtinSlashCommands, extraSlashCommands],
  );

  // ── attachment helpers ────────────────────────────────────────────
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
      // For MVP we inline tiny images via data URLs into remoteUrl. Wave 2
      // swaps this for a real /admin/uploads endpoint.
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

  // ── handlers ──────────────────────────────────────────────────────
  const handleSend = React.useCallback(() => {
    const t = text.trim();
    if (!t && attachments.length === 0) return;
    if (isStreaming) return;
    onSend(t, attachments);
    setText("");
    setAttachments([]);
  }, [text, attachments, isStreaming, onSend]);

  const handleKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (slashOpen) return; // slash menu owns navigation
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

  const handlePickSlash = React.useCallback(
    (cmd: SlashCommand) => {
      const replacement = cmd.run();
      setText(typeof replacement === "string" ? replacement : "");
      taRef.current?.focus();
    },
    [],
  );

  // ── render ────────────────────────────────────────────────────────
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
      <ComposerAttachments
        attachments={attachments}
        onRemove={(id) =>
          setAttachments((prev) => prev.filter((a) => a.id !== id))
        }
      />

      <div className="relative mx-auto flex max-w-3xl items-end gap-2 px-3 pt-2 pb-2">
        {/* hidden file input */}
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
          aria-label="Attach files"
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
          <textarea
            ref={taRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            rows={1}
            placeholder={placeholder ?? "Message…  (Enter to send, Shift+Enter for newline, / for commands)"}
            className={cn(
              "w-full resize-none rounded-md border border-tp-glass-edge bg-tp-glass-inner",
              "px-3 py-2 text-[13px] text-tp-ink placeholder:text-tp-ink-3",
              "focus:border-tp-amber focus:outline-none",
            )}
            data-testid="composer-textarea"
          />
        </div>

        {/* model + persona pills */}
        <div className="flex flex-col gap-1 self-end">
          <button
            type="button"
            onClick={onOpenModelPicker}
            className="inline-flex items-center gap-1 rounded border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-0.5 text-[10px] text-tp-ink-2 hover:text-tp-ink"
            data-testid="composer-model"
            aria-label="Change model"
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
              aria-label="Change persona"
            >
              <Sparkles className="h-3 w-3" aria-hidden="true" />
              {personaLabel}
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
            aria-label="Stop generating"
          >
            <Square className="h-3.5 w-3.5" aria-hidden="true" />
            Stop
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
            aria-label="Send message"
          >
            <Send className="h-3.5 w-3.5" aria-hidden="true" />
            Send
          </button>
        )}
      </div>
    </div>
  );
}
