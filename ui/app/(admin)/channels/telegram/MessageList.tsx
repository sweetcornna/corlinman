"use client";

import * as React from "react";
import { motion } from "framer-motion";
import {
  AtSign,
  EyeOff,
  FileText,
  MessageCircle,
  Mic,
  Reply,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { useMotionVariants } from "@/lib/motion";
import { CountdownRing } from "@/components/ui/countdown-ring";
import type { TelegramMessage, TelegramMedia } from "@/lib/api/telegram";

/**
 * Recent-update list for the Telegram channel page — Phase 5e Tidepool
 * retoken.
 *
 * Each row is a warm-glass log line with:
 *   - A routing badge (mention / reply / ignored / private) using the
 *     severity-pill vocabulary from `<LogRow>`.
 *   - The message body + optional media affordance:
 *       * photo    → 56×56 thumbnail (click → `onPhotoClick`).
 *       * voice    → mic + duration + non-functional play stub.
 *       * document → file icon + filename + human-readable size (link-out).
 *   - A `<CountdownRing/>` chip when a reply is still in flight.
 *
 * Ignored group rows dim to 60% opacity — load-bearing className
 * (`opacity-60`) preserved for the page-level test suite.
 */
export function MessageList({
  messages,
  onMessageClick,
  onPhotoClick,
  selectedId,
  mediaBaseUrl = "",
}: {
  messages: TelegramMessage[];
  /** Optional: called when any row is clicked (opens the detail drawer). */
  onMessageClick?: (msg: TelegramMessage) => void;
  onPhotoClick?: (msg: TelegramMessage) => void;
  selectedId?: string | null;
  /** Prefix prepended to `media.local_path` when rendering thumbnails. */
  mediaBaseUrl?: string;
}) {
  const { t } = useTranslation();
  const variants = useMotionVariants();

  if (messages.length === 0) {
    return (
      <p className="px-5 py-10 text-center text-[12.5px] text-tp-ink-3">
        {t("channels.telegram.tp.noUpdates")}
      </p>
    );
  }

  return (
    <motion.ul
      initial="hidden"
      animate="visible"
      variants={variants.stagger}
      className="flex flex-col divide-y divide-tp-glass-edge"
    >
      {messages.map((msg) => {
        const ignored =
          msg.kind === "group" &&
          (msg.routing === "ignored" || msg.mention_reason === "none");
        const selected = selectedId === msg.id;
        return (
          <motion.li
            key={msg.id}
            variants={variants.listItem}
            data-testid={`tg-message-${msg.id}`}
            className={cn(
              "group flex items-start gap-3 px-4 py-3 transition-colors",
              "hover:bg-tp-glass-inner-hover",
              selected && "bg-tp-amber-soft",
              ignored && "opacity-60",
            )}
            onClick={() => onMessageClick?.(msg)}
            role={onMessageClick ? "button" : undefined}
            tabIndex={onMessageClick ? 0 : undefined}
            onKeyDown={(e) => {
              if (!onMessageClick) return;
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onMessageClick(msg);
              }
            }}
          >
            <span className="shrink-0 pt-0.5 font-mono text-[11px] tabular-nums text-tp-ink-4">
              {formatTs(msg.timestamp_ms)}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px]">
                {msg.from_username ? (
                  <span className="font-mono font-semibold text-tp-ink">
                    {msg.from_username}
                  </span>
                ) : null}
                <span className="text-tp-ink-3">
                  {msg.chat_title
                    ? t("channels.telegram.tp.groupContext", {
                        name: msg.chat_title,
                      })
                    : t("channels.telegram.tp.dmContext")}
                </span>
                <RoutingBadge msg={msg} />
              </div>
              {msg.content ? (
                <p className="mt-1 line-clamp-2 whitespace-pre-wrap break-words text-[12.5px] text-tp-ink-2">
                  {msg.content}
                </p>
              ) : null}
              {msg.media ? (
                <MediaPreview
                  message={msg}
                  baseUrl={mediaBaseUrl}
                  onPhotoClick={onPhotoClick}
                />
              ) : null}
            </div>
            {msg.reply_deadline_ms && msg.reply_total_ms ? (
              <CountdownRing
                size={16}
                strokeWidth={2}
                remainingMs={msg.reply_deadline_ms}
                totalMs={msg.reply_total_ms}
                label={t("channels.telegram.tp.replyDueAria", {
                  s: Math.ceil(msg.reply_deadline_ms / 1000),
                })}
                className="shrink-0"
                data-testid={`tg-reply-ring-${msg.id}`}
              />
            ) : null}
          </motion.li>
        );
      })}
    </motion.ul>
  );
}

/* ------------------------------------------------------------------ */
/*                          Routing badge                              */
/* ------------------------------------------------------------------ */

interface BadgeSpec {
  Icon: React.ComponentType<{ className?: string }>;
  labelKey: string;
  tone: "ok" | "warn" | "info" | "amber";
  testId: string;
}

function routingSpec(msg: TelegramMessage): BadgeSpec {
  if (msg.kind === "private") {
    return {
      Icon: MessageCircle,
      labelKey: "channels.telegram.tp.routePrivate",
      tone: "amber",
      testId: "route-private",
    };
  }
  if (msg.mention_reason === "mention") {
    return {
      Icon: AtSign,
      labelKey: "channels.telegram.tp.routeMention",
      tone: "ok",
      testId: "route-mention",
    };
  }
  if (msg.mention_reason === "reply_to_bot") {
    return {
      Icon: Reply,
      labelKey: "channels.telegram.tp.routeReply",
      tone: "ok",
      testId: "route-reply",
    };
  }
  return {
    Icon: EyeOff,
    labelKey: "channels.telegram.tp.routeIgnored",
    tone: "info",
    testId: "route-ignored",
  };
}

const badgeTone: Record<BadgeSpec["tone"], string> = {
  ok: "bg-tp-ok-soft text-tp-ok border-tp-ok/25",
  warn: "bg-tp-warn-soft text-tp-warn border-tp-warn/25",
  info: "bg-tp-glass-inner-strong text-tp-ink-3 border-tp-glass-edge",
  amber: "bg-tp-amber-soft text-tp-amber border-tp-amber/25",
};

function RoutingBadge({ msg }: { msg: TelegramMessage }): React.ReactElement {
  const { t } = useTranslation();
  const spec = routingSpec(msg);
  const label = t(spec.labelKey);
  return (
    <span
      data-testid={`tg-${spec.testId}-${msg.id}`}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-[1px]",
        "font-mono text-[10px] uppercase tracking-[0.08em]",
        badgeTone[spec.tone],
      )}
      title={label}
    >
      <spec.Icon className="h-3 w-3" aria-hidden="true" />
      {label}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*                           Media preview                             */
/* ------------------------------------------------------------------ */

function MediaPreview({
  message,
  baseUrl,
  onPhotoClick,
}: {
  message: TelegramMessage;
  baseUrl: string;
  onPhotoClick?: (msg: TelegramMessage) => void;
}) {
  const { t } = useTranslation();
  const media = message.media;
  if (!media) return null;
  const src = resolveMediaUrl(baseUrl, media.local_path);

  if (media.kind === "photo") {
    return (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onPhotoClick?.(message);
        }}
        className={cn(
          "mt-2 inline-flex overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass-inner",
          "transition-colors hover:bg-tp-glass-inner-hover",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
        )}
        aria-label={t("channels.telegram.tp.photoPreviewTitle")}
        data-testid={`tg-photo-thumb-${message.id}`}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={message.content ?? t("channels.telegram.tp.photoPreviewTitle")}
          className="h-14 w-14 object-cover"
          loading="lazy"
        />
      </button>
    );
  }

  if (media.kind === "voice") {
    const duration = media.duration_sec
      ? `${Math.round(media.duration_sec)}s`
      : null;
    return (
      <div
        className={cn(
          "mt-2 inline-flex flex-wrap items-center gap-2 rounded-lg border border-tp-glass-edge",
          "bg-tp-glass-inner px-2 py-1 text-[11.5px] text-tp-ink-2",
        )}
        data-testid={`tg-voice-${message.id}`}
      >
        <Mic className="h-3.5 w-3.5 text-tp-amber" aria-hidden="true" />
        <span className="font-mono text-[11px] text-tp-ink-3">
          {t("channels.telegram.tp.voiceLabel")}
          {duration ? ` · ${duration}` : ""}
        </span>
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <audio
          controls
          preload="none"
          src={src}
          className="h-7 max-w-[220px]"
        />
      </div>
    );
  }

  return <DocChip media={media} src={src} messageId={message.id} />;
}

function DocChip({
  media,
  src,
  messageId,
}: {
  media: TelegramMedia;
  src: string;
  messageId: string;
}) {
  const label = media.filename ?? media.local_path.split("/").pop() ?? "file";
  return (
    <a
      href={src}
      target="_blank"
      rel="noreferrer"
      onClick={(e) => e.stopPropagation()}
      className={cn(
        "mt-2 inline-flex items-center gap-2 rounded-lg border border-tp-glass-edge",
        "bg-tp-glass-inner px-2 py-1 text-[11.5px] text-tp-ink-2",
        "transition-colors hover:bg-tp-glass-inner-hover hover:text-tp-ink",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40",
      )}
      data-testid={`tg-doc-${messageId}`}
    >
      <FileText className="h-3.5 w-3.5 text-tp-ink-3" aria-hidden="true" />
      <span className="truncate font-mono text-[11px]">{label}</span>
      {typeof media.size_bytes === "number" ? (
        <span className="text-[10px] text-tp-ink-4">
          {formatBytes(media.size_bytes)}
        </span>
      ) : null}
    </a>
  );
}

/* ------------------------------------------------------------------ */
/*                               Helpers                               */
/* ------------------------------------------------------------------ */

function resolveMediaUrl(baseUrl: string, path: string): string {
  if (!baseUrl) return path;
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  const sep = baseUrl.endsWith("/") || path.startsWith("/") ? "" : "/";
  return `${baseUrl}${sep}${path}`;
}

function formatTs(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "--:--";
  const d = new Date(ms);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default MessageList;
