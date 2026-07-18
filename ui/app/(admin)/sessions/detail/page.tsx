"use client";

/**
 * `/admin/sessions/detail?key=...` — session detail shell.
 *
 * Uses a query string instead of a dynamic-route segment because
 * `next.config.ts` ships `output: "export"`, which forbids arbitrary
 * dynamic paths without a `generateStaticParams()` enumeration. The
 * other admin detail surfaces (`agents/detail`, `plugins/detail`) use
 * the same query-string pattern.
 *
 * Renders:
 *  - Header + back link to the sessions list
 *  - Past-turns pill row (W2.3, calls `/admin/sessions/{key}/turns`)
 *  - Live SSE-driven event timeline (W2.1)
 *  - Sticky cost footer (W2.3 — replaces the deleted [key]/page version)
 */

import * as React from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslation } from "react-i18next";
import { ChevronLeft } from "@/components/icons";

import { CostFooter } from "@/components/sessions/cost-footer";
import { EventTimeline } from "@/components/sessions/event-timeline";
import { PastTurnsPills } from "@/components/sessions/past-turns-pills";
import { Button } from "@/components/ui/button";
import { GlassPanel } from "@/components/ui/glass-panel";

export default function SessionDetailPage() {
  const { t } = useTranslation();
  const search = useSearchParams();
  const rawKey = search?.get("key") ?? "";
  // Search params come pre-decoded in next/navigation; do NOT double-
  // decode (matches agents/detail + plugins/detail behaviour).
  const sessionKey = rawKey;

  return (
    <div className="relative flex min-h-[60vh] flex-col">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1">
          <Button
            asChild
            variant="ghost"
            size="sm"
            className="-ml-2 h-7 px-2 text-sg-ink-3 hover:text-sg-ink"
          >
            <Link href="/sessions">
              <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
              {t("sessions.title")}
            </Link>
          </Button>
          <h1 className="font-mono text-lg font-semibold tracking-tight text-sg-ink">
            {sessionKey || t("sessions.empty")}
          </h1>
          <p className="text-sm text-sg-ink-3">{t("sessions.subtitle")}</p>
        </div>
      </header>

      {sessionKey ? (
        <div className="mt-4">
          <PastTurnsPills sessionKey={sessionKey} />
        </div>
      ) : null}

      <GlassPanel as="section" variant="soft" className="mt-3 flex-1 p-4 sm:p-6">
        {sessionKey ? (
          <EventTimeline sessionKey={sessionKey} />
        ) : (
          <p className="text-sm text-sg-ink-3">{t("sessions.empty")}</p>
        )}
      </GlassPanel>

      {sessionKey ? <CostFooter sessionKey={sessionKey} /> : null}
    </div>
  );
}
