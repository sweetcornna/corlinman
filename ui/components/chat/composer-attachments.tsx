"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  AlertCircle,
  Image as ImageIcon,
  Loader2,
  Paperclip,
  X,
} from "lucide-react";

import { cn } from "@/lib/utils";
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

export function ComposerAttachments({
  attachments,
  onRemove,
}: ComposerAttachmentsProps) {
  const { t } = useTranslation();
  if (attachments.length === 0) return null;
  return (
    <ul
      className="flex flex-wrap gap-1.5 px-3 pt-2"
      aria-label={t("chat.pendingAttachmentsAriaLabel")}
      data-testid="composer-attachments"
    >
      {attachments.map((att) => (
        <li
          key={att.id}
          className={cn(
            "flex items-center gap-1 rounded border border-tp-glass-edge",
            "bg-tp-glass-inner px-1.5 py-0.5 text-[11px] text-tp-ink",
            att.error && "border-tp-err/40",
          )}
        >
          {att.uploading ? (
            <Loader2 className="h-3 w-3 animate-spin text-tp-amber" aria-hidden="true" />
          ) : att.error ? (
            <AlertCircle className="h-3 w-3 text-tp-err" aria-hidden="true" />
          ) : att.kind === "image" ? (
            <ImageIcon className="h-3 w-3 text-tp-ink-3" aria-hidden="true" />
          ) : (
            <Paperclip className="h-3 w-3 text-tp-ink-3" aria-hidden="true" />
          )}
          <span className="font-mono">{att.name}</span>
          <span className="text-tp-ink-3">·</span>
          <span className="font-mono text-tp-ink-3">{formatBytes(att.sizeBytes)}</span>
          <button
            type="button"
            onClick={() => onRemove(att.id)}
            className="ml-0.5 rounded p-0.5 text-tp-ink-3 hover:bg-tp-glass-inner hover:text-tp-ink"
            aria-label={t("chat.removeAttachment", { name: att.name })}
          >
            <X className="h-3 w-3" aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
  );
}
