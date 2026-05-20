"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslation } from "react-i18next";
import { ChevronRight } from "lucide-react";

// Maps URL segments to i18n keys under the `breadcrumbs.*` namespace.
const SEGMENT_KEY: Record<string, string> = {
  plugins: "breadcrumbs.plugins",
  detail: "breadcrumbs.detail",
  agents: "breadcrumbs.agents",
  rag: "breadcrumbs.rag",
  channels: "breadcrumbs.channels",
  qq: "breadcrumbs.qq",
  scheduler: "breadcrumbs.scheduler",
  approvals: "breadcrumbs.approvals",
  models: "breadcrumbs.models",
  providers: "breadcrumbs.providers",
  embedding: "breadcrumbs.embedding",
  config: "breadcrumbs.config",
  logs: "breadcrumbs.logs",
  tenants: "breadcrumbs.tenants",
  sessions: "breadcrumbs.sessions",
  account: "breadcrumbs.account",
  security: "breadcrumbs.security",
};

const NON_LINKABLE_HREFS = new Set(["/account"]);

/** Auto-derived breadcrumb from `usePathname`. */
export function Breadcrumbs() {
  const pathname = usePathname() ?? "/";
  const { t } = useTranslation();
  const segments = pathname.split("/").filter(Boolean);

  if (segments.length === 0) {
    return (
      <span className="text-sm font-medium text-foreground">
        {t("breadcrumbs.dashboard")}
      </span>
    );
  }

  const crumbs: { href: string; label: string }[] = [];
  let acc = "";
  for (const seg of segments) {
    acc += `/${seg}`;
    const key = SEGMENT_KEY[seg];
    crumbs.push({ href: acc, label: key ? t(key) : seg });
  }

  return (
    <nav aria-label="breadcrumb" className="flex items-center gap-1 text-sm">
      <Link
        href="/"
        className="inline-flex min-h-8 items-center rounded-md px-1.5 text-muted-foreground transition-colors hover:bg-tp-glass-inner hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40"
      >
        {t("breadcrumbs.dashboard")}
      </Link>
      {crumbs.map((c, i) => (
        <React.Fragment key={c.href}>
          <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/60" />
          {i === crumbs.length - 1 ? (
            <span className="font-medium text-foreground">{c.label}</span>
          ) : NON_LINKABLE_HREFS.has(c.href) ? (
            <span className="text-muted-foreground">{c.label}</span>
          ) : (
            <Link
              href={c.href as never}
              className="inline-flex min-h-8 items-center rounded-md px-1.5 text-muted-foreground transition-colors hover:bg-tp-glass-inner hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-tp-amber/40"
            >
              {c.label}
            </Link>
          )}
        </React.Fragment>
      ))}
    </nav>
  );
}
