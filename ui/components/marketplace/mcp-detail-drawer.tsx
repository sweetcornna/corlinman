"use client";

/**
 * `<McpDetailDrawer>` — detail drawer for one MCP market item, with the
 * required-env collection form gating Install.
 *
 * Mirrors `<HubSkillDetailDrawer>` (header row, description, sections) but
 * the footer Install path runs inline (single POST `/admin/mcp/install`)
 * rather than an SSE pipeline, because the MCP install is synchronous and
 * stages the server disabled.
 *
 * IMPORTANT: when the fetched detail declares a non-empty `requires_env`,
 * the drawer renders one input per key and sends the collected map in the
 * install body's `env` field. Install is blocked until every required key
 * has a non-empty value.
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
  getMcpMarketItem,
  installMcpServer,
  type McpMarketItem,
} from "@/lib/api";

export interface McpDetailDrawerProps {
  item: McpMarketItem | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function McpDetailDrawer({
  item,
  open,
  onOpenChange,
}: McpDetailDrawerProps): React.JSX.Element {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const slug = item?.slug ?? null;

  const query = useQuery({
    queryKey: ["mcp-market", slug],
    queryFn: () => getMcpMarketItem(slug as string),
    enabled: open && slug !== null,
    retry: false,
  });

  const detail = query.data;
  const headerName = detail?.name ?? item?.name ?? "";
  const headerEmoji = detail?.emoji ?? item?.emoji ?? "✦";
  const version = detail?.latest_version ?? item?.latest_version ?? "";
  // Prefer the fetched detail's requires_env (authoritative) but fall back
  // to the summary while the detail is loading.
  const requiresEnv = detail?.requires_env ?? item?.requires_env ?? [];

  // Local env-collection state, keyed by required env var name. Reset
  // whenever the drawer opens for a different slug.
  const [envValues, setEnvValues] = React.useState<Record<string, string>>({});
  React.useEffect(() => {
    setEnvValues({});
  }, [slug, open]);

  const envComplete = requiresEnv.every(
    (key) => (envValues[key] ?? "").trim().length > 0,
  );

  const install = useMutation({
    mutationFn: () => {
      const env: Record<string, string> = {};
      for (const key of requiresEnv) {
        env[key] = (envValues[key] ?? "").trim();
      }
      return installMcpServer({
        slug: slug as string,
        env: requiresEnv.length > 0 ? env : undefined,
      });
    },
    onSuccess: (server) => {
      toast.success(
        t("marketplace.mcp.detail.installSuccess", { name: server.name }),
      );
      void qc.invalidateQueries({ queryKey: ["mcp-servers"] });
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
        t("marketplace.mcp.detail.installFailed", {
          name: headerName,
          message: msg,
        }),
      );
    },
  });

  const installDisabled = !detail || !envComplete || install.isPending;

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
            data-testid="mcp-detail-close"
          >
            {t("marketplace.common.close")}
          </Button>
          <Button
            size="sm"
            disabled={installDisabled}
            onClick={() => install.mutate()}
            data-testid="mcp-detail-install"
          >
            {install.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : null}
            {install.isPending
              ? t("marketplace.common.installing")
              : t("marketplace.mcp.detail.install")}
          </Button>
        </div>
      }
    >
      {item ? (
        <div
          className="flex flex-col gap-5 px-5 py-5 text-sm"
          data-testid="mcp-detail-body"
        >
          {/* Header row */}
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

          {/* Description */}
          <p className="text-[14px] leading-[1.6] text-tp-ink-2">
            {detail?.description ?? item.description}
          </p>

          {/* Loading */}
          {query.isPending ? (
            <div className="flex items-center gap-2 text-[12.5px] text-tp-ink-3">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              {t("marketplace.mcp.detail.loading")}
            </div>
          ) : null}

          {/* Error */}
          {query.isError ? (
            <div
              role="alert"
              className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-xs text-red-700"
              data-testid="mcp-detail-error"
            >
              {(query.error as Error | undefined)?.message ??
                t("marketplace.mcp.detail.errorUnknown")}
            </div>
          ) : null}

          {/* Transport */}
          {detail?.transport ? (
            <Section title={t("marketplace.mcp.detail.transport")}>
              <span
                data-testid="mcp-detail-transport"
                className="inline-flex items-center rounded-full border border-tp-glass-edge bg-tp-glass-inner px-2.5 py-[3px] font-mono text-[10.5px] text-tp-ink-3"
              >
                {detail.transport}
              </span>
            </Section>
          ) : null}

          {/* Tags */}
          {detail?.tags && detail.tags.length > 0 ? (
            <Section title={t("marketplace.mcp.detail.tags")}>
              <ul className="flex flex-wrap gap-1.5" data-testid="mcp-detail-tags">
                {detail.tags.map((tag) => (
                  <li
                    key={tag}
                    className="inline-flex items-center rounded-md border border-tp-glass-edge bg-tp-glass-inner px-2 py-[3px] font-mono text-[11px] text-tp-ink-3"
                  >
                    {tag}
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {/* Required env — install gate */}
          {requiresEnv.length > 0 ? (
            <Section title={t("marketplace.mcp.detail.requiresEnv")}>
              <p className="mb-2 text-[12.5px] text-tp-ink-3">
                {t("marketplace.mcp.detail.requiresEnvHint")}
              </p>
              <div
                className="flex flex-col gap-2.5"
                data-testid="mcp-detail-env-form"
              >
                {requiresEnv.map((key) => (
                  <label key={key} className="flex flex-col gap-1">
                    <span className="font-mono text-[11px] text-tp-ink-2">
                      {key}
                    </span>
                    <input
                      type="text"
                      value={envValues[key] ?? ""}
                      onChange={(e) =>
                        setEnvValues((prev) => ({
                          ...prev,
                          [key]: e.target.value,
                        }))
                      }
                      placeholder={t("marketplace.mcp.detail.envPlaceholder", {
                        key,
                      })}
                      aria-label={key}
                      data-testid={`mcp-detail-env-${key}`}
                      className={cn(
                        "h-9 rounded-md border border-tp-glass-edge bg-tp-glass-inner px-2.5",
                        "font-mono text-[12.5px] text-tp-ink placeholder:text-tp-ink-4",
                        "focus:outline-none focus:ring-2 focus:ring-tp-amber/40",
                      )}
                    />
                  </label>
                ))}
              </div>
            </Section>
          ) : null}
        </div>
      ) : null}
    </Drawer>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2">
      <h4 className="font-mono text-[10px] uppercase tracking-[0.12em] text-tp-ink-4">
        {title}
      </h4>
      {children}
    </section>
  );
}

export default McpDetailDrawer;
