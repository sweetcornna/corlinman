"use client";

import { useTranslation } from "react-i18next";
import { GlassPanel } from "@/components/ui/glass-panel";

/**
 * Offline / empty panels for the Nodes page. Mirrors the pattern used on
 * Plugins/Skills/Characters: a soft glass panel with warm prose and a
 * single-line truncated diagnostic below (for the offline case).
 */

export function OfflineBlock({ message }: { message?: string }) {
  const { t } = useTranslation();
  // Truncate diagnostic messages — a raw fetch error can be the gateway's
  // full 404 HTML body, which blows up the layout. Cap to a single line.
  const firstLine = message
    ?.split(/\r?\n/)
    .find((ln) => ln.trim().length > 0)
    ?.trim();
  const short =
    firstLine && firstLine.length > 180
      ? firstLine.slice(0, 180) + "…"
      : firstLine;
  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col items-center gap-2 p-8 text-center"
      data-testid="nodes-offline-block"
    >
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-sg-err">
        {t("nodes.tp.offlineTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-sg-ink-2">
        {t("nodes.tp.offlineHint")}
      </p>
      {short ? (
        <p
          className="max-w-full truncate font-mono text-[11px] text-sg-ink-4"
          title={message}
        >
          {short}
        </p>
      ) : null}
    </GlassPanel>
  );
}

export function EmptyBlock() {
  const { t } = useTranslation();
  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col items-center gap-2 p-8 text-center"
      data-testid="nodes-empty-block"
    >
      <div className="text-[14px] font-medium text-sg-ink">
        {t("nodes.tp.emptyTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-sg-ink-3">
        {t("nodes.tp.emptyHint")}
      </p>
    </GlassPanel>
  );
}

/**
 * Honest "not yet available" panel for `/nodes`.
 *
 * The runner registry has NO backend endpoint (there is no
 * `GET /wstool/runners` route in the gateway), so the topology can never
 * populate. Rather than poll an always-empty mock and render the generic
 * "No runners registered" empty state — which falsely implies a working but
 * idle registry — we state plainly that the feature is not wired yet.
 *
 * Copy is intentionally inlined (not i18n keys): no existing translation
 * string truthfully describes "no backend endpoint exists", and the i18n
 * resources are out of scope for this change. See ARCH_DEBT.md "R5 — /nodes
 * runner registry" for the real fix (gateway route + i18n strings).
 */
export function NotImplementedBlock() {
  return (
    <GlassPanel
      variant="soft"
      className="flex flex-col items-center gap-2 p-8 text-center"
      data-testid="nodes-not-implemented-block"
    >
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-sg-ink-4">
        Not available
      </div>
      <div className="text-[14px] font-medium text-sg-ink">
        Node / runner registry is not yet available
      </div>
      <p className="max-w-prose text-[13px] text-sg-ink-3">
        This view has no backend endpoint yet — the gateway does not expose the
        WebSocket tool runner registry. It will light up once a{" "}
        <code className="font-mono text-sg-ink-2">GET /v1/wstool/runners</code>{" "}
        route ships and is wired here.
      </p>
    </GlassPanel>
  );
}

export default OfflineBlock;
