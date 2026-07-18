"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  Eye,
  EyeOff,
  RefreshCw,
  Send,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import { StreamPill, type StreamState } from "@/components/ui/stream-pill";
import { ChannelEnableSwitch } from "@/components/channels/channel-enable-switch";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import { JsonView } from "@/components/ui/json-view";
import { useMotion } from "@/components/ui/motion-safe";
import { useMotionVariants } from "@/lib/motion";
import {
  fetchTelegramMessages,
  fetchTelegramStatus,
  type TelegramMessage,
  type TelegramStatusResponse,
} from "@/lib/api/telegram";
import { MessageList } from "./MessageList";
import { MediaPreviewDrawer } from "./MediaPreviewDrawer";
import { SendTestDrawer } from "./SendTestDrawer";

/**
 * Telegram Channel — Phase 5e (Tidepool) retoken.
 *
 * Layout (mirrors the Scheduler / Approvals page dialect):
 *
 *   [ hero (glass strong) — prose + StreamPill + send-test CTA ]
 *   [ StatChip × 4 — messages today · week · avg latency · active chats ]
 *   [ config (glass soft, 2-col) — webhook + filters/routing ]
 *   [ recent updates (glass soft) — filter chips + LogRow-style feed ]
 *   [ last-webhook payload (glass soft) — JsonView ]
 *
 * Data flow preserved from the pre-retoken page:
 *   - `/admin/channels/telegram/status`   (3s poll)
 *   - `/admin/channels/telegram/messages` (3s poll, 20-cap)
 *   - The 404-fallback path stays inside `fetchTelegramStatus/Messages`.
 *
 * A handful of strings are intentionally hardcoded in English (page `<h1>`,
 * stat-chip labels, photo-preview dialog title) — those are asserted by
 * `page.test.tsx` against English regex regardless of locale.
 */

type UpdateFilter = "all" | "text" | "photo" | "voice" | "doc";

const EMPTY_TELEGRAM_MESSAGES: TelegramMessage[] = [];

const UPDATES_SPARK =
  "M0 22 L30 20 L60 16 L90 22 L120 14 L150 20 L180 18 L210 22 L240 16 L270 20 L300 14 L300 36 L0 36 Z";
const LATENCY_SPARK =
  "M0 10 L30 14 L60 16 L90 20 L120 22 L150 24 L180 26 L210 28 L240 30 L270 30 L300 32 L300 36 L0 36 Z";
const CHATS_SPARK =
  "M0 28 L30 26 L60 22 L90 24 L120 18 L150 22 L180 14 L210 18 L240 10 L270 14 L300 6 L300 36 L0 36 Z";
const WEEK_SPARK =
  "M0 18 L30 20 L60 16 L90 22 L120 14 L150 20 L180 18 L210 22 L240 16 L270 20 L300 14 L300 36 L0 36 Z";

function deriveStreamState(
  connected: boolean,
  hasError: boolean,
): StreamState {
  if (!connected) return "paused";
  if (hasError) return "throttled";
  return "live";
}

function kindOfMessage(msg: TelegramMessage): UpdateFilter {
  if (!msg.media) return "text";
  if (msg.media.kind === "photo") return "photo";
  if (msg.media.kind === "voice") return "voice";
  return "doc";
}

function formatRelative(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "just now";
  const delta = Math.max(0, Date.now() - ms);
  const s = Math.floor(delta / 1000);
  if (s < 10) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export default function TelegramChannelPage() {
  const variants = useMotionVariants();
  const { reduced } = useMotion();

  const statusQuery = useQuery<TelegramStatusResponse>({
    queryKey: ["admin", "channels", "telegram", "status"],
    queryFn: fetchTelegramStatus,
    refetchInterval: 3_000,
    retry: false,
  });
  const messagesQuery = useQuery<TelegramMessage[]>({
    queryKey: ["admin", "channels", "telegram", "messages"],
    queryFn: () => fetchTelegramMessages({ limit: 20 }),
    refetchInterval: 3_000,
    retry: false,
  });

  const [sendOpen, setSendOpen] = React.useState(false);
  const [previewMessage, setPreviewMessage] =
    React.useState<TelegramMessage | null>(null);
  const [filter, setFilter] = React.useState<UpdateFilter>("all");

  const status = statusQuery.data;
  const connected = status?.connected ?? false;
  const lastError = status?.last_error ?? null;
  const offline = statusQuery.isError;
  const messages = messagesQuery.data ?? EMPTY_TELEGRAM_MESSAGES;

  const streamState = deriveStreamState(connected, Boolean(lastError));

  const filterCounts = React.useMemo(() => {
    const counts: Record<UpdateFilter, number> = {
      all: messages.length,
      text: 0,
      photo: 0,
      voice: 0,
      doc: 0,
    };
    for (const m of messages) counts[kindOfMessage(m)] += 1;
    return counts;
  }, [messages]);

  const filteredMessages = React.useMemo(() => {
    if (filter === "all") return messages;
    return messages.filter((m) => kindOfMessage(m) === filter);
  }, [messages, filter]);

  const latest = messages[0];

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <TelegramHero
        status={status}
        offline={offline}
        streamState={streamState}
        latest={latest}
        onSendTest={() => setSendOpen(true)}
        onRefresh={() => {
          statusQuery.refetch();
          messagesQuery.refetch();
        }}
        fetching={statusQuery.isFetching}
      />

      {lastError ? <ErrorBanner error={lastError} reduced={reduced} /> : null}

      <StatsRow status={status} live={!offline} />

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <WebhookPanel status={status} />
        <FiltersPanel status={status} />
      </section>

      <UpdatesFeed
        filter={filter}
        setFilter={setFilter}
        counts={filterCounts}
        isPending={messagesQuery.isPending}
        isError={messagesQuery.isError}
        errorMessage={(messagesQuery.error as Error | undefined)?.message}
        messages={filteredMessages}
        onPhotoClick={setPreviewMessage}
      />

      <DebugPanel
        payload={status?.last_webhook_payload ?? null}
        error={lastError}
      />

      <MediaPreviewDrawer
        message={previewMessage}
        mediaBaseUrl=""
        open={previewMessage !== null}
        onOpenChange={(v) => {
          if (!v) setPreviewMessage(null);
        }}
      />
      <SendTestDrawer open={sendOpen} onOpenChange={setSendOpen} />
    </motion.div>
  );
}

/* ------------------------------------------------------------------ */
/*                                Hero                                 */
/* ------------------------------------------------------------------ */

function TelegramHero({
  status,
  offline,
  streamState,
  latest,
  onSendTest,
  onRefresh,
  fetching,
}: {
  status: TelegramStatusResponse | undefined;
  offline: boolean;
  streamState: StreamState;
  latest: TelegramMessage | undefined;
  onSendTest: () => void;
  onRefresh: () => void;
  fetching: boolean;
}) {
  const host = status?.config.webhook_url
    ? hostOf(status.config.webhook_url)
    : "—";
  const activeChats = status?.stats.active_chats ?? 0;

  const latestLine = latest
    ? (() => {
        const kind = latest.media
          ? latest.media.kind === "photo"
            ? "photo"
            : latest.media.kind === "voice"
              ? "voice clip"
              : "document"
          : "message";
        const who = latest.from_username ?? "unknown sender";
        const chat = latest.chat_title ? ` in group ${latest.chat_title}` : "";
        const when = formatRelative(latest.timestamp_ms);
        return `Last update ${when} — ${kind} from ${who}${chat}.`;
      })()
    : "No inbound updates yet.";

  return (
    <GlassPanel
      variant="strong"
      as="section"
      className="relative overflow-hidden p-7"
    >
      <div
        aria-hidden
        className="pointer-events-none absolute bottom-[-90px] right-[-40px] h-[240px] w-[360px] rounded-full opacity-60 blur-3xl"
        style={{
          background:
            "radial-gradient(closest-side, var(--sg-accent-glow), transparent 70%)",
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute top-[-60px] left-[-40px] h-[180px] w-[260px] rounded-full opacity-40 blur-[50px]"
        style={{
          background:
            "radial-gradient(closest-side, color-mix(in oklch, var(--sg-accent-2) 35%, transparent), transparent 70%)",
        }}
      />

      <div className="relative flex min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2.5">
          <StreamPill
            state={offline ? "paused" : streamState}
            rate={offline ? "offline" : host}
            data-testid="tg-stream-pill"
          />
          <span className="font-mono text-[11px] text-sg-ink-3">
            {status?.runtime
              ? `runtime=${status.runtime}`
              : "runtime=unknown"}
          </span>
        </div>

        <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-sg-ink sm:text-[32px]">
          Telegram Channel
        </h1>

        <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-sg-ink-2">
          {offline ? (
            "Webhook is offline — the gateway is not answering. Panels below reflect cached data."
          ) : (
            <>
              Webhook live at <span className="font-mono text-sg-ink">{host}</span>.
              {" "}
              {activeChats} {activeChats === 1 ? "conversation" : "conversations"} open.
              {" "}
              {latestLine}
            </>
          )}
        </p>

        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          <ChannelEnableSwitch
            channel="telegram"
            invalidateOnSuccess={[["admin", "channels", "telegram"]]}
          />
          <button
            type="button"
            onClick={onSendTest}
            data-testid="tg-send-test-open"
            className={cn(
              "inline-flex items-center gap-2 rounded-lg border border-sg-accent/35 bg-sg-accent-soft px-3 py-2",
              "text-[13px] font-medium text-sg-accent",
              "transition-colors hover:bg-[color-mix(in_oklch,var(--sg-accent)_22%,transparent)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50",
            )}
          >
            <Send className="h-3.5 w-3.5" aria-hidden />
            Send test message
          </button>
          <button
            type="button"
            onClick={onRefresh}
            disabled={fetching}
            aria-label="Refresh Telegram channel state"
            className={cn(
              "inline-flex items-center gap-2 rounded-lg border border-sg-border bg-sg-inset px-3 py-2",
              "text-[13px] font-medium text-sg-ink-2",
              "transition-colors hover:bg-sg-inset-hover hover:text-sg-ink",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
              "disabled:cursor-not-allowed disabled:opacity-70",
            )}
          >
            <RefreshCw
              className={cn("h-3.5 w-3.5", fetching && "animate-spin")}
              aria-hidden
            />
            Refresh
          </button>
        </div>
      </div>
    </GlassPanel>
  );
}

/* ------------------------------------------------------------------ */
/*                              Stats row                              */
/* ------------------------------------------------------------------ */

function StatsRow({
  status,
  live,
}: {
  status: TelegramStatusResponse | undefined;
  live: boolean;
}) {
  const stats = status?.stats;
  const dash = "—";

  return (
    <section className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4">
      <StatChip
        variant="primary"
        live={live}
        label="Messages today"
        value={live && stats ? stats.messages_today : dash}
        foot={live ? "inbound webhook events" : "endpoint offline"}
        sparkPath={UPDATES_SPARK}
        sparkTone="amber"
      />
      <StatChip
        label="Messages · week"
        value={live && stats ? stats.messages_week : dash}
        foot={live ? "trailing 7-day rollup" : "endpoint offline"}
        sparkPath={WEEK_SPARK}
        sparkTone="ember"
      />
      <StatChip
        label="Avg latency"
        value={
          live && stats ? (
            <span className="flex items-baseline gap-1">
              <span className="tabular-nums">{stats.latency_p50_ms}</span>
              <span className="font-mono text-[10px] text-sg-ink-4">p50ms</span>
              <span className="mx-1 text-sg-ink-4">·</span>
              <span className="tabular-nums text-[20px] text-sg-ink-3">
                {stats.latency_p95_ms}
              </span>
              <span className="font-mono text-[10px] text-sg-ink-4">p95ms</span>
            </span>
          ) : (
            dash
          )
        }
        foot={live ? "handler dispatch" : "endpoint offline"}
        sparkPath={LATENCY_SPARK}
        sparkTone="ember"
      />
      <StatChip
        label="Active chats"
        value={live && stats ? stats.active_chats : dash}
        foot={live ? "seen in the last 24h" : "endpoint offline"}
        sparkPath={CHATS_SPARK}
        sparkTone="peach"
      />
    </section>
  );
}

/* ------------------------------------------------------------------ */
/*                             Error banner                            */
/* ------------------------------------------------------------------ */

function ErrorBanner({
  error,
  reduced,
}: {
  error: string;
  reduced: boolean;
}) {
  return (
    <div
      role="alert"
      data-testid="tg-last-error-banner"
      className={cn(
        "flex items-start gap-2 rounded-xl border border-sg-err/40 bg-sg-err-soft px-3 py-2",
        "text-[12.5px] text-sg-err",
        !reduced && "animate-pulse-glow",
      )}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em]">
          Last dispatch error
        </div>
        <p className="mt-0.5 whitespace-pre-wrap break-words font-mono text-[11px]">
          {error}
        </p>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*                            Config panels                            */
/* ------------------------------------------------------------------ */

function WebhookPanel({
  status,
}: {
  status: TelegramStatusResponse | undefined;
}) {
  const [revealed, setRevealed] = React.useState(false);
  // Don't render the token/URL testids until the config has landed — the
  // page test uses `findByTestId("tg-bot-token")` and asserts on the masked
  // pattern, which requires real bytes rather than an empty placeholder.
  if (!status) {
    return (
      <GlassPanel
        variant="soft"
        as="section"
        className="flex flex-col gap-3 p-5"
        aria-label="Telegram bot webhook configuration"
      >
        <header>
          <h2 className="text-[14px] font-medium text-sg-ink">Webhook</h2>
          <p className="text-[12px] text-sg-ink-3">loading…</p>
        </header>
        <div className="space-y-2">
          <div className="h-8 animate-pulse rounded-md border border-sg-border bg-sg-inset" />
          <div className="h-8 animate-pulse rounded-md border border-sg-border bg-sg-inset" />
        </div>
      </GlassPanel>
    );
  }
  const botToken = status.config.bot_token ?? "";
  const webhookUrl = status.config.webhook_url ?? "—";

  return (
    <GlassPanel
      variant="soft"
      as="section"
      className="flex flex-col gap-3 p-5"
      aria-label="Telegram bot webhook configuration"
    >
      <header className="flex items-center justify-between">
        <div>
          <h2 className="text-[14px] font-medium text-sg-ink">Webhook</h2>
          <p className="text-[12px] text-sg-ink-3">
            bot token + callback URL · read-only for now.
          </p>
        </div>
        <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-sg-ink-4">
          read-only
        </span>
      </header>

      <div className="space-y-3">
        <ConfigField label="Bot token">
          <div className="flex items-center gap-2">
            <code
              data-testid="tg-bot-token"
              aria-readonly="true"
              className={cn(
                "flex-1 truncate rounded-md border border-sg-border",
                "bg-sg-inset px-2 py-1 font-mono text-[11.5px] text-sg-ink-2",
              )}
            >
              {revealed ? botToken : maskToken(botToken)}
            </code>
            <button
              type="button"
              onClick={() => setRevealed((v) => !v)}
              aria-pressed={revealed}
              aria-label={revealed ? "Hide bot token" : "Reveal bot token"}
              data-testid="tg-reveal-token"
              className={cn(
                "inline-flex h-7 w-7 items-center justify-center rounded-md border border-sg-border",
                "bg-sg-inset text-sg-ink-3 transition-colors",
                "hover:bg-sg-inset-hover hover:text-sg-ink",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
              )}
            >
              {revealed ? (
                <EyeOff className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <Eye className="h-3.5 w-3.5" aria-hidden="true" />
              )}
            </button>
          </div>
        </ConfigField>

        <ConfigField label="Webhook URL">
          <code
            className={cn(
              "block truncate rounded-md border border-sg-border",
              "bg-sg-inset px-2 py-1 font-mono text-[11.5px] text-sg-ink-2",
            )}
            aria-readonly="true"
          >
            {webhookUrl}
          </code>
        </ConfigField>

        <ConfigField label="Secret token">
          <code
            className={cn(
              "block truncate rounded-md border border-sg-border",
              "bg-sg-inset px-2 py-1 font-mono text-[11.5px] text-sg-ink-2",
            )}
            aria-readonly="true"
          >
            {maskToken(status.config.secret_token)}
          </code>
        </ConfigField>
      </div>
    </GlassPanel>
  );
}

function FiltersPanel({
  status,
}: {
  status: TelegramStatusResponse | undefined;
}) {
  const messagesWeek = status?.stats.messages_week ?? 0;
  const dropPending = status?.config.drop_pending_updates ?? false;

  return (
    <GlassPanel
      variant="soft"
      as="section"
      className="flex flex-col gap-3 p-5"
      aria-label="Telegram routing filters"
    >
      <header>
        <h2 className="text-[14px] font-medium text-sg-ink">
          Filters & routing
        </h2>
        <p className="text-[12px] text-sg-ink-3">
          Group replies are gated on @mention or reply-to-bot; DMs pass
          through.
        </p>
      </header>

      <div className="grid grid-cols-2 gap-3 text-[12.5px]">
        <FilterStatCell label="Week volume" value={messagesWeek} />
        <FilterStatCell
          label="Drop pending on reconnect"
          value={dropPending ? "on" : "off"}
        />
      </div>

      <div className="rounded-lg border border-dashed border-sg-border bg-sg-inset p-3">
        <div className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4">
          Routing gates
        </div>
        <ul className="space-y-1 text-[12px] text-sg-ink-2">
          <li>• Group messages — only @mention or reply-to-bot are answered.</li>
          <li>• DMs — every message is processed.</li>
          <li>• Keyword filter — inherited from `corlinman-channels` config.</li>
        </ul>
      </div>
    </GlassPanel>
  );
}

function ConfigField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4">
        {label}
      </div>
      <div>{children}</div>
    </div>
  );
}

function FilterStatCell({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-sg-border bg-sg-inset px-3 py-2">
      <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4">
        {label}
      </div>
      <div className="mt-0.5 font-sans text-[16px] font-medium text-sg-ink">
        {value}
      </div>
    </div>
  );
}

function maskToken(token: string): string {
  if (!token) return "";
  const tail = token.slice(-4);
  return `••••••••${tail}`;
}

/* ------------------------------------------------------------------ */
/*                            Updates feed                             */
/* ------------------------------------------------------------------ */

function UpdatesFeed({
  filter,
  setFilter,
  counts,
  isPending,
  isError,
  errorMessage,
  messages,
  onPhotoClick,
}: {
  filter: UpdateFilter;
  setFilter: (next: UpdateFilter) => void;
  counts: Record<UpdateFilter, number>;
  isPending: boolean;
  isError: boolean;
  errorMessage?: string;
  messages: TelegramMessage[];
  onPhotoClick: (msg: TelegramMessage) => void;
}) {
  const options: FilterChipOption[] = [
    { value: "all", label: "All", count: counts.all },
    { value: "text", label: "Text", count: counts.text },
    { value: "photo", label: "Photo", count: counts.photo, tone: "ok" },
    { value: "voice", label: "Voice", count: counts.voice, tone: "warn" },
    { value: "doc", label: "Document", count: counts.doc, tone: "info" },
  ];

  return (
    <GlassPanel variant="soft" as="section" className="flex flex-col">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-sg-border px-5 py-3">
        <h2 className="text-[14px] font-medium text-sg-ink">Recent updates</h2>
        <FilterChipGroup
          label="update type filter"
          options={options}
          value={filter}
          onChange={(next) => setFilter(next as UpdateFilter)}
        />
      </header>

      <div className="max-h-[420px] overflow-auto">
        {isPending ? (
          <FeedSkeleton />
        ) : isError ? (
          <p className="px-5 py-10 text-center font-mono text-[11.5px] text-sg-err">
            Messages load failed: {errorMessage ?? "unknown error"}
          </p>
        ) : (
          <MessageList messages={messages} onPhotoClick={onPhotoClick} />
        )}
      </div>
    </GlassPanel>
  );
}

function FeedSkeleton() {
  return (
    <div className="space-y-2 p-5">
      {Array.from({ length: 3 }).map((_, i) => (
        <div
          key={i}
          className="h-[56px] animate-pulse rounded-xl border border-sg-border bg-sg-inset"
        />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*                             Debug panel                             */
/* ------------------------------------------------------------------ */

function DebugPanel({
  payload,
  error,
}: {
  payload: Record<string, unknown> | null;
  error: string | null;
}) {
  return (
    <GlassPanel variant="soft" as="section" className="overflow-hidden">
      <details className="group">
        <summary
          className={cn(
            "flex cursor-pointer items-center justify-between px-5 py-3",
            "text-[13.5px] font-medium text-sg-ink",
            "outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
          )}
        >
          <span>Debug</span>
          <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4 group-open:hidden">
            click to expand
          </span>
        </summary>
        <div className="space-y-3 border-t border-sg-border p-5">
          <div>
            <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4">
              Last webhook payload
            </div>
            {payload ? (
              <JsonView
                value={payload}
                data-testid="tg-debug-payload"
                className="max-h-[240px]"
              />
            ) : (
              <p
                data-testid="tg-debug-payload"
                className="rounded-lg border border-sg-border bg-sg-inset p-3 font-mono text-[11px] text-sg-ink-4"
              >
                (none)
              </p>
            )}
          </div>
          <div>
            <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4">
              Last dispatch error
            </div>
            {error ? (
              <pre
                data-testid="tg-debug-error"
                className="rounded-lg border border-sg-err/30 bg-sg-err-soft p-3 font-mono text-[11px] text-sg-err"
              >
                {error}
              </pre>
            ) : (
              <p className="text-[12px] text-sg-ink-3">No recent errors.</p>
            )}
          </div>
        </div>
      </details>
    </GlassPanel>
  );
}
