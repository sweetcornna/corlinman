"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import { AlertTriangle, RefreshCw, Send } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import { StreamPill, type StreamState } from "@/components/ui/stream-pill";
import { ChannelEnableSwitch } from "@/components/channels/channel-enable-switch";
import { ChannelConfigEditor } from "@/components/channels/ChannelConfigEditor";
import { useMotion } from "@/components/ui/motion-safe";
import { useMotionVariants } from "@/lib/motion";
import type { ChannelName } from "@/lib/api";
import type {
  FullInboxMessage,
  FullInboxSendRequest,
  FullInboxSendResponse,
  FullInboxStatusResponse,
} from "@/lib/api/full-inbox-channel";
import { InboxMessageList } from "./InboxMessageList";
import { InboxSendTestDrawer } from "./InboxSendTestDrawer";

/**
 * Shared admin page for the full-inbox channels — Discord, Slack, Feishu.
 *
 * Models the Telegram page dialect (hero + StatChip row + config panel +
 * recent-message feed) but against the uniform `ChannelStatusOut` contract:
 *   - `/admin/channels/{ch}/status`   (3s poll) → received / sent / errors
 *   - `/admin/channels/{ch}/messages` (3s poll, 20-cap)
 *   - send drawer → POST /admin/channels/{ch}/send
 *
 * Per-channel specifics (slug, i18n namespace, fetchers, test-id prefix)
 * are injected so each route's `page.tsx` stays a thin binding.
 *
 * Tidepool primitives: GlassPanel / StatChip / StreamPill / ChannelEnableSwitch.
 */

export interface FullInboxChannelPageProps {
  /** Stable channel id — also the `[channels.<id>]` config key. */
  channel: Extract<ChannelName, "discord" | "slack" | "feishu">;
  /** i18n namespace root, e.g. "channels.discord.tp". */
  nsKey: string;
  /** Prefix for `data-testid`s, e.g. "discord". */
  testIdPrefix: string;
  fetchStatus: () => Promise<FullInboxStatusResponse>;
  fetchMessages: (opts?: { limit?: number }) => Promise<FullInboxMessage[]>;
  sendTest: (body: FullInboxSendRequest) => Promise<FullInboxSendResponse>;
}

const EMPTY_MESSAGES: FullInboxMessage[] = [];

const RECEIVED_SPARK =
  "M0 22 L30 20 L60 16 L90 22 L120 14 L150 20 L180 18 L210 22 L240 16 L270 20 L300 14 L300 36 L0 36 Z";
const SENT_SPARK =
  "M0 18 L30 20 L60 16 L90 22 L120 14 L150 20 L180 18 L210 22 L240 16 L270 20 L300 14 L300 36 L0 36 Z";
const ERRORS_SPARK =
  "M0 30 L30 28 L60 30 L90 26 L120 28 L150 24 L180 26 L210 22 L240 24 L270 20 L300 22 L300 36 L0 36 Z";
const EVENT_SPARK =
  "M0 28 L30 26 L60 22 L90 24 L120 18 L150 22 L180 14 L210 18 L240 10 L270 14 L300 6 L300 36 L0 36 Z";

function deriveStreamState(
  online: boolean,
  hasError: boolean,
): StreamState {
  if (!online) return "paused";
  if (hasError) return "throttled";
  return "live";
}

function formatRelative(ms: number | null): string | null {
  if (ms === null || !Number.isFinite(ms) || ms <= 0) return null;
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

export function FullInboxChannelPage({
  channel,
  nsKey,
  testIdPrefix,
  fetchStatus,
  fetchMessages,
  sendTest,
}: FullInboxChannelPageProps) {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const { reduced } = useMotion();

  const statusQuery = useQuery<FullInboxStatusResponse>({
    queryKey: ["admin", "channels", channel, "status"],
    queryFn: fetchStatus,
    refetchInterval: 3_000,
    retry: false,
  });
  const messagesQuery = useQuery<FullInboxMessage[]>({
    queryKey: ["admin", "channels", channel, "messages"],
    queryFn: () => fetchMessages({ limit: 20 }),
    refetchInterval: 3_000,
    retry: false,
  });

  const [sendOpen, setSendOpen] = React.useState(false);

  const status = statusQuery.data;
  const offline = statusQuery.isError;
  const online = status?.online ?? false;
  const errorMessage = status?.error_message ?? null;
  const messages = messagesQuery.data ?? EMPTY_MESSAGES;

  const streamState = deriveStreamState(online, Boolean(errorMessage));

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <Hero
        title={t(`${nsKey}.title`)}
        status={status}
        offline={offline}
        streamState={streamState}
        nsKey={nsKey}
        channel={channel}
        testIdPrefix={testIdPrefix}
        onSendTest={() => setSendOpen(true)}
        onRefresh={() => {
          statusQuery.refetch();
          messagesQuery.refetch();
        }}
        fetching={statusQuery.isFetching}
      />

      {errorMessage ? (
        <ErrorBanner
          error={errorMessage}
          reduced={reduced}
          testIdPrefix={testIdPrefix}
          label={t(`${nsKey}.lastErrorBanner`)}
        />
      ) : null}

      <StatsRow status={status} live={!offline} nsKey={nsKey} />

      <ConfigPanel status={status} nsKey={nsKey} testIdPrefix={testIdPrefix} />

      {status ? (
        <ChannelConfigEditor
          channel={channel}
          configKeys={status.config_keys ?? {}}
          onSaved={() => void statusQuery.refetch()}
        />
      ) : null}

      <UpdatesFeed
        nsKey={nsKey}
        testIdPrefix={testIdPrefix}
        isPending={messagesQuery.isPending}
        isError={messagesQuery.isError}
        errorMessage={(messagesQuery.error as Error | undefined)?.message}
        messages={messages}
      />

      <InboxSendTestDrawer
        open={sendOpen}
        onOpenChange={setSendOpen}
        nsKey={nsKey}
        testIdPrefix={testIdPrefix}
        sendFn={sendTest}
      />
    </motion.div>
  );
}

/* ------------------------------------------------------------------ */
/*                                Hero                                 */
/* ------------------------------------------------------------------ */

function Hero({
  title,
  status,
  offline,
  streamState,
  nsKey,
  channel,
  testIdPrefix,
  onSendTest,
  onRefresh,
  fetching,
}: {
  title: string;
  status: FullInboxStatusResponse | undefined;
  offline: boolean;
  streamState: StreamState;
  nsKey: string;
  channel: ChannelName;
  testIdPrefix: string;
  onSendTest: () => void;
  onRefresh: () => void;
  fetching: boolean;
}) {
  const { t } = useTranslation();
  const lastEvent = formatRelative(status?.last_event_at_ms ?? null);

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
      <div className="relative flex min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2.5">
          <StreamPill
            state={offline ? "paused" : streamState}
            rate={
              offline
                ? t(`${nsKey}.pillOffline`)
                : status?.online
                  ? t(`${nsKey}.pillOnline`)
                  : t(`${nsKey}.pillPaused`)
            }
            data-testid={`${testIdPrefix}-stream-pill`}
          />
          <span className="font-mono text-[11px] text-sg-ink-3">
            {status?.configured
              ? t(`${nsKey}.runtimeConfigured`)
              : t(`${nsKey}.runtimeNotConfigured`)}
          </span>
        </div>

        <h1 className="text-balance font-sans text-[28px] font-semibold leading-[1.15] tracking-[-0.025em] text-sg-ink sm:text-[32px]">
          {title}
        </h1>

        <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-sg-ink-2">
          {offline
            ? t(`${nsKey}.proseOffline`)
            : !status?.configured
              ? t(`${nsKey}.proseNotConfigured`)
              : !status.enabled
                ? t(`${nsKey}.proseDisabled`)
                : status.online
                  ? lastEvent
                    ? t(`${nsKey}.proseOnlineEvent`, { age: lastEvent })
                    : t(`${nsKey}.proseOnlineNoEvent`)
                  : t(`${nsKey}.proseConfiguredOffline`)}
        </p>

        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          <ChannelEnableSwitch
            channel={channel}
            invalidateOnSuccess={[["admin", "channels", channel, "status"]]}
          />
          <button
            type="button"
            onClick={onSendTest}
            data-testid={`${testIdPrefix}-send-test-open`}
            className={cn(
              "inline-flex items-center gap-2 rounded-lg border border-sg-accent/35 bg-sg-accent-soft px-3 py-2",
              "text-[13px] font-medium text-sg-accent",
              "transition-colors hover:bg-[color-mix(in_oklch,var(--sg-accent)_22%,transparent)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50",
            )}
          >
            <Send className="h-3.5 w-3.5" aria-hidden />
            {t(`${nsKey}.sendTestCta`)}
          </button>
          <button
            type="button"
            onClick={onRefresh}
            disabled={fetching}
            aria-label={t(`${nsKey}.refreshAria`)}
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
            {t("common.refresh", "Refresh")}
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
  nsKey,
}: {
  status: FullInboxStatusResponse | undefined;
  live: boolean;
  nsKey: string;
}) {
  const { t } = useTranslation();
  const dash = "—";
  const offlineFoot = t(`${nsKey}.statOfflineFoot`);
  const lastEvent = formatRelative(status?.last_event_at_ms ?? null);

  return (
    <section className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4">
      <StatChip
        variant="primary"
        live={live && (status?.online ?? false)}
        label={t(`${nsKey}.statReceived`)}
        value={live && status ? status.received : dash}
        foot={live ? t(`${nsKey}.statFootReceived`) : offlineFoot}
        sparkPath={RECEIVED_SPARK}
        sparkTone="amber"
      />
      <StatChip
        label={t(`${nsKey}.statSent`)}
        value={live && status ? status.sent : dash}
        foot={live ? t(`${nsKey}.statFootSent`) : offlineFoot}
        sparkPath={SENT_SPARK}
        sparkTone="ember"
      />
      <StatChip
        label={t(`${nsKey}.statErrors`)}
        value={live && status ? status.errors : dash}
        delta={
          live && status
            ? status.errors === 0
              ? { label: t(`${nsKey}.caughtUp`), tone: "up" }
              : { label: t(`${nsKey}.needsAttention`), tone: "down" }
            : undefined
        }
        foot={live ? t(`${nsKey}.statFootErrors`) : offlineFoot}
        sparkPath={ERRORS_SPARK}
        sparkTone="ember"
      />
      <StatChip
        label={t(`${nsKey}.statLastEvent`)}
        value={live && status ? (lastEvent ?? t(`${nsKey}.statNoEvent`)) : dash}
        foot={live ? t(`${nsKey}.statFootLastEvent`) : offlineFoot}
        sparkPath={EVENT_SPARK}
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
  testIdPrefix,
  label,
}: {
  error: string;
  reduced: boolean;
  testIdPrefix: string;
  label: string;
}) {
  return (
    <div
      role="alert"
      data-testid={`${testIdPrefix}-last-error-banner`}
      className={cn(
        "flex items-start gap-2 rounded-xl border border-sg-err/40 bg-sg-err-soft px-3 py-2",
        "text-[12.5px] text-sg-err",
        !reduced && "animate-pulse-glow",
      )}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em]">
          {label}
        </div>
        <p className="mt-0.5 whitespace-pre-wrap break-words font-mono text-[11px]">
          {error}
        </p>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*                            Config panel                             */
/* ------------------------------------------------------------------ */

function ConfigPanel({
  status,
  nsKey,
  testIdPrefix,
}: {
  status: FullInboxStatusResponse | undefined;
  nsKey: string;
  testIdPrefix: string;
}) {
  const { t } = useTranslation();

  if (!status) {
    return (
      <GlassPanel
        variant="soft"
        as="section"
        className="flex flex-col gap-3 p-5"
        aria-label={t(`${nsKey}.configTitle`)}
      >
        <header>
          <h2 className="text-[14px] font-medium text-sg-ink">
            {t(`${nsKey}.configTitle`)}
          </h2>
          <p className="text-[12px] text-sg-ink-3">{t("common.loading", "loading…")}</p>
        </header>
        <div className="space-y-2">
          <div className="h-8 animate-pulse rounded-md border border-sg-border bg-sg-inset/70" />
          <div className="h-8 animate-pulse rounded-md border border-sg-border bg-sg-inset/70" />
        </div>
      </GlassPanel>
    );
  }

  const entries = Object.entries(status.config_keys ?? {});

  return (
    <GlassPanel
      variant="soft"
      as="section"
      className="flex flex-col gap-3 p-5"
      aria-label={t(`${nsKey}.configTitle`)}
      data-testid={`${testIdPrefix}-config-panel`}
    >
      <header className="flex items-center justify-between">
        <div>
          <h2 className="text-[14px] font-medium text-sg-ink">
            {t(`${nsKey}.configTitle`)}
          </h2>
          <p className="text-[12px] text-sg-ink-3">{t(`${nsKey}.configHint`)}</p>
        </div>
        <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-sg-ink-4">
          {t(`${nsKey}.configReadOnly`)}
        </span>
      </header>

      {entries.length === 0 ? (
        <p className="rounded-lg border border-dashed border-sg-border bg-sg-inset p-3 text-[12px] text-sg-ink-4">
          {t(`${nsKey}.configEmpty`)}
        </p>
      ) : (
        <dl className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          {entries.map(([key, value]) => (
            <ConfigField key={key} label={key}>
              {Array.isArray(value) ? (
                value.length === 0 ? (
                  <code className="block truncate rounded-md border border-sg-border bg-sg-inset px-2 py-1 font-mono text-[11.5px] text-sg-ink-4">
                    []
                  </code>
                ) : (
                  <div className="flex flex-wrap gap-1">
                    {value.map((v, i) => (
                      <code
                        key={`${v}-${i}`}
                        className="rounded-md border border-sg-border bg-sg-inset px-1.5 py-0.5 font-mono text-[11px] text-sg-ink-2"
                      >
                        {v}
                      </code>
                    ))}
                  </div>
                )
              ) : (
                <code
                  className="block truncate rounded-md border border-sg-border bg-sg-inset px-2 py-1 font-mono text-[11.5px] text-sg-ink-2"
                  title={value}
                >
                  {value}
                </code>
              )}
            </ConfigField>
          ))}
        </dl>
      )}
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
      <dt className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-sg-ink-4">
        {label}
      </dt>
      <dd>{children}</dd>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*                            Updates feed                             */
/* ------------------------------------------------------------------ */

function UpdatesFeed({
  nsKey,
  testIdPrefix,
  isPending,
  isError,
  errorMessage,
  messages,
}: {
  nsKey: string;
  testIdPrefix: string;
  isPending: boolean;
  isError: boolean;
  errorMessage?: string;
  messages: FullInboxMessage[];
}) {
  const { t } = useTranslation();
  return (
    <GlassPanel variant="soft" as="section" className="flex flex-col">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-sg-border px-5 py-3">
        <h2 className="text-[14px] font-medium text-sg-ink">
          {t(`${nsKey}.feedTitle`)}
        </h2>
        <span className="font-mono text-[10.5px] text-sg-ink-4">
          {isPending || isError ? "—" : `${messages.length}`}
        </span>
      </header>

      <div className="max-h-[420px] overflow-auto">
        {isPending ? (
          <FeedSkeleton />
        ) : isError ? (
          <p className="px-5 py-10 text-center font-mono text-[11.5px] text-sg-err">
            {t(`${nsKey}.feedLoadFailed`, {
              msg: errorMessage ?? "unknown error",
            })}
          </p>
        ) : (
          <InboxMessageList
            messages={messages}
            nsKey={nsKey}
            testIdPrefix={testIdPrefix}
          />
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
          className="h-[56px] animate-pulse rounded-xl border border-sg-border bg-sg-inset/70"
        />
      ))}
    </div>
  );
}

export default FullInboxChannelPage;
