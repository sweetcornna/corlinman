"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { AtSign, EyeOff } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { useMotionVariants } from "@/lib/motion";
import type { FullInboxMessage } from "@/lib/api/full-inbox-channel";

/**
 * Recent-message list for the full-inbox channels (Discord / Slack /
 * Feishu). A trimmed cousin of the Telegram `MessageList` — these channels
 * have no media descriptors, so each row is a warm-glass log line with a
 * routing badge (mention vs. group), sender, chat context, and body.
 *
 * Group rows that were not answered (`routing="queued"` + `mention_reason
 * ="none"`) dim to 60% opacity, matching the Telegram "ignored" treatment.
 *
 * `nsKey` selects the per-channel i18n namespace (e.g.
 * "channels.discord.tp") so labels stay channel-specific.
 */
export function InboxMessageList({
  messages,
  nsKey,
  testIdPrefix,
}: {
  messages: FullInboxMessage[];
  nsKey: string;
  testIdPrefix: string;
}) {
  const { t } = useTranslation();
  const variants = useMotionVariants();

  if (messages.length === 0) {
    return (
      <p className="px-5 py-10 text-center text-[12.5px] text-tp-ink-3">
        {t(`${nsKey}.noUpdates`)}
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
          msg.routing === "queued" &&
          msg.mention_reason === "none";
        const isMention = msg.kind === "mention" || msg.mention_reason === "mention";
        return (
          <motion.li
            key={msg.id}
            variants={variants.listItem}
            data-testid={`${testIdPrefix}-message-${msg.id}`}
            className={cn(
              "group flex items-start gap-3 px-4 py-3 transition-colors",
              "hover:bg-tp-glass-inner-hover",
              ignored && "opacity-60",
            )}
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
                    ? t(`${nsKey}.groupContext`, { name: msg.chat_title })
                    : t(`${nsKey}.dmContext`)}
                </span>
                <RoutingBadge
                  isMention={isMention}
                  responded={msg.routing === "responded"}
                  nsKey={nsKey}
                  testId={`${testIdPrefix}-${isMention ? "route-mention" : "route-group"}-${msg.id}`}
                />
              </div>
              {msg.content ? (
                <p className="mt-1 line-clamp-2 whitespace-pre-wrap break-words text-[12.5px] text-tp-ink-2">
                  {msg.content}
                </p>
              ) : null}
            </div>
          </motion.li>
        );
      })}
    </motion.ul>
  );
}

function RoutingBadge({
  isMention,
  responded,
  nsKey,
  testId,
}: {
  isMention: boolean;
  responded: boolean;
  nsKey: string;
  testId: string;
}) {
  const { t } = useTranslation();
  const Icon = isMention ? AtSign : EyeOff;
  const labelKey = isMention ? `${nsKey}.routeMention` : `${nsKey}.routeGroup`;
  const label = t(labelKey);
  const tone = responded
    ? "bg-tp-ok-soft text-tp-ok border-tp-ok/25"
    : "bg-tp-glass-inner-strong text-tp-ink-3 border-tp-glass-edge";
  return (
    <span
      data-testid={testId}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-[1px]",
        "font-mono text-[10px] uppercase tracking-[0.08em]",
        tone,
      )}
      title={label}
    >
      <Icon className="h-3 w-3" aria-hidden="true" />
      {label}
    </span>
  );
}

function formatTs(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "--:--";
  const d = new Date(ms);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

export default InboxMessageList;
