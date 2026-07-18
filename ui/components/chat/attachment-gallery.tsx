"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import {
  Download,
  FileAudio,
  FileText,
  FileVideo,
  Paperclip,
  X,
} from "@/components/icons";

import { cn } from "@/lib/utils";
import type { ChatAttachment } from "@/lib/chat/types";

interface AttachmentGalleryProps {
  attachments: ChatAttachment[];
  className?: string;
}

function attachmentSrc(att: ChatAttachment): string | undefined {
  return att.remoteUrl ?? att.previewUrl;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}

function NonImageIcon({ kind }: { kind: ChatAttachment["kind"] }) {
  const className = "h-4 w-4 shrink-0 text-sg-ink-3";
  if (kind === "audio") return <FileAudio className={className} aria-hidden="true" />;
  if (kind === "video") return <FileVideo className={className} aria-hidden="true" />;
  if (kind === "document") return <FileText className={className} aria-hidden="true" />;
  return <Paperclip className={className} aria-hidden="true" />;
}

/**
 * Renders a user message's attachments. Image attachments become a
 * thumbnail grid (3 per row) that open a fullscreen lightbox on click;
 * everything else stays as a compact icon chip.
 */
export function AttachmentGallery({ attachments, className }: AttachmentGalleryProps) {
  const { t } = useTranslation();
  const [zoomSrc, setZoomSrc] = React.useState<string | null>(null);

  const images = attachments.filter(
    (a) => a.kind === "image" && attachmentSrc(a),
  );
  const others = attachments.filter(
    (a) => a.kind !== "image" || !attachmentSrc(a),
  );

  if (attachments.length === 0) return null;

  return (
    <div
      className={cn("flex flex-col gap-2", className)}
      data-testid="attachment-gallery"
      aria-label={t("chat.attachmentsAriaLabel")}
    >
      {images.length > 0 ? (
        <div className="grid grid-cols-3 gap-1.5">
          {images.map((att) => {
            const src = attachmentSrc(att)!;
            return (
              <button
                key={att.id}
                type="button"
                onClick={() => setZoomSrc(src)}
                className="group/thumb relative block aspect-square overflow-hidden rounded-sg-md border border-sg-border shadow-sg-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50"
                data-testid="attachment-thumb"
                aria-label={att.name}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={src}
                  alt={att.name}
                  className="h-28 w-full cursor-zoom-in object-cover transition-transform duration-200 group-hover/thumb:scale-[1.03]"
                />
              </button>
            );
          })}
        </div>
      ) : null}

      {others.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5">
          {others.map((att) => {
            const href = att.remoteUrl;
            return (
              <li
                key={att.id}
                className="flex max-w-[15rem] items-center gap-2 rounded-sg-sm border border-sg-border bg-sg-inset px-2.5 py-1.5 text-[11px] text-sg-ink-2"
                data-testid="attachment-file-card"
              >
                <NonImageIcon kind={att.kind} />
                <div className="min-w-0 flex-1">
                  <span className="block truncate font-mono text-sg-ink">
                    {att.name}
                  </span>
                  <span className="block font-mono text-[10px] text-sg-ink-4">
                    {formatBytes(att.sizeBytes)}
                  </span>
                </div>
                {href ? (
                  <a
                    href={href}
                    download={att.name}
                    className="shrink-0 rounded-md p-1 text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
                    aria-label={t("chat.downloadAttachment", { name: att.name })}
                    data-testid="attachment-download"
                  >
                    <Download className="h-3.5 w-3.5" aria-hidden="true" />
                  </a>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}

      {zoomSrc ? (
        <AttachmentLightbox src={zoomSrc} onClose={() => setZoomSrc(null)} />
      ) : null}
    </div>
  );
}

/** Fullscreen image zoom — closes on click, Esc, or the corner button. */
function AttachmentLightbox({ src, onClose }: { src: string; onClose: () => void }) {
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      data-testid="attachment-lightbox"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6"
      onClick={onClose}
    >
      <button
        type="button"
        onClick={onClose}
        aria-label="close"
        className="absolute right-4 top-4 rounded-full bg-sg-inset p-2 text-sg-ink-2 hover:text-sg-ink"
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={src} alt="" className="max-h-full max-w-full rounded-sg-lg object-contain shadow-sg-4" />
    </div>
  );
}
