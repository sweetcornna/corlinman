"use client";

import Link from "next/link";
import { useTranslation } from "react-i18next";
import { MessageSquareText, Play, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { TableCell, TableRow } from "@/components/ui/table";
import type { SessionSummary } from "@/lib/api/sessions";
import { SessionCostCells } from "@/components/sessions/session-cost-cells";

/**
 * Single row in the sessions list — extracted so the page module stays
 * focused on data fetching + scaffolding. Mirrors the shape of the rows on
 * `/admin/agents` (mono-font key, light text for secondary metadata, an
 * action button anchored to the right).
 *
 * Timestamps are unix milliseconds (per Agent A's wire contract). We
 * format with `new Date(ms).toLocaleString()` so the operator's locale is
 * honored automatically. `last_seen_at_ms` is rendered as a separate
 * "freshness" column when present.
 */

interface SessionRowProps {
  session: SessionSummary;
  onReplay: (session: SessionSummary) => void;
  onDelete: (session: SessionSummary) => void;
}

function formatTime(ms: number): string {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return String(ms);
  return d.toLocaleString();
}

export function SessionRow({ session, onReplay, onDelete }: SessionRowProps) {
  const { t } = useTranslation();
  // Fall back to the message timestamp when the backend doesn't surface a
  // separate `last_seen_at_ms` — older gateways don't track typing/heartbeat.
  const lastSeen = session.last_seen_at_ms ?? session.last_message_at;
  return (
    <TableRow
      className="border-b border-sg-border"
      data-testid={`session-row-${session.session_key}`}
    >
      <TableCell className="pl-4 font-mono text-[13px] text-sg-ink">
        {session.session_key}
      </TableCell>
      <TableCell className="font-mono text-xs text-sg-ink-4">
        {session.message_count}
      </TableCell>
      <TableCell className="text-xs text-sg-ink-3">
        <time dateTime={new Date(session.last_message_at).toISOString()}>
          {formatTime(session.last_message_at)}
        </time>
      </TableCell>
      <TableCell
        className="text-xs text-sg-ink-3"
        data-testid={`session-last-seen-${session.session_key}`}
      >
        <time dateTime={new Date(lastSeen).toISOString()}>
          {formatTime(lastSeen)}
        </time>
      </TableCell>
      {/* W2.3 cost enrichment — 3 lazy-fetched cells (total, avg turn, last tool). */}
      <SessionCostCells sessionKey={session.session_key} />
      <TableCell className="pr-4 text-right">
        <div className="inline-flex items-center gap-1.5">
          <Button
            asChild
            type="button"
            variant="outline"
            size="sm"
            data-testid={`session-continue-${session.session_key}`}
          >
            <Link
              href={`/chat?session=${encodeURIComponent(session.session_key)}`}
              aria-label={`${t("sessions.continueInChat")} ${session.session_key}`}
            >
              <MessageSquareText className="h-3.5 w-3.5" aria-hidden="true" />
              {t("sessions.continueInChat")}
            </Link>
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onReplay(session)}
            data-testid={`session-replay-${session.session_key}`}
            aria-label={`${t("sessions.replay")} ${session.session_key}`}
          >
            <Play className="h-3.5 w-3.5" aria-hidden="true" />
            {t("sessions.replay")}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onDelete(session)}
            data-testid={`session-delete-${session.session_key}`}
            aria-label={t("sessions.deleteAriaLabel", {
              key: session.session_key,
            })}
            className="text-sg-err hover:bg-sg-err-soft hover:text-sg-err"
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            {t("sessions.delete")}
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}
