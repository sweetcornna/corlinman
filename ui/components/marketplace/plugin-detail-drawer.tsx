"use client";

/**
 * `<PluginDetailDrawer>` — detail drawer for one plugin market item.
 *
 * Mirrors `<McpDetailDrawer>` minus the env-collection gate (plugins have no
 * `requires_env` flow). Install is a single POST that stages the plugin
 * disabled; lifecycle (enable/disable/delete) lives on the Installed tab.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Download, Loader2, Star } from "lucide-react";
import { toast } from "sonner";

import { Drawer } from "@/components/ui/drawer";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  CorlinmanApiError,
  getPluginMarketItem,
  installPluginMarket,
  type PluginMarketItem,
} from "@/lib/api";

export interface PluginDetailDrawerProps {
  item: PluginMarketItem | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function PluginDetailDrawer({
  item,
  open,
  onOpenChange,
}: PluginDetailDrawerProps): React.JSX.Element {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const slug = item?.slug ?? null;

  const query = useQuery({
    queryKey: ["plugin-market", slug],
    queryFn: () => getPluginMarketItem(slug as string),
    enabled: open && slug !== null,
    retry: false,
  });

  const detail = query.data;
  const headerName = detail?.name ?? item?.name ?? "";
  const headerEmoji = detail?.emoji ?? item?.emoji ?? "✦";
  const version = detail?.latest_version ?? item?.latest_version ?? "";

  const install = useMutation({
    mutationFn: () => installPluginMarket({ slug: slug as string }),
    onSuccess: () => {
      toast.success(
        t("marketplace.plugins.detail.installSuccess", { name: headerName }),
      );
      void qc.invalidateQueries({ queryKey: ["plugin-market-installed"] });
      onOpenChange(false);
    },
    onError: (err) => {
      const msg =
        err instanceof CorlinmanApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : String(err);
      toast.error(
        t("marketplace.plugins.detail.installFailed", {
          name: headerName,
          message: msg,
        }),
      );
    },
  });

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      width="lg"
      title={headerName}
      description={item?.description}
      className="bg-tp-glass-2 backdrop-blur-glass-strong backdrop-saturate-glass-strong"
      footer={
        <div className="flex w-full items-center justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
            data-testid="plugin-detail-close"
          >
            {t("marketplace.common.close")}
          </Button>
          <Button
            size="sm"
            disabled={!detail || install.isPending}
            onClick={() => install.mutate()}
            data-testid="plugin-detail-install"
          >
            {install.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : null}
            {install.isPending
              ? t("marketplace.common.installing")
              : t("marketplace.plugins.detail.install")}
          </Button>
        </div>
      }
    >
      {item ? (
        <div
          className="flex flex-col gap-5 px-5 py-5 text-sm"
          data-testid="plugin-detail-body"
        >
          <div className="flex flex-wrap items-center gap-3">
            <div
              className={cn(
                "flex h-11 w-11 shrink-0 items-center justify-center rounded-full",
                "border border-tp-amber/25 bg-tp-amber-soft text-[20px] leading-none",
              )}
              aria-hidden
            >
              <span className="opacity-85">{headerEmoji}</span>
            </div>
            <div className="min-w-0 flex-1">
              <h2 className="truncate text-[18px] font-medium leading-tight tracking-[-0.01em] text-tp-ink">
                {headerName}
              </h2>
              <div className="mt-0.5 flex flex-wrap items-center gap-2 font-mono text-[10.5px] text-tp-ink-4">
                <span>v{version}</span>
                <span aria-hidden>·</span>
                <span className="inline-flex items-center gap-1">
                  <Star className="h-3 w-3" aria-hidden />
                  {item.stars}
                </span>
                <span aria-hidden>·</span>
                <span className="inline-flex items-center gap-1">
                  <Download className="h-3 w-3" aria-hidden />
                  {item.downloads}
                </span>
              </div>
            </div>
          </div>

          <p className="text-[14px] leading-[1.6] text-tp-ink-2">
            {detail?.description ?? item.description}
          </p>

          {query.isPending ? (
            <div className="flex items-center gap-2 text-[12.5px] text-tp-ink-3">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              {t("marketplace.plugins.detail.loading")}
            </div>
          ) : null}

          {query.isError ? (
            <div
              role="alert"
              className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-xs text-red-700"
              data-testid="plugin-detail-error"
            >
              {(query.error as Error | undefined)?.message ??
                t("marketplace.plugins.detail.errorUnknown")}
            </div>
          ) : null}

          {detail?.tags && detail.tags.length > 0 ? (
            <section className="space-y-2">
              <h4 className="font-mono text-[10px] uppercase tracking-[0.12em] text-tp-ink-4">
                {t("marketplace.mcp.detail.tags")}
              </h4>
              <ul
                className="flex flex-wrap gap-1.5"
                data-testid="plugin-detail-tags"
              >
                {detail.tags.map((tag) => (
                  <li
                    key={tag}
                    className="inline-flex items-center rounded-md border border-tp-glass-edge bg-tp-glass-inner px-2 py-[3px] font-mono text-[11px] text-tp-ink-3"
                  >
                    {tag}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      ) : null}
    </Drawer>
  );
}

export default PluginDetailDrawer;
