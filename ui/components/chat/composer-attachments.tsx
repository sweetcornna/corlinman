"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  AlertCircle,
  FileText,
  Film,
  Music,
  Paperclip,
  X,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { springs } from "@/lib/motion";
import type { ChatAttachment } from "@/lib/chat/types";

interface ComposerAttachmentsProps {
  attachments: ChatAttachment[];
  onRemove: (id: string) => void;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}

/** Clamp a 0..1 upload fraction to a whole-percent string for the UI. */
function formatPercent(fraction: number): string {
  const pct = Math.max(0, Math.min(100, Math.round(fraction * 100)));
  return `${pct}%`;
}

/**
 * Thin upload-progress bar. When `fraction` is a number the fill is
 * determinate (driven by real upload progress); when it's `undefined`
 * the fill falls back to an indeterminate shimmer. Styled with sg-*
 * tokens only and no backdrop-filter (content-card rule).
 */
function UploadBar({ fraction }: { fraction?: number }) {
  const determinate = typeof fraction === "number";
  return (
    <span
      className="mt-0.5 block h-0.5 w-full overflow-hidden rounded-full bg-sg-inset-strong"
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={determinate ? Math.round(fraction! * 100) : undefined}
      data-testid="composer-attachment-progress"
    >
      <span
        className={cn(
          "block h-full rounded-full bg-sg-accent",
          determinate ? "transition-[width] duration-200" : "w-1/3 animate-pulse",
        )}
        style={determinate ? { width: formatPercent(fraction!) } : undefined}
      />
    </span>
  );
}

function NonImageIcon({ kind }: { kind: ChatAttachment["kind"] }) {
  const cls = "h-3.5 w-3.5 text-sg-ink-3";
  if (kind === "audio") return <Music className={cls} aria-hidden="true" />;
  if (kind === "video") return <Film className={cls} aria-hidden="true" />;
  if (kind === "document") return <FileText className={cls} aria-hidden="true" />;
  return <Paperclip className={cls} aria-hidden="true" />;
}

export function ComposerAttachments({
  attachments,
  onRemove,
}: ComposerAttachmentsProps) {
  const { t } = useTranslation();
  const reducedMotion = useReducedMotion();
  if (attachments.length === 0) return null;
  return (
    <ul
      className="flex flex-wrap gap-2 px-1 pb-2"
      aria-label={t("chat.pendingAttachmentsAriaLabel")}
      data-testid="composer-attachments"
    >
      <AnimatePresence initial={false}>
      {attachments.map((att) => {
        const src = att.previewUrl ?? att.remoteUrl;
        const isImage = att.kind === "image" && !!src && !att.error;
        return (
          <motion.li
            key={att.id}
            layout={!reducedMotion}
            initial={reducedMotion ? false : { opacity: 0, scale: 0.7 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={reducedMotion ? { opacity: 0 } : { opacity: 0, scale: 0.7 }}
            transition={reducedMotion ? { duration: 0 } : springs.bouncy}
            className="group relative"
          >
            {isImage ? (
              <div
                className={cn(
                  "relative h-16 w-16 overflow-hidden rounded-sg-sm border border-sg-border",
                  att.error && "border-sg-err/50",
                )}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={src}
                  alt={att.name}
                  className="h-16 w-16 object-cover"
                />
                {att.uploading ? (
                  <>
                    <div
                      className="absolute inset-0 animate-pulse bg-sg-overlay"
                      aria-hidden="true"
                    />
                    {typeof att.progress === "number" ? (
                      <span
                        className="absolute bottom-0.5 right-0.5 rounded-sg-sm bg-sg-card-strong px-1 text-[9px] font-mono text-sg-ink-2"
                        data-testid="composer-attachment-percent"
                      >
                        {formatPercent(att.progress)}
                      </span>
                    ) : null}
                  </>
                ) : null}
              </div>
            ) : (
              <div
                className={cn(
                  "flex h-16 min-w-[7rem] max-w-[11rem] flex-col justify-center gap-0.5 rounded-sg-sm",
                  "border border-sg-border bg-sg-inset px-2.5 py-1.5 text-[11px] text-sg-ink",
                  att.error && "border-sg-err/50",
                )}
              >
                <div className="flex items-center gap-1.5">
                  {att.error ? (
                    <AlertCircle className="h-3.5 w-3.5 shrink-0 text-sg-err" aria-hidden="true" />
                  ) : (
                    <NonImageIcon kind={att.kind} />
                  )}
                  <span className="truncate font-mono">{att.name}</span>
                </div>
                <span className="font-mono text-[10px] text-sg-ink-4">
                  {att.error
                    ? att.error
                    : att.uploading && typeof att.progress === "number"
                      ? formatPercent(att.progress)
                      : formatBytes(att.sizeBytes)}
                </span>
                {att.uploading ? <UploadBar fraction={att.progress} /> : null}
              </div>
            )}
            <button
              type="button"
              onClick={() => onRemove(att.id)}
              className={cn(
                // 24×24 touch target (h-6 w-6). A transparent ::before
                // (via `before:*`) widens the clickable hit area beyond
                // the visible circle without disturbing the layout, while
                // the visible chrome stays compact. The inner glyph keeps
                // the small visual footprint.
                "absolute -right-2 -top-2 flex h-6 w-6 items-center justify-center rounded-full",
                "border border-sg-border bg-sg-card-strong text-sg-ink-3 shadow-sg-1",
                "opacity-0 transition-opacity hover:text-sg-ink group-hover:opacity-100",
                "focus-visible:opacity-100",
                "before:absolute before:-inset-1 before:content-['']",
              )}
              aria-label={t("chat.removeAttachment", { name: att.name })}
              data-testid="composer-attachment-remove"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </motion.li>
        );
      })}
      </AnimatePresence>
    </ul>
  );
}
