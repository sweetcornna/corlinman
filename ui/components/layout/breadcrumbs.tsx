"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslation } from "react-i18next";
import { ChevronRight } from "@/components/icons";

import { segmentLabelKey } from "@/lib/nav-registry";

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
    // Segment labels derive from the nav registry (page labels + a small
    // extras map for non-page segments like detail/account/security).
    const key = segmentLabelKey(seg);
    crumbs.push({ href: acc, label: key ? t(key) : seg });
  }

  return (
    <nav
      aria-label="breadcrumb"
      className="flex min-w-0 items-center gap-1 text-sm"
    >
      <Link
        href="/"
        className="hidden min-h-8 items-center rounded-md px-1.5 text-muted-foreground transition-colors hover:bg-sg-inset hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40 sm:inline-flex"
      >
        {t("breadcrumbs.dashboard")}
      </Link>
      {crumbs.map((c, i) => (
        <React.Fragment key={c.href}>
          <ChevronRight
            className={
              i === 0
                ? "hidden h-3.5 w-3.5 text-muted-foreground/60 sm:block"
                : "h-3.5 w-3.5 shrink-0 text-muted-foreground/60"
            }
          />
          {i === crumbs.length - 1 ? (
            <span className="min-w-0 truncate font-medium text-foreground">
              {c.label}
            </span>
          ) : NON_LINKABLE_HREFS.has(c.href) ? (
            <span className="truncate text-muted-foreground">{c.label}</span>
          ) : (
            <Link
              href={c.href as never}
              className="inline-flex min-h-8 items-center rounded-md px-1.5 text-muted-foreground transition-colors hover:bg-sg-inset hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
            >
              {c.label}
            </Link>
          )}
        </React.Fragment>
      ))}
    </nav>
  );
}
