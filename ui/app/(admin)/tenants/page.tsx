"use client";

/**
 * Tenants admin page (Phase 4 W1 4-1B).
 *
 * Operator-only surface for the multi-tenant boundary. Lists every row
 * in `tenants.sqlite` and exposes a Create-tenant modal (see
 * `<CreateTenantDialog />`). Acceptance is documented in the W1 4-1B
 * brief — at minimum:
 *   - Renders 2+ tenants from /admin/tenants
 *   - Submitting the create form refreshes the list on 201
 *   - Surfaces server 400/409 errors inline
 *   - Renders a "multi-tenant mode is off" banner on 403 tenants_disabled
 *
 * Auth: this page sits inside the `(admin)` route group, so the layout's
 * `getSession()` guard already enforces authentication. Operator-only
 * scoping (vs tenant-admin) lands with the Rust-side role check in a
 * follow-up; for now any authenticated user reaches the page.
 */

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Plus, RefreshCw } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetchTenants } from "@/lib/api/tenants";
import type { TenantsListState, TenantRow } from "@/lib/api/tenants";
import { CreateTenantDialog } from "@/components/tenants/create-tenant-dialog";

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function TenantsPage() {
  const { t } = useTranslation();
  const [createOpen, setCreateOpen] = React.useState(false);

  const query = useQuery<TenantsListState>({
    queryKey: ["admin", "tenants"],
    queryFn: fetchTenants,
    retry: false,
  });

  const data = query.data;
  const tenants: TenantRow[] = data?.kind === "ok" ? data.data.tenants : [];

  return (
    <>
      <header className="flex items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("tenants.title")}
          </h1>
          <p className="text-sm text-tp-ink-3">{t("tenants.subtitle")}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => query.refetch()}
            disabled={query.isFetching}
            aria-label={t("tenants.refreshAria")}
            data-testid="tenants-refresh"
          >
            <RefreshCw
              className={query.isFetching ? "h-3 w-3 animate-spin" : "h-3 w-3"}
            />
            {t("tenants.refresh")}
          </Button>
          <Button
            size="sm"
            onClick={() => setCreateOpen(true)}
            disabled={data?.kind === "disabled"}
            data-testid="tenants-add-btn"
          >
            <Plus className="h-3 w-3" />
            {t("tenants.add")}
          </Button>
        </div>
      </header>

      {data?.kind === "disabled" ? (
        <DisabledBanner />
      ) : data?.kind === "unauthenticated" ? (
        <Alert variant="danger" data-testid="tenants-unauthenticated">
          {t("tenants.unauthenticated")}
        </Alert>
      ) : (
        <section className="overflow-hidden rounded-lg border border-tp-glass-edge bg-tp-glass">
          <Table>
            <TableHeader>
              <TableRow className="border-b border-tp-glass-edge hover:bg-transparent">
                <TableHead className="pl-4">
                  {t("tenants.colSlug")}
                </TableHead>
                <TableHead>{t("tenants.colDisplayName")}</TableHead>
                <TableHead className="w-56">
                  {t("tenants.colCreatedAt")}
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.isPending ? (
                Array.from({ length: 3 }).map((_, i) => (
                  <TableRow
                    key={`sk-${i}`}
                    className="border-b border-tp-glass-edge"
                  >
                    {Array.from({ length: 3 }).map((_, j) => (
                      <TableCell
                        key={j}
                        className={j === 0 ? "pl-4" : undefined}
                      >
                        <Skeleton className="h-4 w-24" />
                      </TableCell>
                    ))}
                  </TableRow>
                ))
              ) : data?.kind === "error" ? (
                <TableRow>
                  <TableCell
                    colSpan={3}
                    className="py-10 text-center text-sm text-sg-err"
                    data-testid="tenants-load-failed"
                  >
                    {t("tenants.loadFailedRetry", { msg: data.message })}
                  </TableCell>
                </TableRow>
              ) : tenants.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={3}
                    className="py-10 text-center text-sm text-tp-ink-3"
                  >
                    {t("tenants.empty")}
                  </TableCell>
                </TableRow>
              ) : (
                tenants.map((tn) => (
                  <TableRow
                    key={tn.tenant_id}
                    className="border-b border-tp-glass-edge transition-colors hover:bg-tp-glass-inner-hover"
                    data-testid={`tenant-row-${tn.tenant_id}`}
                  >
                    <TableCell className="pl-4">
                      <Badge variant="secondary" className="font-mono">
                        {tn.tenant_id}
                      </Badge>
                    </TableCell>
                    <TableCell>{tn.display_name}</TableCell>
                    <TableCell className="text-xs text-tp-ink-3">
                      {formatTime(tn.created_at)}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </section>
      )}

      <CreateTenantDialog open={createOpen} onOpenChange={setCreateOpen} />
    </>
  );
}

function DisabledBanner(): React.ReactElement {
  const { t } = useTranslation();
  return (
    <section
      className="rounded-lg border border-tp-glass-edge bg-tp-glass p-6"
      data-testid="tenants-disabled-banner"
    >
      <h2 className="text-base font-medium text-tp-ink">
        {t("tenants.disabledTitle")}
      </h2>
      <p className="mt-1 text-sm text-tp-ink-3">{t("tenants.disabledHint")}</p>
    </section>
  );
}
