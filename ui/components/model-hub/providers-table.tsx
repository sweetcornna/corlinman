"use client";

/**
 * Built-in providers table + its delete-confirm dialog.
 *
 * Extracted from `app/(admin)/providers/page.tsx` (PR4 model-hub
 * consolidation): the table portion of the old `ProvidersAdminContent`.
 * Add/Edit intents are surfaced to the host via `onAdd` / `onEdit` so the
 * editor dialog state stays in `providers-admin-content.tsx`.
 *
 * - Delete: DELETE /admin/providers/:name; a 409 surfaces the list of
 *   referencing aliases/embedding so the user can unbind them first.
 * - When the gateway returns 503 we render a dedicated empty state rather
 *   than toasting — the v0.1.x gateway simply does not ship this surface yet.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Pencil, Plus, Trash2 } from "@/components/icons";

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
  deleteProvider,
  fetchProviders,
  type ProviderView,
} from "@/lib/api";
import { TestConnectionButton } from "@/components/providers/test-connection-button";
import { BackendPendingBanner, EmptyProviders } from "./shared";

/** The server conflict body may be JSON-ish (`{"error": "...", "references":
 *  ["alias.smart", "embedding"]}`) or a plain string. Be liberal in what we
 *  accept so a mis-shaped 409 doesn't crash the page. */
function parseReferences(raw: string): string[] {
  try {
    const parsed = JSON.parse(raw) as { references?: unknown };
    if (Array.isArray(parsed.references)) {
      return parsed.references.map((r) => String(r));
    }
  } catch {
    /* not JSON */
  }
  return [raw];
}

export interface ProvidersTableProps {
  onAdd: () => void;
  onEdit: (provider: ProviderView) => void;
}

export function ProvidersTable({ onAdd, onEdit }: ProvidersTableProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const providers = useQuery<ProviderView[]>({
    queryKey: ["admin", "providers"],
    queryFn: fetchProviders,
    retry: false,
  });

  const [deleting, setDeleting] = React.useState<ProviderView | null>(null);
  const [deleteBlock, setDeleteBlock] = React.useState<string[] | null>(null);

  const backendPending =
    providers.isError &&
    providers.error instanceof CorlinmanApiError &&
    providers.error.status === 503;

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteProvider(name),
    onSuccess: () => {
      toast.success(t("providers.deleteSuccess"));
      setDeleting(null);
      setDeleteBlock(null);
      qc.invalidateQueries({ queryKey: ["admin", "providers"] });
    },
    onError: (err) => {
      if (err instanceof CorlinmanApiError && err.status === 409) {
        // The server wraps references in the body; extract + surface.
        const parsed = parseReferences(err.message);
        setDeleteBlock(parsed);
        return;
      }
      toast.error(
        t("providers.deleteFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  return (
    <>
      <header className="flex items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("providers.title")}
          </h1>
          <p className="text-sm text-sg-ink-3">
            {t("providers.subtitle")}
          </p>
        </div>
        <Button
          size="sm"
          onClick={onAdd}
          data-testid="providers-add-btn"
        >
          <Plus className="h-3 w-3" />
          {t("providers.add")}
        </Button>
      </header>

      <section className="space-y-3 rounded-lg border border-sg-border bg-sg-card p-4">
        {providers.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : backendPending ? (
          <BackendPendingBanner label={t("providers.backendPending")} />
        ) : providers.isError ? (
          <p className="text-xs text-sg-err">
            {t("providers.loadFailed")}:{" "}
            {(providers.error as Error).message}
          </p>
        ) : (providers.data ?? []).length === 0 ? (
          <EmptyProviders
            title={t("providers.noneTitle")}
            hint={t("providers.noneHint")}
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-sg-border hover:bg-transparent">
                <TableHead className="w-44 pl-3">
                  {t("providers.colName")}
                </TableHead>
                <TableHead className="w-40">
                  {t("providers.colKind")}
                </TableHead>
                <TableHead>{t("providers.colBaseUrl")}</TableHead>
                <TableHead className="w-44">
                  {t("providers.colKey")}
                </TableHead>
                <TableHead className="w-24">
                  {t("providers.colEnabled")}
                </TableHead>
                <TableHead className="w-44"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {providers.data!.map((p) => (
                <TableRow
                  key={p.name}
                  className="border-b border-sg-border"
                  data-testid={`provider-row-${p.name}`}
                >
                  <TableCell className="pl-3 font-mono text-xs">
                    {p.name}
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
                    {p.api_key_source === "env" ? (
                      <span className="font-mono text-sg-ink-3">
                        {t("providers.keyFromEnv", {
                          name: p.api_key_env_name ?? "?",
                        })}
                      </span>
                    ) : p.api_key_source === "value" ? (
                      <span className="text-sg-ink-3">
                        {t("providers.keyLiteral")}
                      </span>
                    ) : (
                      <span className="text-sg-err">
                        {t("providers.keyUnset")}
                      </span>
                    )}
                  </TableCell>
                  <TableCell>
                    {p.enabled ? (
                      <Badge className="border-transparent bg-sg-ok-soft text-sg-ok">
                        {t("common.enabled")}
                      </Badge>
                    ) : (
                      <Badge variant="secondary">
                        {t("common.disabled")}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      <TestConnectionButton name={p.name} />
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("providers.edit")}
                        onClick={() => onEdit(p)}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("providers.remove")}
                        onClick={() => setDeleting(p)}
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

      <Dialog
        open={!!deleting}
        onOpenChange={(o) => {
          if (!o) {
            setDeleting(null);
            setDeleteBlock(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {deleteBlock
                ? t("providers.deleteBlockedTitle", {
                    name: deleting?.name ?? "",
                  })
                : t("providers.deleteConfirmTitle", {
                    name: deleting?.name ?? "",
                  })}
            </DialogTitle>
            <DialogDescription>
              {deleteBlock
                ? t("providers.deleteBlockedBody")
                : t("providers.deleteConfirmBody")}
            </DialogDescription>
          </DialogHeader>
          {deleteBlock ? (
            <ul className="space-y-1 text-xs font-mono">
              {deleteBlock.map((ref) => (
                <li key={ref} className="text-sg-err">
                  • {ref}
                </li>
              ))}
            </ul>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setDeleting(null);
                setDeleteBlock(null);
              }}
            >
              {t("providers.deleteCancel")}
            </Button>
            {!deleteBlock ? (
              <Button
                variant="destructive"
                disabled={deleteMutation.isPending}
                onClick={() =>
                  deleting && deleteMutation.mutate(deleting.name)
                }
                data-testid="providers-confirm-delete-btn"
              >
                {t("providers.deleteConfirm")}
              </Button>
            ) : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
