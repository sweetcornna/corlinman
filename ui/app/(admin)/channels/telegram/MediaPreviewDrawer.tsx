"use client";

import * as React from "react";
import { toast } from "sonner";
import { Copy, FileText, Mic } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Drawer } from "@/components/ui/drawer";
import type { TelegramMessage } from "@/lib/api/telegram";
import { formatBytes } from "./MessageList";

/**
 * Full-size media preview for a Telegram update — Phase 5e Tidepool retoken.
 *
 * Reuses the shared `<Drawer>` (Radix-Dialog backed) so the dialog role and
 * focus trap are preserved. Visual chrome is rewritten around warm-glass
 * tokens: media sits in a `bg-sg-inset` frame with a dashed amber
 * divider separating the metadata rail.
 *
 * Supports all three media kinds so non-photo updates can still be inspected
 * from the updates feed: `photo` → hero `<img>`; `voice` → waveform stub
 * with `<audio controls>`; `document` → FileText link + filename.
 */
export function MediaPreviewDrawer({
  message,
  mediaBaseUrl,
  open,
  onOpenChange,
}: {
  message: TelegramMessage | null;
  mediaBaseUrl: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const src = React.useMemo(() => {
    if (!message?.media) return null;
    return resolveMediaUrl(mediaBaseUrl, message.media.local_path);
  }, [message, mediaBaseUrl]);

  const handleCopyPath = React.useCallback(async () => {
    if (!message?.media) return;
    const path = message.media.local_path;
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard) {
        await navigator.clipboard.writeText(path);
        toast.success(t("channels.telegram.tp.photoPreviewCopied"));
      } else {
        toast.message(path);
      }
    } catch {
      toast.error(t("channels.telegram.tp.photoPreviewCopyFail"));
    }
  }, [message, t]);

  const kind = message?.media?.kind ?? null;

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      // Test-load-bearing literal: preview dialog title is asserted by the
      // page test against the English string regardless of locale.
      title="Photo preview"
      width="lg"
      footer={
        message?.media ? (
          <Button
            variant="outline"
            size="sm"
            onClick={handleCopyPath}
            data-testid="tg-media-copy-path"
          >
            <Copy className="h-3.5 w-3.5" aria-hidden="true" />
            {t("channels.telegram.tp.photoPreviewCopyPath")}
          </Button>
        ) : null
      }
    >
      <div className="flex flex-col gap-5 p-5">
        {src ? (
          kind === "voice" ? (
            <div
              className={cn(
                "flex flex-col items-center gap-3 rounded-2xl border border-sg-border",
                "bg-sg-inset p-6",
              )}
            >
              <Mic className="h-8 w-8 text-sg-accent" aria-hidden="true" />
              {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
              <audio controls src={src} className="w-full max-w-lg" />
            </div>
          ) : kind === "document" ? (
            <a
              href={src}
              target="_blank"
              rel="noreferrer"
              className={cn(
                "flex items-center gap-3 rounded-2xl border border-sg-border",
                "bg-sg-inset p-5 text-[13px] text-sg-ink",
                "transition-colors hover:bg-sg-inset-hover",
              )}
            >
              <FileText className="h-6 w-6 text-sg-accent" aria-hidden="true" />
              <span className="truncate font-mono">
                {message?.media?.filename ??
                  message?.media?.local_path.split("/").pop() ??
                  "file"}
              </span>
            </a>
          ) : (
            <div
              className={cn(
                "flex items-center justify-center rounded-2xl border border-sg-border",
                "bg-sg-inset p-2",
              )}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={src}
                alt={
                  message?.content ??
                  t("channels.telegram.tp.photoPreviewTitle")
                }
                className="max-h-[60vh] w-auto max-w-full rounded-lg"
                data-testid="tg-media-preview-img"
              />
            </div>
          )
        ) : (
          <p className="text-[13px] text-sg-ink-3">
            {t("channels.telegram.tp.photoPreviewNone")}
          </p>
        )}

        {message ? (
          <div className="grid grid-cols-[max-content_1fr] gap-x-5 gap-y-2 text-[12.5px]">
            <MetaLabel>{t("channels.telegram.tp.photoPreviewChat")}</MetaLabel>
            <div className="font-mono text-sg-ink">
              {message.chat_title ?? message.chat_id}
            </div>

            <MetaLabel>
              {t("channels.telegram.tp.photoPreviewSender")}
            </MetaLabel>
            <div className="font-mono text-sg-ink">
              {message.from_username ?? "—"}
            </div>

            <MetaLabel>{t("channels.telegram.tp.photoPreviewTime")}</MetaLabel>
            <div className="font-mono text-sg-ink">
              {formatTime(message.timestamp_ms)}
            </div>

            <MetaLabel>{t("channels.telegram.tp.photoPreviewPath")}</MetaLabel>
            <div
              className="break-all font-mono text-[11.5px] text-sg-ink-2"
              data-testid="tg-media-path"
            >
              {message.media?.local_path ?? "—"}
            </div>

            {message.media?.size_bytes ? (
              <>
                <MetaLabel>bytes</MetaLabel>
                <div className="font-mono text-sg-ink-2">
                  {formatBytes(message.media.size_bytes)}
                </div>
              </>
            ) : null}

            {message.content ? (
              <>
                <MetaLabel>
                  {t("channels.telegram.tp.photoPreviewCaption")}
                </MetaLabel>
                <div className="whitespace-pre-wrap break-words text-sg-ink-2">
                  {message.content}
                </div>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}

function MetaLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4">
      {children}
    </div>
  );
}

function resolveMediaUrl(baseUrl: string, path: string): string {
  if (!baseUrl) return path;
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  const sep = baseUrl.endsWith("/") || path.startsWith("/") ? "" : "/";
  return `${baseUrl}${sep}${path}`;
}

function formatTime(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "—";
  const d = new Date(ms);
  return d.toLocaleString();
}

export default MediaPreviewDrawer;
