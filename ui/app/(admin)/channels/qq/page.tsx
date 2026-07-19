"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { AlertTriangle } from "@/components/icons";
import { toast } from "sonner";

import { useMotionVariants } from "@/lib/motion";
import {
  fetchQqStatus,
  reconnectQq,
  updateQqKeywords,
  type QqStatus,
} from "@/lib/api";
import { ChannelShell } from "@/components/channels/channel-shell";
import { QqHero } from "@/components/channels/qq/qq-hero";
import { QqStatsRow } from "@/components/channels/qq/qq-stats-row";
import { QqAccountPanel } from "@/components/channels/qq/qq-account-panel";
import { QqFiltersPanel } from "@/components/channels/qq/qq-filters-panel";
import { QqMessagesPanel } from "@/components/channels/qq/qq-messages-panel";
import { ChannelConfigEditor } from "@/components/channels/ChannelConfigEditor";
import { QzonePanel } from "@/components/scheduler/qzone-panel";
import {
  QqHeroSkeleton,
  QqOfflineBlock,
} from "@/components/channels/qq/qq-list-states";
import {
  deriveConnection,
  formatRelativeAgo,
  normaliseRecent,
} from "@/components/channels/qq/qq-util";
import { ScanLoginDialog } from "./ScanLoginDialog";

/**
 * QQ channel admin — Phase 5e Tidepool cutover.
 *
 * Layout:
 *   [ ChannelShell (title + LiveDot + actions) ]
 *     [ QqHero (glass strong, prose + reconnect + scan-login) ]
 *     [ QqStatsRow — Inbound · Chats · Keywords · Throttled ]
 *     [ QqAccountPanel │ QqFiltersPanel ]  (lg: 2-col)
 *     [ ChannelConfigEditor ]
 *     [ QzonePanel — QZone daily publishing + auto-reply (borrows the
 *       NapCat login state, so it lives with the channel) ]
 *     [ QqMessagesPanel (LogRow dense feed) ]
 *
 * Data flow preserved from pre-cutover:
 *   - /admin/channels/qq/status          (10s poll)
 *   - /admin/channels/qq/keywords        (POST on save)
 *   - /admin/channels/qq/reconnect       (POST button)
 *   - Scan-login flow lives in <ScanLoginDialog>.
 *
 * Keywords state uses a local draft initialised once from the server
 * snapshot so in-flight edits don't flash back to server values on each
 * 10s refetch. The save button is disabled until the draft diverges.
 *
 * Tidepool primitives in use: `ChannelShell` (shared with Telegram),
 * `GlassPanel`, `StatChip`, `StreamPill`, `LogRow`.
 */

export default function QqChannelPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const qc = useQueryClient();

  const status = useQuery<QqStatus>({
    queryKey: ["admin", "channels", "qq"],
    queryFn: fetchQqStatus,
    refetchInterval: 10_000,
    retry: false,
  });

  // 1-Hz tick for the "last inbound N seconds ago" hero prose.
  const [now, setNow] = React.useState<number>(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);

  const [draft, setDraft] = React.useState<Record<string, string[]>>({});
  const [draftInit, setDraftInit] = React.useState(false);
  React.useEffect(() => {
    if (status.data && !draftInit) {
      setDraft(status.data.group_keywords ?? {});
      setDraftInit(true);
    }
  }, [status.data, draftInit]);

  const saveMutation = useMutation({
    mutationFn: (next: Record<string, string[]>) => updateQqKeywords(next),
    onSuccess: () => {
      toast.success(t("channels.saveSuccess"));
      qc.invalidateQueries({ queryKey: ["admin", "channels", "qq"] });
    },
    onError: (err) =>
      toast.error(
        t("channels.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });

  const reconnectMutation = useMutation({
    mutationFn: reconnectQq,
    onSuccess: () => {
      toast.success(t("channels.reconnectRequested"));
      qc.invalidateQueries({ queryKey: ["admin", "channels", "qq"] });
    },
    onError: (err) =>
      toast.warning(err instanceof Error ? err.message : String(err)),
  });

  const [scanLoginOpen, setScanLoginOpen] = React.useState(false);

  // ─── derived ─────────────────────────────────────────────────────────

  const offline = status.isError;
  const connection = deriveConnection(status.data);
  const connected = connection === "connected";
  const connectionLabel = t(`channels.qq.tp.state.${connection}`);

  const recentMessages = React.useMemo(() => {
    const raw = (status.data?.recent_messages ?? []) as Array<
      Record<string, unknown>
    >;
    return raw.map((m) => normaliseRecent(m));
  }, [status.data]);

  const chatCount = React.useMemo(
    () => Object.keys(status.data?.group_keywords ?? {}).length,
    [status.data],
  );

  const keywordCount = React.useMemo(() => {
    const groups = status.data?.group_keywords ?? {};
    let n = 0;
    for (const kws of Object.values(groups)) n += kws.length;
    return n;
  }, [status.data]);

  const lastInboundAgo = React.useMemo(() => {
    if (recentMessages.length === 0) return null;
    return formatRelativeAgo(recentMessages[0]!.ts, now);
  }, [recentMessages, now]);

  const dirty = React.useMemo(() => {
    if (!status.data) return false;
    return !keywordsEqual(status.data.group_keywords ?? {}, draft);
  }, [draft, status.data]);

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <ChannelShell
        channelId="qq"
        title={t("channels.title")}
        subtitle={t("channels.subtitle")}
        connected={connected}
        connectionLabel={connectionLabel}
      >
        <ScanLoginDialog
          open={scanLoginOpen}
          onOpenChange={setScanLoginOpen}
        />

        {status.isPending ? (
          <QqHeroSkeleton />
        ) : offline ? (
          <QqOfflineBlock message={(status.error as Error | undefined)?.message} />
        ) : (
          <>
            {status.data?.account_online === false && (
              <div
                role="alert"
                className="flex items-start gap-3 rounded-sg-md border border-sg-err/30 bg-sg-err-soft px-4 py-3 text-sm text-sg-err"
              >
                <AlertTriangle
                  aria-hidden
                  className="mt-0.5 h-4 w-4 shrink-0"
                />
                <div className="flex-1">
                  <p className="font-medium">
                    {t("channels.qq.accountOfflineTitle", "QQ 账号已下线")}
                  </p>
                  <p className="mt-1 text-xs text-sg-err/85">
                    {t(
                      "channels.qq.accountOfflineBody",
                      "NapCat 心跳正常但 QQ 账号被 Tencent 踢下线了。需要重新扫码登录后机器人才能收发消息。",
                    )}
                    {status.data?.account_last_error && (
                      <>
                        {" "}
                        <span className="font-mono text-sg-err/70">
                          ({status.data.account_last_error})
                        </span>
                      </>
                    )}
                  </p>
                  <button
                    type="button"
                    className="mt-2 inline-flex items-center gap-1 rounded-sg-sm border border-sg-err/30 bg-sg-err-soft px-2.5 py-1 text-xs font-medium text-sg-err transition-colors hover:bg-[color-mix(in_oklch,var(--sg-err)_18%,transparent)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-err/40"
                    onClick={() => setScanLoginOpen(true)}
                  >
                    {t("channels.qq.accountOfflineAction", "打开扫码登录")}
                  </button>
                </div>
              </div>
            )}
            <QqHero
              connection={connection}
              wsUrl={status.data?.ws_url ?? null}
              chatCount={chatCount}
              lastInboundAgo={lastInboundAgo}
              reconnecting={reconnectMutation.isPending}
              onReconnect={() => reconnectMutation.mutate()}
              onScanLogin={() => setScanLoginOpen(true)}
            />

            <QqStatsRow
              inbound={recentMessages.length}
              chats={chatCount}
              keywords={keywordCount}
              live={connection === "connected"}
            />

            <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <QqAccountPanel
                status={status.data}
                connection={connection}
                reconnecting={reconnectMutation.isPending}
                onReconnect={() => reconnectMutation.mutate()}
              />
              <QqFiltersPanel
                draft={draft}
                saving={saveMutation.isPending}
                dirty={dirty}
                onChange={setDraft}
                onSave={() => saveMutation.mutate(draft)}
              />
            </section>

            <ChannelConfigEditor
              channel="qq"
              configKeys={status.data?.config_keys ?? {}}
              onSaved={() =>
                void qc.invalidateQueries({
                  queryKey: ["admin", "channels", "qq"],
                })
              }
            />

            <section className="rounded-sg-md border border-sg-border bg-sg-inset px-4 py-4">
              <QzonePanel />
            </section>

            <QqMessagesPanel
              messages={recentMessages}
              offline={!status.data}
            />
          </>
        )}
      </ChannelShell>
    </motion.div>
  );
}

function keywordsEqual(
  a: Record<string, string[]>,
  b: Record<string, string[]>,
): boolean {
  const keysA = Object.keys(a);
  const keysB = Object.keys(b);
  if (keysA.length !== keysB.length) return false;
  for (const k of keysA) {
    const bk = b[k];
    const ak = a[k];
    if (!bk || bk.length !== ak!.length) return false;
    for (let i = 0; i < ak!.length; i++) {
      if (ak![i] !== bk[i]) return false;
    }
  }
  return true;
}
