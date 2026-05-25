"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { Search } from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { useMotionVariants } from "@/lib/motion";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  CorlinmanApiError,
  deleteInstalledSkill,
  listInstalledSkills,
  pinInstalledSkill,
  type InstalledSkillRow,
  type InstalledSkillsResponse,
} from "@/lib/api";
import { useActiveProfile } from "@/lib/context/active-profile";
import { SkillsHeader } from "@/components/skills/skills-header";
import {
  InstalledList,
  parseOrigin,
  type InstalledFilterValue,
} from "@/components/skills/installed-list";
import { HubTab } from "@/components/skills/hub-tab";

/**
 * Skills admin page — Tidepool cutover + W2.1 backend wire.
 *
 * Layout mirrors the Plugins / Approvals rhythm:
 *   ┌─────────── glass-strong hero ──────────────┐
 *   │ lead pill · title · prose · ⌘K CTA         │
 *   └────────────────────────────────────────────┘
 *   [ StatChip × 4 — total · bundled · user · hub ]
 *   [ SearchInput ]  [ FilterChipGroup — all|bundled|user|hub|pinned ]
 *   ┌─ card grid — minmax(280px,1fr) ────────────┐
 *   │ <InstalledList>                            │
 *   └────────────────────────────────────────────┘
 *
 * W2.1 — the Installed tab is now wired to the real gateway endpoint
 * `/admin/skills?profile=…`. Each row carries an `origin` tag we render
 * as a three-tone badge (bundled / user / hub). The Browse Hub tab is
 * owned by W2.2 (separate component, lands concurrently).
 */

const SPARK_TOTAL =
  "M0 28 L30 24 L60 26 L90 20 L120 22 L150 16 L180 18 L210 12 L240 14 L270 8 L300 10 L300 36 L0 36 Z";
const SPARK_BUNDLED =
  "M0 22 L30 22 L60 20 L90 22 L120 18 L150 20 L180 18 L210 20 L240 16 L270 18 L300 16 L300 36 L0 36 Z";
const SPARK_USER =
  "M0 10 L30 14 L60 16 L90 20 L120 22 L150 24 L180 26 L210 28 L240 30 L270 30 L300 32 L300 36 L0 36 Z";
const SPARK_HUB =
  "M0 28 L30 26 L60 24 L90 22 L120 20 L150 16 L180 14 L210 10 L240 8 L270 6 L300 4 L300 36 L0 36 Z";

const EMPTY_ROWS: InstalledSkillRow[] = [];

export default function SkillsPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const queryClient = useQueryClient();
  const [tab, setTab] = React.useState<"installed" | "hub">("installed");
  const [search, setSearch] = React.useState("");
  const [filter, setFilter] = React.useState<InstalledFilterValue>("all");
  const [pendingDelete, setPendingDelete] =
    React.useState<InstalledSkillRow | null>(null);
  const [deleteConfirmName, setDeleteConfirmName] = React.useState("");

  const { slug } = useActiveProfile();
  const query = useQuery<InstalledSkillsResponse>({
    queryKey: ["admin", "skills", slug],
    queryFn: () => listInstalledSkills(slug),
    retry: false,
  });

  const rows = query.data?.rows ?? EMPTY_ROWS;
  const offline = query.isError;

  const counts = React.useMemo(() => {
    const c = { total: rows.length, bundled: 0, user: 0, hub: 0, pinned: 0 };
    for (const row of rows) {
      const kind = parseOrigin(row.origin).kind;
      if (kind === "bundled") c.bundled += 1;
      else if (kind === "hub") c.hub += 1;
      else c.user += 1;
      if (row.pinned) c.pinned += 1;
    }
    return c;
  }, [rows]);

  // Map our counts onto the header's expected shape (which still speaks
  // the pre-cutover `{total, ready, requires, withTools}` vocabulary).
  // We re-cast so the existing prose strings keep reading naturally:
  //   - total          → total installed
  //   - ready          → user + hub  (non-bundled, mutable rows)
  //   - requires       → bundled     (visible but immutable)
  //   - withTools      → pinned
  const headerCounts = React.useMemo(
    () => ({
      total: counts.total,
      ready: counts.user + counts.hub,
      requires: counts.bundled,
      withTools: counts.pinned,
    }),
    [counts],
  );

  // ---- pin mutation -------------------------------------------------------
  const pinMutation = useMutation({
    mutationFn: ({ name, pinned }: { name: string; pinned: boolean }) =>
      pinInstalledSkill(name, pinned, slug),
    onSuccess: (updated) => {
      // Patch the cached list rather than refetching so the toggle is
      // instant. Falls back silently when the cache is missing — the
      // next query refetch will pick the row up regardless.
      queryClient.setQueryData<InstalledSkillsResponse | undefined>(
        ["admin", "skills", slug],
        (prev) => {
          if (!prev) return prev;
          return {
            ...prev,
            rows: prev.rows.map((row) =>
              row.name === updated.name ? updated : row,
            ),
          };
        },
      );
    },
    onError: (err, vars) => {
      const msg =
        err instanceof CorlinmanApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : String(err);
      toast.error(
        t("skills.installed.pinFailed", { name: vars.name, message: msg }),
      );
    },
  });

  // ---- delete mutation ----------------------------------------------------
  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteInstalledSkill(name, slug),
    onSuccess: (_void, name) => {
      toast.success(t("skills.installed.deleteSuccess", { name }));
      setPendingDelete(null);
      setDeleteConfirmName("");
      void queryClient.invalidateQueries({
        queryKey: ["admin", "skills", slug],
      });
    },
    onError: (err, name) => {
      // The server emits 409 `bundled_protected` for bundled rows;
      // surface a more specific message in that case so the operator
      // understands the gate.
      let msg: string;
      if (err instanceof CorlinmanApiError) {
        if (err.status === 409) {
          msg = t("skills.installed.bundledTooltip");
        } else {
          msg = err.message;
        }
      } else {
        msg = err instanceof Error ? err.message : String(err);
      }
      toast.error(
        t("skills.installed.deleteFailed", { name, message: msg }),
      );
    },
  });

  // Track in-flight names so the InstalledList can disable just the
  // relevant buttons. `useMutation` exposes the pending variables, so
  // we wrap them in a Set keyed by name.
  const pinBusy = React.useMemo<Set<string>>(() => {
    const s = new Set<string>();
    if (pinMutation.isPending && pinMutation.variables?.name) {
      s.add(pinMutation.variables.name);
    }
    return s;
  }, [pinMutation.isPending, pinMutation.variables]);

  const deleteBusy = React.useMemo<Set<string>>(() => {
    const s = new Set<string>();
    if (deleteMutation.isPending && deleteMutation.variables) {
      s.add(deleteMutation.variables);
    }
    return s;
  }, [deleteMutation.isPending, deleteMutation.variables]);

  // ---- callbacks ----------------------------------------------------------

  const handlePin = React.useCallback(
    (row: InstalledSkillRow, nextPinned: boolean) => {
      pinMutation.mutate({ name: row.name, pinned: nextPinned });
    },
    [pinMutation],
  );

  const handleDelete = React.useCallback((row: InstalledSkillRow) => {
    // Defensive client-side gate — server also refuses with 409.
    if (parseOrigin(row.origin).kind === "bundled") return;
    setPendingDelete(row);
    setDeleteConfirmName("");
  }, []);

  // ---- filter chips -------------------------------------------------------

  const filterOptions: FilterChipOption[] = [
    {
      value: "all",
      label: t("skills.installed.filterAll"),
      count: counts.total,
    },
    {
      value: "bundled",
      label: t("skills.installed.filterBundled"),
      count: counts.bundled,
      tone: "info",
    },
    {
      value: "user",
      label: t("skills.installed.filterUser"),
      count: counts.user,
      tone: "warn",
    },
    {
      value: "hub",
      label: t("skills.installed.filterHub"),
      count: counts.hub,
      tone: "ok",
    },
    {
      value: "pinned",
      label: t("skills.installed.filterPinned"),
      count: counts.pinned,
      tone: "neutral",
    },
  ];

  // ---- render -------------------------------------------------------------

  const isPending = query.isPending;
  const confirmName = pendingDelete?.name ?? "";
  const confirmEnabled =
    deleteConfirmName.trim() === confirmName && confirmName.length > 0;

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <SkillsHeader counts={offline ? undefined : headerCounts} offline={offline} />

      {/* Installed | Browse hub tab switcher */}
      <nav
        role="tablist"
        aria-label={t("skills.title")}
        className="flex items-center gap-1 border-b border-tp-glass-edge"
      >
        {(["installed", "hub"] as const).map((id) => {
          const active = tab === id;
          return (
            <button
              key={id}
              role="tab"
              type="button"
              aria-selected={active}
              data-testid={`skills-tab-${id}`}
              onClick={() => setTab(id)}
              className={
                "px-3 py-1.5 text-[12.5px] font-medium transition-colors " +
                (active
                  ? "border-b-2 border-tp-amber text-tp-ink"
                  : "border-b-2 border-transparent text-tp-ink-3 hover:text-tp-ink-2")
              }
            >
              {t(id === "installed" ? "skills.installed.tab" : "skills.hub.tab")}
            </button>
          );
        })}
      </nav>

      {tab === "hub" ? (
        <HubTab />
      ) : (
        <>
      {/* Stat chips row */}
      <motion.section
        className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4"
        variants={variants.stagger}
        initial="hidden"
        animate="visible"
      >
        <StatChip
          variant="primary"
          live={!offline}
          label={t("skills.installed.statTotal")}
          value={offline ? "—" : counts.total}
          foot={
            offline
              ? t("skills.installed.offlineTitle")
              : t("skills.installed.statFootTotal")
          }
          sparkPath={SPARK_TOTAL}
          sparkTone="amber"
        />
        <StatChip
          label={t("skills.installed.statBundled")}
          value={offline ? "—" : counts.bundled}
          foot={
            offline
              ? t("skills.installed.offlineTitle")
              : t("skills.installed.statFootBundled")
          }
          sparkPath={SPARK_BUNDLED}
          sparkTone="ember"
        />
        <StatChip
          label={t("skills.installed.statUser")}
          value={offline ? "—" : counts.user}
          foot={
            offline
              ? t("skills.installed.offlineTitle")
              : t("skills.installed.statFootUser")
          }
          sparkPath={SPARK_USER}
          sparkTone="ember"
        />
        <StatChip
          label={t("skills.installed.statHub")}
          value={offline ? "—" : counts.hub}
          foot={
            offline
              ? t("skills.installed.offlineTitle")
              : t("skills.installed.statFootHub")
          }
          sparkPath={SPARK_HUB}
          sparkTone="peach"
        />
      </motion.section>

      {/* Search + filter chips */}
      <section className="flex flex-wrap items-center justify-between gap-3">
        <label className="relative flex min-w-[220px] flex-1 items-center sm:max-w-[360px]">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tp-ink-4"
            aria-hidden
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("skills.installed.searchPlaceholder")}
            aria-label={t("skills.installed.searchPlaceholder")}
            className="h-9 w-full rounded-lg border border-tp-glass-edge bg-tp-glass-inner pl-8 pr-3 text-[13px] text-tp-ink placeholder:text-tp-ink-4 transition-colors hover:bg-tp-glass-inner-hover focus:outline-none focus:ring-2 focus:ring-tp-amber/40"
          />
        </label>
        <FilterChipGroup
          options={filterOptions}
          value={filter}
          onChange={(next) => setFilter(next as InstalledFilterValue)}
          label={t("skills.installed.filterLabel")}
        />
      </section>

      {/* Card grid / offline / loading */}
      {isPending ? (
        <CardGridSkeleton />
      ) : offline ? (
        <OfflineBlock message={(query.error as Error | undefined)?.message} />
      ) : (
        <InstalledList
          rows={rows}
          onPin={handlePin}
          onDelete={handleDelete}
          search={search}
          filter={filter}
          pinBusy={pinBusy}
          deleteBusy={deleteBusy}
        />
      )}
        </>
      )}

      {/* Re-type delete confirmation — gates non-bundled deletes only.
          Bundled rows are blocked client-side, so this dialog is only
          ever shown for `user` / `hub:*` origins. */}
      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(o) => {
          if (!o) {
            setPendingDelete(null);
            setDeleteConfirmName("");
          }
        }}
        title={t("skills.installed.deleteConfirmTitle", {
          name: confirmName,
        })}
        description={
          <span className="flex flex-col gap-3">
            <span>
              {t("skills.installed.deleteConfirmBody", { name: confirmName })}
            </span>
            <input
              type="text"
              value={deleteConfirmName}
              onChange={(e) => setDeleteConfirmName(e.target.value)}
              placeholder={confirmName}
              aria-label={t("skills.installed.deleteConfirmRetype", {
                name: confirmName,
              })}
              data-testid="installed-delete-confirm-input"
              className={cn(
                "h-9 rounded-md border border-tp-glass-edge bg-tp-glass-inner",
                "px-2 font-mono text-[13px] text-tp-ink placeholder:text-tp-ink-4",
                "focus:outline-none focus:ring-2 focus:ring-tp-err/40",
              )}
            />
          </span>
        }
        confirmLabel={t("skills.installed.deleteConfirmAction")}
        cancelLabel={t("common.cancel")}
        destructive
        busy={deleteMutation.isPending}
        onConfirm={async () => {
          if (!pendingDelete || !confirmEnabled) return;
          await deleteMutation.mutateAsync(pendingDelete.name);
        }}
        testId="installed-delete-confirm"
      />
    </motion.div>
  );
}

// ─── Sub-components ───────────────────────────────────────────────────────

function CardGridSkeleton() {
  return (
    <section
      aria-hidden
      className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]"
    >
      {Array.from({ length: 6 }).map((_, i) => (
        <GlassPanel
          key={i}
          variant="soft"
          className="flex h-[148px] flex-col gap-3 p-4"
        >
          <div className="flex items-center gap-2.5">
            <div className="h-9 w-9 rounded-full bg-tp-glass-inner-strong" />
            <div className="flex-1 space-y-1.5">
              <div className="h-3.5 w-2/3 rounded bg-tp-glass-inner-strong" />
              <div className="h-2.5 w-1/3 rounded bg-tp-glass-inner" />
            </div>
          </div>
          <div className="h-3 w-5/6 rounded bg-tp-glass-inner" />
          <div className="mt-auto flex gap-1.5">
            <div className="h-4 w-16 rounded bg-tp-glass-inner" />
            <div className="h-4 w-20 rounded bg-tp-glass-inner" />
            <div className="h-4 w-12 rounded bg-tp-glass-inner" />
          </div>
        </GlassPanel>
      ))}
    </section>
  );
}

function OfflineBlock({ message }: { message?: string }) {
  const { t } = useTranslation();
  // Truncate diagnostic messages — a raw fetch error can be the gateway's
  // full 404 HTML body, which blows up the layout. Cap to a single line.
  const firstLine = message?.split(/\r?\n/).find((ln) => ln.trim().length > 0)?.trim();
  const short =
    firstLine && firstLine.length > 180
      ? firstLine.slice(0, 180) + "…"
      : firstLine;
  return (
    <GlassPanel variant="soft" className="flex flex-col items-center gap-2 p-8 text-center">
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-tp-err">
        {t("skills.installed.offlineTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-2">
        {t("skills.installed.offlineHint")}
      </p>
      {short ? (
        <p
          className="max-w-full truncate font-mono text-[11px] text-tp-ink-4"
          title={message}
        >
          {short}
        </p>
      ) : null}
    </GlassPanel>
  );
}
