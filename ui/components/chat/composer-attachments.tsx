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
                  <div
                    className="absolute inset-0 animate-pulse bg-sg-overlay"
                    aria-hidden="true"
                  />
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
                  {att.error ? att.error : formatBytes(att.sizeBytes)}
                </span>
                {att.uploading ? (
                  <span
                    className="mt-0.5 h-0.5 w-full overflow-hidden rounded-full bg-sg-inset-strong"
                    aria-hidden="true"
                  >
                    <span className="block h-full w-1/3 animate-pulse rounded-full bg-sg-accent" />
                  </span>
                ) : null}
              </div>
            )}
            <button
              type="button"
              onClick={() => onRemove(att.id)}
              className={cn(
                "absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full",
                "border border-sg-border bg-sg-card-strong text-sg-ink-3 shadow-sg-1",
                "opacity-0 transition-opacity hover:text-sg-ink group-hover:opacity-100",
                "focus-visible:opacity-100",
              )}
              aria-label={t("chat.removeAttachment", { name: att.name })}
            >
              <X className="h-3 w-3" aria-hidden="true" />
            </button>
          </motion.li>
        );
      })}
      </AnimatePresence>
    </ul>
  );
}
