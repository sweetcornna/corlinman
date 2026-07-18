"use client";

/**
 * Tenant switcher (Phase 4 W1 4-1B).
 *
 * Native `<select>` dropdown that lets the operator pick the active tenant.
 * On change we append `?tenant=<slug>` to the current URL and reload via
 * `window.location.assign` — full reload is intentional so server-side
 * data fetched at layout time picks up the new scope.
 *
 * Behaviour:
 *   - Reads tenants from /admin/tenants (same call the page uses; React
 *     Query dedupes the request).
 *   - Selecting `default` strips the `?tenant=` param.
 *   - When [tenants].enabled = false the switcher renders disabled with
 *     a "multi-tenant mode is off" tooltip — kept visible so the operator
 *     knows the toggle exists; just not interactive.
 *   - When the API call fails (offline, 401), the switcher hides itself
 *     rather than showing a broken dropdown — failures already surface on
 *     the dedicated /tenants page.
 *
 * URL update logic is extracted to `buildTenantHref` (see
 * `lib/api/tenants.ts`) so it can be unit-tested without driving the
 * router.
 */

import * as React from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Building2 } from "@/components/icons";

import { fetchTenants, buildTenantHref } from "@/lib/api/tenants";
import type { TenantsListState } from "@/lib/api/tenants";
import { cn } from "@/lib/utils";

const DEFAULT_SLUG = "default";

export interface TenantSwitcherProps {
  /** Optional override — the bare `window.location.assign` is the default. */
  navigate?: (href: string) => void;
  className?: string;
}

export function TenantSwitcher({
  navigate,
  className,
}: TenantSwitcherProps): React.ReactElement | null {
  const { t } = useTranslation();
  const pathname = usePathname() ?? "/";
  const searchParams = useSearchParams();
  const search = searchParams ? `?${searchParams.toString()}` : "";

  const query = useQuery<TenantsListState>({
    queryKey: ["admin", "tenants"],
    queryFn: fetchTenants,
    // The switcher is not the source of truth for the page — failures
    // here just hide it. Don't retry-storm.
    retry: false,
    // Cache fairly aggressively; the list rarely changes.
    staleTime: 30_000,
  });

  const active =
    (searchParams?.get("tenant") ?? "").trim() || DEFAULT_SLUG;

  // Hide on hard errors. Disabled / unauth render specific affordances.
  if (query.isPending) return null;
  if (!query.data) return null;
  if (query.data.kind === "error" || query.data.kind === "unauthenticated") {
    return null;
  }

  const disabled = query.data.kind === "disabled";
  const tenants = query.data.kind === "ok" ? query.data.data.tenants : [];

  function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const next = e.target.value;
    const href = buildTenantHref(pathname, search, next, DEFAULT_SLUG);
    if (navigate) {
      navigate(href);
    } else if (typeof window !== "undefined") {
      // Full reload by design — server-rendered layout state needs to
      // re-run with the new tenant scope.
      window.location.assign(href);
    }
  }

  return (
    <label
      className={cn(
        "group relative flex h-9 items-center gap-1.5 rounded-lg border border-sg-border bg-sg-inset px-2 text-[12px] text-sg-ink-2 transition-colors",
        disabled
          ? "cursor-not-allowed opacity-60"
          : "hover:border-sg-border-strong hover:bg-sg-inset-hover hover:text-sg-ink",
        className,
      )}
      title={
        disabled ? t("tenants.switcherDisabled") : t("tenants.switcherLabel")
      }
      data-testid="tenant-switcher"
    >
      <Building2 className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="sr-only">{t("tenants.switcherSelectAria")}</span>
      <select
        aria-label={t("tenants.switcherSelectAria")}
        value={active}
        disabled={disabled}
        onChange={handleChange}
        data-testid="tenant-switcher-select"
        className="h-full min-h-8 cursor-pointer appearance-none bg-transparent pr-1 font-mono text-[12px] focus:outline-none disabled:cursor-not-allowed"
      >
        {disabled ? (
          <option value={DEFAULT_SLUG}>
            {t("tenants.switcherDisabled")}
          </option>
        ) : null}
        {tenants.map((tn) => (
          <option key={tn.tenant_id} value={tn.tenant_id}>
            {tn.tenant_id === DEFAULT_SLUG
              ? `${t("tenants.switcherDefault")} (${tn.tenant_id})`
              : tn.tenant_id}
          </option>
        ))}
      </select>
    </label>
  );
}
