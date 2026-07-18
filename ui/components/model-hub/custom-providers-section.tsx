"use client";

/**
 * W-B2 — separate "Custom providers" section below the built-in registry.
 * Reads from `GET /admin/providers/custom`, deletes via
 * `DELETE /admin/providers/custom/{slug}`, adds via the
 * AddCustomProviderModal which posts to `POST /admin/providers/custom`.
 *
 * Backend pending (503) renders the same "feature pending" banner used
 * by the built-in table so a v0.1 gateway doesn't toast-spam the operator.
 *
 * Extracted from `app/(admin)/providers/page.tsx` (PR4 model-hub
 * consolidation); its previously hardcoded English strings now live under
 * `providers.custom.*` in both locale bundles.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Plus, Trash2 } from "@/components/icons";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  CorlinmanApiError,
  deleteCustomProvider,
  listCustomProviders,
  type CustomProviderRow,
} from "@/lib/api";
import { AddCustomProviderModal } from "@/components/providers/add-custom-modal";
import { BackendPendingBanner, EmptyProviders } from "./shared";

export type CustomProvidersSectionProps = {
  onCustomProvidersChanged?: () => void;
};

export function CustomProvidersSection({
  onCustomProvidersChanged,
}: CustomProvidersSectionProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [addOpen, setAddOpen] = React.useState(false);
  const [deleting, setDeleting] =
    React.useState<CustomProviderRow | null>(null);

  const customs = useQuery<CustomProviderRow[]>({
    queryKey: ["admin", "providers", "custom"],
    queryFn: listCustomProviders,
    retry: false,
  });

  const backendPending =
    customs.isError &&
    customs.error instanceof CorlinmanApiError &&
    customs.error.status === 503;

  const deleteMutation = useMutation({
    mutationFn: (slug: string) => deleteCustomProvider(slug),
    onSuccess: () => {
      toast.success(
        t("providers.custom.deleteSuccess", { slug: deleting?.slug ?? "" }),
      );
      setDeleting(null);
      qc.invalidateQueries({ queryKey: ["admin", "providers", "custom"] });
      // Built-in section pulls from the same TOML — refresh both so the
      // operator doesn't see a stale ghost row.
      qc.invalidateQueries({ queryKey: ["admin", "providers"] });
      onCustomProvidersChanged?.();
    },
    onError: (err) => {
      toast.error(
        t("providers.custom.deleteFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  return (
    <>
      <header className="flex items-end justify-between gap-3 pt-2">
        <div className="space-y-1">
          <h2 className="text-lg font-semibold tracking-tight">
            {t("providers.custom.title")}
          </h2>
          <p className="text-xs text-sg-ink-3">
            {t("providers.custom.subtitle")}
          </p>
        </div>
        <Button
          size="sm"
          onClick={() => setAddOpen(true)}
          data-testid="custom-providers-add-btn"
        >
          <Plus className="h-3 w-3" />
          {t("providers.custom.add")}
        </Button>
      </header>

      <section className="space-y-3 rounded-lg border border-sg-border bg-sg-card p-4">
        {customs.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : backendPending ? (
          <BackendPendingBanner label={t("providers.backendPending")} />
        ) : customs.isError ? (
          <p className="text-xs text-sg-err">
            {t("providers.custom.loadFailed", {
              msg: (customs.error as Error).message,
            })}
          </p>
        ) : (customs.data ?? []).length === 0 ? (
          <EmptyProviders
            title={t("providers.custom.emptyTitle")}
            hint={t("providers.custom.emptyHint")}
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-sg-border hover:bg-transparent">
                <TableHead className="w-44 pl-3">
                  {t("providers.custom.colSlug")}
                </TableHead>
                <TableHead className="w-40">
                  {t("providers.custom.colKind")}
                </TableHead>
                <TableHead>{t("providers.custom.colBaseUrl")}</TableHead>
                <TableHead className="w-28">
                  {t("providers.custom.colKey")}
                </TableHead>
                <TableHead className="w-24" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {customs.data!.map((p) => (
                <TableRow
                  key={p.slug}
                  className="border-b border-sg-border"
                  data-testid={`custom-provider-row-${p.slug}`}
                >
                  <TableCell className="pl-3 font-mono text-xs">
                    {p.slug}
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary" className="font-mono">
                      {p.kind}
                    </Badge>
                  </TableCell>
                  <TableCell className="font-mono text-[11px] text-sg-ink-3">
                    {p.base_url ?? t("providers.baseUrlDefault")}
                  </TableCell>
                  <TableCell className="text-xs">
                    {p.has_api_key ? (
                      <Badge className="border-transparent bg-sg-ok-soft text-sg-ok">
                        {t("providers.custom.keySet")}
                      </Badge>
                    ) : (
                      <Badge variant="secondary">
                        {t("providers.custom.keyUnset")}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("providers.custom.deleteAria", {
                          slug: p.slug,
                        })}
                        onClick={() => setDeleting(p)}
                        data-testid={`custom-provider-delete-${p.slug}`}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      <AddCustomProviderModal
        open={addOpen}
        onOpenChange={setAddOpen}
        onCreated={() => {
          qc.invalidateQueries({
            queryKey: ["admin", "providers", "custom"],
          });
          qc.invalidateQueries({ queryKey: ["admin", "providers"] });
          onCustomProvidersChanged?.();
        }}
      />

      {/* Confirm-delete dialog — same shape as the built-in section's
          delete confirm, scoped to a single custom row. */}
      <Dialog
        open={!!deleting}
        onOpenChange={(o) => {
          if (!o) setDeleting(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {t("providers.custom.deleteTitle", {
                slug: deleting?.slug ?? "",
              })}
            </DialogTitle>
            <DialogDescription>
              {t("providers.custom.deleteBody", {
                slug: deleting?.slug ?? "",
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              {t("providers.deleteCancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={() =>
                deleting && deleteMutation.mutate(deleting.slug)
              }
              data-testid="custom-providers-confirm-delete-btn"
            >
              {deleteMutation.isPending
                ? t("providers.savingLabel")
                : t("providers.deleteConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
