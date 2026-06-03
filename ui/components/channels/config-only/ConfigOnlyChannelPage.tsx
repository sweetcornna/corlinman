"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { ChannelShell } from "@/components/channels/channel-shell";
import { ChannelEnableSwitch } from "@/components/channels/channel-enable-switch";
import { ChannelConfigEditor } from "@/components/channels/ChannelConfigEditor";
import { useMotionVariants } from "@/lib/motion";
import type { ChannelName } from "@/lib/api";
import type { ConfigOnlyStatusResponse } from "@/lib/api/full-inbox-channel";

/**
 * Shared admin page for the config-only channels — WeChat-Official /
 * QQ-Official. Modelled on the QQ page's ChannelShell layout but with NO
 * messages feed and NO send affordance (these channels expose status +
 * non-secret config only):
 *
 *   [ ChannelShell (title + LiveDot + connection label) ]
 *     [ status / config panel (GlassPanel soft) ]
 *
 * Data: `/admin/channels/{ch}/status` (10s poll). `online` is always false
 * for these channels, so the LiveDot reflects configured+enabled instead.
 */

export interface ConfigOnlyChannelPageProps {
  /** Stable channel id — also the `[channels.<id>]` config key. */
  channel: Extract<ChannelName, "wechat_official" | "qq_official">;
  /** i18n namespace root, e.g. "channels.wechat_official.tp". */
  nsKey: string;
  /** Prefix for `data-testid`s, e.g. "wechat_official". */
  testIdPrefix: string;
  fetchStatus: () => Promise<ConfigOnlyStatusResponse>;
}

export function ConfigOnlyChannelPage({
  channel,
  nsKey,
  testIdPrefix,
  fetchStatus,
}: ConfigOnlyChannelPageProps) {
  const { t } = useTranslation();
  const variants = useMotionVariants();

  const statusQuery = useQuery<ConfigOnlyStatusResponse>({
    queryKey: ["admin", "channels", channel, "status"],
    queryFn: fetchStatus,
    refetchInterval: 10_000,
    retry: false,
  });

  const status = statusQuery.data;
  const offline = statusQuery.isError;
  const configured = status?.configured ?? false;
  const enabled = status?.enabled ?? false;

  // No live runtime — connection reflects configured + enabled state.
  const connected = configured && enabled && !offline;
  const connectionLabel = offline
    ? t(`${nsKey}.state.offline`)
    : !configured
      ? t(`${nsKey}.state.notConfigured`)
      : !enabled
        ? t(`${nsKey}.state.disabled`)
        : t(`${nsKey}.state.configured`);

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <ChannelShell
        channelId={channel}
        title={t(`${nsKey}.title`)}
        subtitle={t(`${nsKey}.subtitle`)}
        connected={connected}
        connectionLabel={connectionLabel}
      >
        {statusQuery.isPending ? (
          <GlassPanel
            variant="strong"
            aria-hidden
            className="h-[180px] animate-pulse"
          />
        ) : offline ? (
          <OfflineBlock
            nsKey={nsKey}
            testIdPrefix={testIdPrefix}
            message={(statusQuery.error as Error | undefined)?.message}
          />
        ) : (
          <>
            <Hero
              status={status}
              nsKey={nsKey}
              channel={channel}
              testIdPrefix={testIdPrefix}
            />
            <ConfigPanel
              status={status}
              nsKey={nsKey}
              testIdPrefix={testIdPrefix}
            />
            <ChannelConfigEditor
              channel={channel}
              configKeys={status?.config_keys ?? {}}
              onSaved={() => void statusQuery.refetch()}
            />
          </>
        )}
      </ChannelShell>
    </motion.div>
  );
}

/* ------------------------------------------------------------------ */
/*                                Hero                                 */
/* ------------------------------------------------------------------ */

function Hero({
  status,
  nsKey,
  channel,
  testIdPrefix,
}: {
  status: ConfigOnlyStatusResponse | undefined;
  nsKey: string;
  channel: ChannelName;
  testIdPrefix: string;
}) {
  const { t } = useTranslation();
  const configured = status?.configured ?? false;
  const enabled = status?.enabled ?? false;

  const prose = !configured
    ? t(`${nsKey}.proseNotConfigured`)
    : !enabled
      ? t(`${nsKey}.proseDisabled`)
      : t(`${nsKey}.proseConfigured`);

  return (
    <GlassPanel
      variant="strong"
      as="section"
      className="relative overflow-hidden p-7"
      data-testid={`${testIdPrefix}-hero`}
    >
      <div
        aria-hidden
        className="pointer-events-none absolute bottom-[-90px] right-[-40px] h-[240px] w-[360px] rounded-full opacity-60 blur-3xl"
        style={{
          background:
            "radial-gradient(closest-side, var(--tp-amber-glow), transparent 70%)",
        }}
      />
      <div className="relative flex min-w-0 flex-col gap-4">
        <span className="font-mono text-[11px] text-tp-ink-3">
          {t(`${nsKey}.configOnlyNote`)}
        </span>
        <p className="max-w-[72ch] text-[14.5px] leading-[1.6] text-tp-ink-2">
          {prose}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          <ChannelEnableSwitch
            channel={channel}
            invalidateOnSuccess={[["admin", "channels", channel, "status"]]}
          />
        </div>
      </div>
    </GlassPanel>
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
  status: ConfigOnlyStatusResponse | undefined;
  nsKey: string;
  testIdPrefix: string;
}) {
  const { t } = useTranslation();
  const entries = Object.entries(status?.config_keys ?? {});

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
          <h2 className="text-[14px] font-medium text-tp-ink">
            {t(`${nsKey}.configTitle`)}
          </h2>
          <p className="text-[12px] text-tp-ink-3">{t(`${nsKey}.configHint`)}</p>
        </div>
        <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-tp-ink-4">
          {t(`${nsKey}.configReadOnly`)}
        </span>
      </header>

      {entries.length === 0 ? (
        <p className="rounded-lg border border-dashed border-tp-glass-edge bg-tp-glass-inner p-3 text-[12px] text-tp-ink-4">
          {t(`${nsKey}.configEmpty`)}
        </p>
      ) : (
        <dl className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          {entries.map(([key, value]) => (
            <div key={key} className="space-y-1">
              <dt className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-tp-ink-4">
                {key}
              </dt>
              <dd>
                {Array.isArray(value) ? (
                  value.length === 0 ? (
                    <code className="block truncate rounded-md border border-tp-glass-edge bg-tp-glass-inner px-2 py-1 font-mono text-[11.5px] text-tp-ink-4">
                      []
                    </code>
                  ) : (
                    <div className="flex flex-wrap gap-1">
                      {value.map((v, i) => (
                        <code
                          key={`${v}-${i}`}
                          className="rounded-md border border-tp-glass-edge bg-tp-glass-inner px-1.5 py-0.5 font-mono text-[11px] text-tp-ink-2"
                        >
                          {v}
                        </code>
                      ))}
                    </div>
                  )
                ) : (
                  <code
                    className={cn(
                      "block truncate rounded-md border border-tp-glass-edge",
                      "bg-tp-glass-inner px-2 py-1 font-mono text-[11.5px] text-tp-ink-2",
                    )}
                    title={value}
                  >
                    {value}
                  </code>
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </GlassPanel>
  );
}

/* ------------------------------------------------------------------ */
/*                            Offline block                            */
/* ------------------------------------------------------------------ */

function OfflineBlock({
  nsKey,
  testIdPrefix,
  message,
}: {
  nsKey: string;
  testIdPrefix: string;
  message?: string;
}) {
  const { t } = useTranslation();
  const firstLine = message
    ?.split(/\r?\n/)
    .find((ln) => ln.trim().length > 0)
    ?.trim();
  const isHtmlDump = firstLine?.startsWith("<");
  const short =
    !isHtmlDump && firstLine && firstLine.length > 180
      ? firstLine.slice(0, 180) + "…"
      : !isHtmlDump
        ? firstLine
        : undefined;
  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col items-center gap-2 p-8 text-center"
      data-testid={`${testIdPrefix}-offline-block`}
    >
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
        {t(`${nsKey}.offlineTitle`)}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-2">
        {t(`${nsKey}.offlineHint`)}
      </p>
      {short ? (
        <p
          className="max-w-full truncate font-mono text-[11px] text-tp-ink-4"
          title={message}
        >
          {short}
        </p>
      ) : null}
    </GlassPanel>
  );
}

export default ConfigOnlyChannelPage;
