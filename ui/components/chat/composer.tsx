"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  Bot,
  Brain,
  Image as ImageIcon,
  Paperclip,
  Send,
  Smile,
  Sparkles,
  Square,
} from "@/components/icons";

import { cn } from "@/lib/utils";
import type { ReasoningEffort } from "@/lib/api/chat";
import type { ChatAttachment } from "@/lib/chat/types";
import {
  attachmentKindFromMime,
  validateAttachment,
} from "@/lib/api/chat";
import { uploadChatFile } from "@/lib/api/files";
import { ComposerAttachments } from "@/components/chat/composer-attachments";
import { EmojiPicker } from "@/components/chat/emoji-picker";
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
  reasoningEffort?: ReasoningEffort;
  onReasoningEffortChange?: (effort: ReasoningEffort) => void;
  allowXHighReasoningEffort?: boolean;
  extraSlashCommands?: SlashCommand[];
  onSlashClear?: () => void;
  placeholder?: string;
  mentionCandidates?: MentionCandidate[];
  replyContext?: { authorLabel: string; preview: string } | null;
  onClearReply?: () => void;
}

const MAX_TEXTAREA_PX = 220;
const REASONING_EFFORT_OPTIONS: Array<{
  value: ReasoningEffort;
  labelKey: string;
}> = [
  { value: "low", labelKey: "chat.reasoningEffortLow" },
  { value: "medium", labelKey: "chat.reasoningEffortMedium" },
  { value: "high", labelKey: "chat.reasoningEffortHigh" },
  { value: "xhigh", labelKey: "chat.reasoningEffortXHigh" },
];

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
  reasoningEffort = "medium",
  onReasoningEffortChange,
  allowXHighReasoningEffort = false,
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
  const [emojiOpen, setEmojiOpen] = React.useState(false);
  const taRef = React.useRef<HTMLTextAreaElement | null>(null);
  const fileRef = React.useRef<HTMLInputElement | null>(null);
  const emojiWrapRef = React.useRef<HTMLDivElement | null>(null);
  const emojiBtnRef = React.useRef<HTMLButtonElement | null>(null);
  const reasoningEffortOptions = React.useMemo(
    () =>
      allowXHighReasoningEffort
        ? REASONING_EFFORT_OPTIONS
        : REASONING_EFFORT_OPTIONS.filter((option) => option.value !== "xhigh"),
    [allowXHighReasoningEffort],
  );
  const activeReasoningEffort =
    !allowXHighReasoningEffort && reasoningEffort === "xhigh"
      ? "high"
      : reasoningEffort;

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

  // Patch a single attachment in place by id — used by the async upload
  // callbacks below to flip `uploading`/`progress`/`remoteUrl`/`error`
  // as each upload runs, without disturbing sibling attachments.
  const patchAttachment = React.useCallback(
    (id: string, patch: Partial<ChatAttachment>) => {
      setAttachments((prev) =>
        prev.map((a) => (a.id === id ? { ...a, ...patch } : a)),
      );
    },
    [],
  );

  const addFiles = React.useCallback(
    (files: FileList | File[]) => {
      const items = Array.from(files);
      const next: ChatAttachment[] = [];
      // Valid files we'll actually upload, paired with their attachment id.
      const toUpload: { id: string; file: File }[] = [];
      for (const file of items) {
        const errCode = validateAttachment(file);
        const err = errCode
          ? errCode === "attachment_empty_file"
            ? t("chat.attachmentEmptyFile")
            : errCode
          : null;
        const id = `att_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
        const kind = attachmentKindFromMime(file.type);
        const att: ChatAttachment = {
          id,
          kind,
          name: file.name,
          mime: file.type,
          sizeBytes: file.size,
          // Local validation failures stay non-uploading with the error
          // shown; everything else enters the uploading state.
          uploading: !err,
          progress: err ? undefined : 0,
          error: err ?? undefined,
        };
        if (kind === "image" && !err) {
          // Keep the instant local preview while the real upload runs.
          try {
            att.previewUrl = URL.createObjectURL(file);
          } catch {
            // ignore — preview is best-effort
          }
        }
        next.push(att);
        if (!err) toUpload.push({ id, file });
      }
      // Show the pending attachments immediately, then kick off uploads.
      setAttachments((prev) => [...prev, ...next]);
      for (const { id, file } of toUpload) {
        uploadChatFile(file, (fraction) =>
          patchAttachment(id, { progress: fraction }),
        )
          .then((res) => {
            patchAttachment(id, {
              remoteUrl: res.url,
              fileId: res.fileId,
              uploading: false,
              progress: 1,
              error: undefined,
            });
          })
          .catch(() => {
            patchAttachment(id, {
              uploading: false,
              progress: undefined,
              error: t("chat.attachmentUploadFailed"),
            });
          });
      }
    },
    [patchAttachment, t],
  );

  // Any attachment still uploading blocks the send: the assistant can't
  // see a file whose `remoteUrl` hasn't landed yet.
  const isUploading = React.useMemo(
    () => attachments.some((a) => a.uploading),
    [attachments],
  );

  // An attachment only counts toward "something to send" once it is
  // actually usable server-side (uploaded, not errored). Failed-only
  // selections used to enable the send button and then deliver an
  // EMPTY user message — the request builder drops errored uploads.
  const hasUsableAttachment = React.useMemo(
    () => attachments.some((a) => !a.error && !a.uploading && (a.fileId || a.remoteUrl)),
    [attachments],
  );

  // Release `blob:` previews once an attachment leaves the composer —
  // browsers retain object URLs for the page lifetime otherwise, which
  // leaks every pasted image in a long session.
  const revokePreviews = React.useCallback((list: ChatAttachment[]) => {
    for (const a of list) {
      if (a.previewUrl) {
        try {
          URL.revokeObjectURL(a.previewUrl);
        } catch {
          // best-effort
        }
      }
    }
  }, []);

  // Unmount sweep: whatever previews are still pending when the composer
  // goes away (session switch, page nav) get released too.
  const attachmentsRef = React.useRef(attachments);
  attachmentsRef.current = attachments;
  React.useEffect(
    () => () => revokePreviews(attachmentsRef.current),
    [revokePreviews],
  );

  const handleSend = React.useCallback(() => {
    const v = text.trim();
    if (!v && !hasUsableAttachment) return;
    if (isStreaming || isUploading) return;
    // Only usable attachments travel with the message (the request
    // builder would drop the rest anyway); previews are released here —
    // sent attachments render from their `remoteUrl`.
    onSend(v, attachments.filter((a) => !a.error && !a.uploading));
    revokePreviews(attachments);
    setText("");
    setAttachments([]);
  }, [
    text,
    attachments,
    hasUsableAttachment,
    isStreaming,
    isUploading,
    onSend,
    revokePreviews,
  ]);

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

  const insertEmoji = React.useCallback((emoji: string) => {
    const el = taRef.current;
    const start = el?.selectionStart ?? text.length;
    const end = el?.selectionEnd ?? text.length;
    setText((prev) => prev.slice(0, start) + emoji + prev.slice(end));
    const newCaret = start + emoji.length;
    window.requestAnimationFrame(() => {
      const node = taRef.current;
      if (node) {
        node.focus();
        node.setSelectionRange(newCaret, newCaret);
        setCaret(newCaret);
      }
    });
  }, [text]);

  // Dismiss the emoji popover and return focus to the trigger button so
  // keyboard users land back where they opened it. Focus is moved
  // synchronously after the React commit (flushed in a microtask via the
  // state setter callback) rather than racing a rAF — the button is always
  // mounted, so a plain `.focus()` is reliable.
  const closeEmoji = React.useCallback(() => {
    setEmojiOpen(false);
    emojiBtnRef.current?.focus();
  }, []);

  // Dismiss the emoji popover on outside click.
  React.useEffect(() => {
    if (!emojiOpen) return;
    const onDown = (e: MouseEvent): void => {
      if (!emojiWrapRef.current?.contains(e.target as Node)) {
        setEmojiOpen(false);
      }
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [emojiOpen]);

  // Cmd+/ (mac) / Ctrl+/ (win/linux) focuses the composer from anywhere.
  // Cmd/Ctrl+K and `?` belong to the command palette; `/` is unclaimed,
  // so there's no conflict with the existing global hotkeys.
  React.useEffect(() => {
    if (typeof window === "undefined") return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "/" && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
        e.preventDefault();
        taRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // iOS soft-keyboard avoidance. When the on-screen keyboard opens, the
  // visualViewport shrinks; scroll the textarea back into view so it
  // isn't hidden behind the keyboard. Debounced + SSR-guarded; relies on
  // the visualViewport API (no-op where unsupported, e.g. older browsers
  // and jsdom).
  React.useEffect(() => {
    if (typeof window === "undefined") return;
    const vv = window.visualViewport;
    if (!vv) return;
    let prevHeight = vv.height;
    let raf = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onResize = (): void => {
      const next = vv.height;
      // Only react when the viewport *shrank* (keyboard came up) and the
      // textarea is the focused element — avoids stealing scroll on every
      // orientation / URL-bar change.
      const shrank = next < prevHeight - 80;
      prevHeight = next;
      if (!shrank) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        if (document.activeElement !== taRef.current) return;
        raf = window.requestAnimationFrame(() => {
          taRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
        });
      }, 120);
    };
    vv.addEventListener("resize", onResize);
    return () => {
      vv.removeEventListener("resize", onResize);
      if (timer) clearTimeout(timer);
      if (raf) window.cancelAnimationFrame(raf);
    };
  }, []);

  const canSend =
    (!!text.trim() || hasUsableAttachment) && !isUploading;

  return (
    <div
      className="mx-auto w-full max-w-3xl px-3 pb-3 pt-1"
      // Keep the composer clear of the iOS home indicator / gesture bar.
      // env() resolves to 0 where unsupported, so this is a safe no-op
      // on desktop and in jsdom.
      style={{ paddingBottom: "calc(0.75rem + env(safe-area-inset-bottom, 0px))" }}
      data-testid="composer-root"
    >
      <div
        className={cn(
          "sg-card relative rounded-sg-xl border border-sg-border shadow-sg-3",
          "transition-[box-shadow,transform,border-color] duration-300 ease-out",
          "focus-within:border-sg-accent/40 focus-within:shadow-sg-primary focus-within:scale-[1.006]",
          isDraggingOver &&
            "border-sg-accent/60 ring-2 ring-sg-accent/40 shadow-sg-primary",
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
        {isDraggingOver ? (
          <div
            className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-sg-xl bg-sg-accent-soft text-[12px] font-medium text-sg-accent"
            data-testid="composer-drop-hint"
            aria-hidden="true"
          >
            {t("chat.composerDropHint")}
          </div>
        ) : null}

        {replyContext ? (
          <div
            className="mx-3 mt-3 flex items-start gap-2 rounded-sg-sm bg-sg-inset px-2.5 py-1.5 text-[11px]"
            data-testid="composer-reply"
          >
            <span
              className="mt-0.5 h-full min-h-[1.75rem] w-0.5 shrink-0 rounded-full bg-sg-accent"
              aria-hidden="true"
            />
            <div className="min-w-0 flex-1">
              <span className="block font-medium text-sg-ink-2">
                ↩ {replyContext.authorLabel}
              </span>
              <span className="block truncate text-sg-ink-3">
                {replyContext.preview}
              </span>
            </div>
            <button
              type="button"
              onClick={onClearReply}
              className="rounded-md p-1 text-sg-ink-3 hover:bg-sg-inset-hover hover:text-sg-ink"
              aria-label={t("chat.composerReplyClear")}
              data-testid="composer-reply-clear"
            >
              ×
            </button>
          </div>
        ) : null}

        <div className="px-3 pt-3">
          <ComposerAttachments
            attachments={attachments}
            onRemove={(id) =>
              setAttachments((prev) => {
                revokePreviews(prev.filter((a) => a.id === id));
                return prev.filter((a) => a.id !== id);
              })
            }
          />

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

          <div className="relative">
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
              aria-keyshortcuts="Meta+/ Control+/"
              aria-label={t("chat.composerFocusShortcut")}
              placeholder={placeholder ?? t("chat.composerPlaceholder")}
              className={cn(
                "w-full resize-none bg-transparent text-[14px] leading-relaxed",
                "text-sg-ink placeholder:text-sg-ink-4 focus:outline-none",
              )}
              data-testid="composer-textarea"
            />
          </div>
        </div>

        <div className="flex items-center justify-between gap-2 px-3 pb-2.5 pt-1">
          {/* Left cluster — attach + emoji. */}
          <div className="flex min-w-0 items-center gap-0.5">
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="rounded-md p-1.5 text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
              aria-label={t("chat.composerAttach")}
              data-testid="composer-attach"
            >
              <Paperclip className="h-4 w-4" aria-hidden="true" />
            </button>

            <div className="relative" ref={emojiWrapRef}>
              <button
                ref={emojiBtnRef}
                type="button"
                onClick={() => setEmojiOpen((v) => !v)}
                className={cn(
                  "rounded-md p-1.5 text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink",
                  emojiOpen && "bg-sg-accent-soft text-sg-accent",
                )}
                aria-label={t("chat.composerEmoji")}
                aria-expanded={emojiOpen}
                aria-haspopup="listbox"
                data-testid="composer-emoji"
              >
                <Smile className="h-4 w-4" aria-hidden="true" />
              </button>
              {emojiOpen ? (
                <EmojiPicker
                  onPick={insertEmoji}
                  onPickSticker={() => {
                    closeEmoji();
                    fileRef.current?.click();
                  }}
                  onClose={closeEmoji}
                />
              ) : null}
            </div>
            {onReasoningEffortChange ? (
              <div
                role="radiogroup"
                aria-label={t("chat.reasoningEffortAriaLabel")}
                className="ml-1 flex h-7 shrink-0 items-center overflow-hidden rounded-full border border-sg-border bg-sg-inset"
                data-testid="composer-reasoning"
              >
                <span className="hidden h-full items-center gap-1 border-r border-sg-border px-1.5 text-[10px] font-medium text-sg-ink-3 sm:inline-flex">
                  <Brain className="h-3 w-3" aria-hidden="true" />
                  {t("chat.reasoningEffortLabel")}
                </span>
                {reasoningEffortOptions.map((option) => {
                  const selected = option.value === activeReasoningEffort;
                  return (
                    <button
                      key={option.value}
                      type="button"
                      role="radio"
                      aria-checked={selected}
                      aria-label={t(option.labelKey)}
                      title={t(option.labelKey)}
                      onClick={() => onReasoningEffortChange(option.value)}
                      className={cn(
                        "h-full min-w-[1.75rem] px-1.5 text-[10px] font-medium transition-colors",
                        selected
                          ? "bg-sg-accent text-white"
                          : "text-sg-ink-3 hover:bg-sg-inset-hover hover:text-sg-ink",
                      )}
                      data-testid={`composer-reasoning-${option.value}`}
                    >
                      {t(option.labelKey)}
                    </button>
                  );
                })}
              </div>
            ) : null}
          </div>

          {/* Right cluster — model pills + send/stop. */}
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={onOpenModelPicker}
              className="inline-flex items-center gap-1 rounded-full border border-sg-border bg-sg-inset px-2.5 py-1 text-[11px] text-sg-ink-2 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
              data-testid="composer-model"
              aria-label={t("chat.composerModelAriaLabel")}
            >
              <Bot className="h-3 w-3" aria-hidden="true" />
              <span className="max-w-[10rem] truncate">{modelLabel}</span>
            </button>
            {personaLabel ? (
              <button
                type="button"
                onClick={onOpenPersonaPicker}
                className="inline-flex items-center gap-1 rounded-full border border-sg-border bg-sg-inset px-2.5 py-1 text-[11px] text-sg-ink-2 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
                data-testid="composer-persona"
                aria-label={t("chat.composerPersonaAriaLabel")}
              >
                <Sparkles className="h-3 w-3" aria-hidden="true" />
                <span className="max-w-[10rem] truncate">{personaLabel}</span>
              </button>
            ) : null}
            {imageModelLabel && onOpenImageModelPicker ? (
              <button
                type="button"
                onClick={onOpenImageModelPicker}
                className="inline-flex items-center gap-1 rounded-full border border-sg-border bg-sg-inset px-2.5 py-1 text-[11px] text-sg-ink-2 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
                data-testid="composer-image-model"
                aria-label={t("chat.modelPicker.titleImage")}
              >
                <ImageIcon className="h-3 w-3" aria-hidden="true" />
                <span className="max-w-[10rem] truncate">{imageModelLabel}</span>
              </button>
            ) : null}

            {isStreaming ? (
              <button
                type="button"
                onClick={onStop}
                className={cn(
                  "inline-flex h-8 w-8 items-center justify-center rounded-full",
                  "border border-sg-err/40 bg-sg-err-soft text-sg-err transition-colors hover:bg-sg-err/20",
                )}
                data-testid="composer-stop"
                aria-label={t("chat.composerStopAriaLabel")}
              >
                <Square className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            ) : (
              <button
                type="button"
                onClick={handleSend}
                disabled={!canSend}
                className={cn(
                  "inline-flex h-8 w-8 items-center justify-center rounded-full transition-all",
                  canSend
                    ? "bg-primary text-primary-foreground hover:shadow-sg-glow"
                    : "cursor-not-allowed bg-sg-inset text-sg-ink-5",
                )}
                data-testid="composer-send"
                aria-label={
                  isUploading
                    ? t("chat.composerSendWaitingUpload")
                    : t("chat.composerSendAriaLabel")
                }
                title={
                  isUploading
                    ? t("chat.composerSendWaitingUpload")
                    : undefined
                }
              >
                <Send className="h-4 w-4" aria-hidden="true" />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
