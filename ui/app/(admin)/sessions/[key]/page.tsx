"use client";

/**
 * `/admin/sessions/[key]` — Phase 4 Wave 2 session detail shell.
 *
 * W2.3 scope is intentionally narrow: render a minimal page header + mount
 * the sticky `<CostFooter>` so operators see cumulative session cost/timing.
 * The rich per-turn drill-down is W2.2's responsibility (it owns
 * `[key]/turns/[turn_id]/page.tsx`) and the live event timeline is W2.1.
 *
 * Until those land we ship the footer alone — a small but real UX win, since
 * today there's no way to see total cost for a session at all.
 *
 * TODO(W2.2-followup): when a `GET /admin/sessions/{key}/turns` listing
 * endpoint exists, render a horizontal "Past turns" pill row at the top of
 * the live timeline so operators can click directly through to
 * `/admin/sessions/{key}/turns/{turn_id}`. Today no such endpoint exists,
 * so the drill-down page is only reachable via deep link.
 */

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useTranslation } from "react-i18next";
import { ChevronLeft } from "lucide-react";

import { CostFooter } from "@/components/sessions/cost-footer";
import { EventTimeline } from "@/components/sessions/event-timeline";
import { Button } from "@/components/ui/button";

export default function SessionDetailPage() {
  const { t } = useTranslation();
  const params = useParams<{ key: string }>();
  const rawKey = Array.isArray(params?.key) ? params.key[0] : params?.key;
  const sessionKey = rawKey ? decodeURIComponent(rawKey) : "";

  return (
    // Sticky-positioning the footer requires a scrollable ancestor — the
    // outermost wrapper here doubles as that ancestor.
    <div className="relative flex min-h-[60vh] flex-col">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1">
          <Button
            asChild
            variant="ghost"
            size="sm"
            className="-ml-2 h-7 px-2 text-tp-ink-3 hover:text-tp-ink"
          >
            <Link href="/admin/sessions">
              <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
              {t("sessions.title")}
            </Link>
          </Button>
          <h1 className="font-mono text-lg font-semibold tracking-tight">
            {sessionKey}
          </h1>
          <p className="text-sm text-tp-ink-3">{t("sessions.subtitle")}</p>
        </div>
      </header>

      {/* W2.1 — live SSE-driven event timeline. */}
      <section className="mt-4 flex-1 rounded-lg border border-tp-glass-edge bg-tp-glass p-4 sm:p-6">
        {sessionKey ? (
          <EventTimeline sessionKey={sessionKey} />
        ) : (
          <p className="text-sm text-tp-ink-3">{t("sessions.empty")}</p>
        )}
      </section>

      {sessionKey ? <CostFooter sessionKey={sessionKey} /> : null}
    </div>
  );
}
