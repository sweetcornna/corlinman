"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { PowerOff, Users } from "lucide-react";

import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  fetchIdentityList,
  type IdentityListResult,
  type UserSummary,
} from "@/lib/api/identity";
import { IdentityDetailDialog } from "@/components/identity/identity-detail-dialog";

/**
 * `/admin/identity` — Phase 4 W2 B2 (Cross-channel identity).
 *
 * Lists canonical user_ids with their alias counts. Selecting a row
 * opens `<IdentityDetailDialog>` which shows the per-channel aliases
 * for that user and exposes the operator actions (issue verification
 * phrase, manual merge).
 *
 * 503 `identity_disabled` is rendered as a banner — mirrors the
 * sessions-page disabled-state UX the rest of Phase 4 follows.
 */
export default function IdentityPage() {
  const { t } = useTranslation();
  const [active, setActive] = React.useState<UserSummary | null>(null);

  const query = useQuery<IdentityListResult>({
    queryKey: ["admin", "identity"],
    queryFn: () => fetchIdentityList({ limit: 50, offset: 0 }),
  });

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("identity.title", "Identity")}
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-tp-ink-2">
            {t(
              "identity.subtitle",
              "Canonical user IDs across QQ, Telegram, and other channels. Issue verification phrases or merge identities by hand.",
            )}
          </p>
        </div>
      </header>

      {query.isLoading && <ListSkeleton />}

      {query.data?.kind === "disabled" && (
        <div className="flex items-start gap-3 rounded-lg border border-tp-amber/30 bg-tp-amber-soft p-4 text-sm text-tp-amber">
          <PowerOff className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div>
            <p className="font-medium">
              {t(
                "identity.disabled.title",
                "Identity service disabled",
              )}
            </p>
            <p className="mt-1 text-tp-amber/85">
              {t(
                "identity.disabled.body",
                "The gateway returned 503 for `/admin/identity`. The cross-channel identity store hasn't been wired into this deployment yet.",
              )}
            </p>
          </div>
        </div>
      )}

      {query.data?.kind === "ok" && (
        <IdentityTable
          users={query.data.users}
          onSelect={setActive}
          activeUserId={active?.user_id ?? null}
        />
      )}

      {active && (
        <IdentityDetailDialog
          user={active}
          open={Boolean(active)}
          onClose={() => setActive(null)}
          onMutated={() => {
            // Refresh the list after a merge/phrase issue so alias_count stays current.
            void query.refetch();
          }}
        />
      )}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="space-y-2">
      {[0, 1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-12 w-full" />
      ))}
    </div>
  );
}

function IdentityTable({
  users,
  onSelect,
  activeUserId,
}: {
  users: UserSummary[];
  onSelect: (u: UserSummary) => void;
  activeUserId: string | null;
}) {
  const { t } = useTranslation();
  if (users.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-tp-glass-edge bg-tp-glass-inner py-12 text-tp-ink-3">
        <Users className="h-8 w-8 opacity-50" aria-hidden />
        <p className="text-sm">
          {t(
            "identity.empty",
            "No canonical users yet. They mint on first chat per (channel, channel_user_id).",
          )}
        </p>
      </div>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{t("identity.col.user", "User")}</TableHead>
          <TableHead>
            {t("identity.col.display_name", "Display name")}
          </TableHead>
          <TableHead className="text-right">
            {t("identity.col.aliases", "Aliases")}
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {users.map((u) => (
          <TableRow
            key={u.user_id}
            data-testid={`identity-row-${u.user_id}`}
            data-active={activeUserId === u.user_id ? "true" : "false"}
            className={cn(
              "cursor-pointer transition-colors hover:bg-tp-glass-inner/40",
              activeUserId === u.user_id && "bg-tp-glass-inner/60",
            )}
            onClick={() => onSelect(u)}
          >
            <TableCell className="font-mono text-xs">{u.user_id}</TableCell>
            <TableCell className="text-tp-ink-2">
              {u.display_name ?? "—"}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {u.alias_count}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
