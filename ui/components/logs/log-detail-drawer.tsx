"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { formatTimeShort } from "@/lib/format";
import { DetailDrawer } from "@/components/ui/detail-drawer";
import { JsonView } from "@/components/ui/json-view";
import { LogRow, type LogSeverity } from "@/components/ui/log-row";

/**
 * Logs-page right-rail detail drawer.
 *
 * Given the selected ring-buffer entry it renders four sections:
 *   1. Payload — JsonView of any non-canonical fields on the event. Shows
 *      an empty-state chip when the event carries no structured extras.
 *   2. Related · same trace — up to 5 rows from the ring buffer that
 *      share `trace_id`, excluding the selected row.
 *   3. Likely cause — optional static hint from the page when the error
 *      pattern matches a known signature. Phase 5 will wire an
 *      LLM-authored explanation.
 *
 * Closing is owned by the parent: clicking the same row toggles select
 * off (see page.tsx).
 */

export interface DetailLogEvent {
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  subsystem: string;
  trace_id: string;
  message: string;
  [extra: string]: unknown;
}

export interface LogDetailDrawerProps {
  event: DetailLogEvent;
  /** Ring-buffer peers used to derive the "same trace" list. */
  related: DetailLogEvent[];
  /** Optional static hint rendered as Likely-cause. */
  likelyCause?: React.ReactNode;
  className?: string;
}

const CANONICAL_KEYS = new Set([
  "ts",
  "level",
  "subsystem",
  "trace_id",
  "message",
]);

export function LogDetailDrawer({
  event,
  related,
  likelyCause,
  className,
}: LogDetailDrawerProps) {
  const { t } = useTranslation();

  const extras = React.useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(event)) {
      if (!CANONICAL_KEYS.has(k)) out[k] = v;
    }
    return out;
  }, [event]);

  const hasPayload = Object.keys(extras).length > 0;

  const sev = severityFromLevel(event.level);
  const headerPill: Record<LogSeverity, string> = {
    ok: "bg-sg-ok-soft text-sg-ok border-sg-ok/25",
    info: "bg-sg-inset-strong text-sg-ink-3 border-sg-border",
    warn: "bg-sg-warn-soft text-sg-warn border-sg-warn/25",
    err: "bg-sg-err-soft text-sg-err border-sg-err/25",
  };
  const relativeAgo = useRelativeAgo(event.ts);

  const meta = (
    <>
      <span
        className={cn(
          "rounded-md border px-2 py-[2px] font-mono text-[10px] font-medium uppercase tracking-[0.08em]",
          headerPill[sev],
        )}
      >
        {event.level}
      </span>
      <span className="font-mono text-[13px] tabular-nums text-sg-ink">
        {formatTsFull(event.ts)}
      </span>
      {relativeAgo ? (
        <span className="font-mono text-[11px] text-sg-ink-4">
          {relativeAgo}
        </span>
      ) : null}
    </>
  );

  return (
    <DetailDrawer
      title={renderMessageWithCode(event.message)}
      subsystem={event.subsystem}
      meta={meta}
      trace={{ id: event.trace_id }}
      className={className}
    >
      <DetailDrawer.Section label={t("logs.tp.sectionPayload")}>
        {hasPayload ? (
          <JsonView value={extras} />
        ) : (
          <div
            className={cn(
              "rounded-lg border border-dashed border-sg-border",
              "bg-sg-inset p-4 text-center",
              "font-mono text-[11.5px] text-sg-ink-4",
            )}
          >
            {t("logs.tp.payloadEmpty")}
          </div>
        )}
      </DetailDrawer.Section>

      <DetailDrawer.Section label={t("logs.tp.sectionRelated")}>
        {related.length === 0 ? (
          <div className="font-mono text-[11.5px] text-sg-ink-4">
            {t("logs.tp.relatedEmpty")}
          </div>
        ) : (
          <div className="flex flex-col">
            {related.slice(0, 5).map((e, i) => (
              <LogRow
                key={`${e.trace_id}-${e.ts}-${i}`}
                variant="dense"
                ts={formatTsShort(e.ts)}
                severity={severityFromLevel(e.level)}
                subsystem={e.subsystem}
                message={renderMessageWithCode(e.message)}
              />
            ))}
          </div>
        )}
      </DetailDrawer.Section>

      {likelyCause ? (
        <DetailDrawer.Section label={t("logs.tp.sectionLikely")}>
          <div className="text-[13px] leading-[1.55] text-sg-ink-2">
            {likelyCause}
          </div>
        </DetailDrawer.Section>
      ) : null}
    </DetailDrawer>
  );
}

/** Map backend `level` onto the LogRow severity vocabulary. */
export function severityFromLevel(level: DetailLogEvent["level"]): LogSeverity {
  if (level === "error") return "err";
  if (level === "warn") return "warn";
  if (level === "info") return "info";
  return "info";
}

/** `HH:mm:ss.SSS` in the viewer's local timezone (backend ISO is UTC —
 * slicing the raw string displayed UTC wall-clock, hours off local). */
export function formatTsFull(iso: string): string {
  if (!iso) return "--:--:--.---";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const d = new Date(t);
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

/** `HH:mm:ss` — the shorter form used inside related rows + main stream. */
export function formatTsShort(iso: string): string {
  if (!iso) return "--:--:--";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  return formatTimeShort(t);
}

/** Live-ish "2m ago" readout that re-renders every 30s. */
function useRelativeAgo(iso: string): string | null {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, []);
  const parsed = React.useMemo(() => {
    const t = Date.parse(iso);
    return Number.isFinite(t) ? t : null;
  }, [iso]);
  if (parsed === null) return null;
  const diff = Math.max(0, now - parsed);
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

/**
 * Wraps inline backtick-delimited segments (`code`) in <code> and
 * the asterisk-delimited segments (*em*) in <em>. This is *not* a
 * markdown renderer — we only need the two inline affordances used
 * in prototype messages, and only at one nesting level.
 */
export function renderMessageWithCode(msg: string): React.ReactNode {
  if (!msg) return msg;
  const out: React.ReactNode[] = [];
  const re = /(`[^`]+`)|(\*[^*]+\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  while ((m = re.exec(msg)) !== null) {
    if (m.index > last) out.push(msg.slice(last, m.index));
    if (m[1]) {
      out.push(
        <code
          key={`c${key}`}
          className={cn(
            "rounded-sm border px-1 py-[1px] font-mono text-[11px]",
            "bg-sg-inset-strong border-sg-border text-sg-ink",
          )}
        >
          {m[1].slice(1, -1)}
        </code>,
      );
    } else if (m[2]) {
      out.push(
        <em key={`e${key}`} className="not-italic font-medium text-sg-ink">
          {m[2].slice(1, -1)}
        </em>,
      );
    }
    key += 1;
    last = re.lastIndex;
  }
  if (last < msg.length) out.push(msg.slice(last));
  return out.length > 0 ? out : msg;
}

export default LogDetailDrawer;
