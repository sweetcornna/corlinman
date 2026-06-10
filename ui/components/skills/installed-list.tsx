"use client";

/**
 * `<InstalledList>` — the live-backend version of the Skills card grid.
 *
 * W2.1 extraction. Replaces the mock-fed grid in `app/(admin)/skills/page.tsx`.
 * Renders one card per `InstalledSkillRow`, parses the `origin` tag into a
 * three-tone badge (`bundled` / `user` / `hub:<slug>@<ver>`), and surfaces
 * pin + delete affordances inline on each card. The parent page owns the
 * data, the filters, and the confirm-delete dialog; this component is
 * presentation + per-row click handlers only.
 *
 * Card layout (mirrors the old mock-fed card so the grid rhythm stays
 * unchanged):
 *
 *   row 1 — emoji-circle glyph · name · origin badge
 *   row 2 — description, line-clamp-2
 *   row 3 — version · state pill · pin toggle · delete button
 *
 * Bundled rows: the delete button stays disabled with a tooltip
 * (`installed.bundledTooltip`). Server also refuses with 409 — the
 * client-side gate just avoids the round-trip.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { Pin, PinOff, Trash2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotion } from "@/components/ui/motion-safe";
import { useMotionVariants } from "@/lib/motion";
import type { InstalledSkillRow } from "@/lib/api";

// ---------- origin badge ---------------------------------------------------

/** Coarse three-bucket badge tone derived from the wire `origin` string. */
export type OriginBadgeKind = "bundled" | "user" | "hub";

export interface OriginBadge {
  kind: OriginBadgeKind;
  /** Pretty label rendered as the badge text. */
  label: string;
  /** Optional version suffix — only set for hub-sourced skills. */
  version: string | null;
}

/**
 * Parse the wire `origin` string into a structured badge descriptor.
 *
 * `bundled`            → `{ kind: "bundled" }`
 * `user`               → `{ kind: "user" }`
 * `hub:<slug>@<ver>`   → `{ kind: "hub",  version: "<ver>" }`
 * anything else        → `{ kind: "user" }`  (defensive fallback)
 *
 * Pure function — exported so the test suite can lock the parsing.
 */
export function parseOrigin(origin: string): OriginBadge {
  if (origin === "bundled") {
    return { kind: "bundled", label: "bundled", version: null };
  }
  if (origin.startsWith("hub:")) {
    // `hub:<slug>@<ver>` — split on `@` so the version becomes the suffix.
    const tail = origin.slice("hub:".length);
    const atIdx = tail.lastIndexOf("@");
    if (atIdx !== -1) {
      return {
        kind: "hub",
        label: "hub",
        version: tail.slice(atIdx + 1) || null,
      };
    }
    return { kind: "hub", label: "hub", version: null };
  }
  return { kind: "user", label: "user", version: null };
}

const ORIGIN_TONE: Record<OriginBadgeKind, string> = {
  // Neutral: bundled rows are immutable, so they read as ink + inset glass
  // rather than a status color.
  bundled:
    "border-sg-ink-3/30 bg-sg-inset-strong text-sg-ink-2",
  user:
    "border-sg-accent/30 bg-sg-accent-soft text-sg-accent",
  // Hub-sourced rows share the success/ok tone.
  hub: "border-sg-ok/30 bg-sg-ok-soft text-sg-ok",
};

const ORIGIN_DOT: Record<OriginBadgeKind, string> = {
  bundled: "bg-sg-ink-3",
  user: "bg-sg-accent",
  hub: "bg-sg-ok",
};

// ---------- filtering ------------------------------------------------------

export type InstalledFilterValue = "all" | "bundled" | "user" | "hub" | "pinned";

/**
 * Narrow a list of installed-skill rows by free-form search + filter chip.
 * Pure helper — exported for testing.
 */
export function filterRows(
  rows: InstalledSkillRow[],
  search: string,
  filter: InstalledFilterValue,
): InstalledSkillRow[] {
  const q = search.trim().toLowerCase();
  return rows.filter((row) => {
    const badge = parseOrigin(row.origin);
    if (filter === "bundled" && badge.kind !== "bundled") return false;
    if (filter === "user" && badge.kind !== "user") return false;
    if (filter === "hub" && badge.kind !== "hub") return false;
    if (filter === "pinned" && !row.pinned) return false;
    if (!q) return true;
    return (
      row.name.toLowerCase().includes(q) ||
      row.description.toLowerCase().includes(q) ||
      row.origin.toLowerCase().includes(q)
    );
  });
}

// ---------- props ----------------------------------------------------------

export interface InstalledListProps {
  rows: InstalledSkillRow[];
  /** Operator clicked the pin / unpin button on one row. */
  onPin: (row: InstalledSkillRow, nextPinned: boolean) => void;
  /** Operator clicked the delete button on one (non-bundled) row. */
  onDelete: (row: InstalledSkillRow) => void;
  /** Operator clicked anywhere on the card body (excluding action buttons). */
  onOpen?: (row: InstalledSkillRow) => void;
  /** Search query — narrows by name/description/origin. */
  search: string;
  /** Active filter chip — narrows by origin / pinned. */
  filter: InstalledFilterValue;
  /** Names whose pin mutation is in-flight; disables the pin button. */
  pinBusy?: ReadonlySet<string>;
  /** Names whose delete mutation is in-flight; disables the delete button. */
  deleteBusy?: ReadonlySet<string>;
}

// ---------- card -----------------------------------------------------------

export function InstalledList({
  rows,
  onPin,
  onDelete,
  onOpen,
  search,
  filter,
  pinBusy,
  deleteBusy,
}: InstalledListProps) {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const filtered = React.useMemo(
    () => filterRows(rows, search, filter),
    [rows, search, filter],
  );

  if (filtered.length === 0) {
    const hasAny = rows.length > 0;
    return (
      <GlassPanel
        variant="subtle"
        className="flex flex-col items-center gap-2 p-8 text-center"
        data-testid="installed-list-empty"
      >
        <div className="text-[14px] font-medium text-sg-ink">
          {hasAny
            ? t("skills.installed.emptyFilteredTitle")
            : t("skills.installed.emptyTitle")}
        </div>
        <p className="text-[13px] text-sg-ink-3">
          {hasAny
            ? t("skills.installed.emptyFilteredHint")
            : t("skills.installed.emptyHint")}
        </p>
      </GlassPanel>
    );
  }

  return (
    <motion.section
      aria-label={t("skills.installed.gridAria")}
      className={cn(
        "grid gap-3",
        "grid-cols-[repeat(auto-fill,minmax(280px,1fr))]",
      )}
      data-testid="installed-list-grid"
      variants={variants.liquidStagger}
      initial="hidden"
      animate="visible"
    >
      {filtered.map((row) => (
        <motion.div key={row.name} variants={variants.liquidRise}>
          <InstalledCard
            row={row}
            onPin={onPin}
            onDelete={onDelete}
            onOpen={onOpen}
            pinBusy={pinBusy?.has(row.name) ?? false}
            deleteBusy={deleteBusy?.has(row.name) ?? false}
          />
        </motion.div>
      ))}
    </motion.section>
  );
}

interface InstalledCardProps {
  row: InstalledSkillRow;
  onPin: (row: InstalledSkillRow, nextPinned: boolean) => void;
  onDelete: (row: InstalledSkillRow) => void;
  onOpen?: (row: InstalledSkillRow) => void;
  pinBusy: boolean;
  deleteBusy: boolean;
}

function InstalledCard({
  row,
  onPin,
  onDelete,
  onOpen,
  pinBusy,
  deleteBusy,
}: InstalledCardProps) {
  const { t } = useTranslation();
  const { reduced } = useMotion();
  const badge = parseOrigin(row.origin);
  const isBundled = badge.kind === "bundled";

  const handleCardClick = onOpen
    ? (e: React.MouseEvent<HTMLDivElement>) => {
        // Action buttons stop propagation themselves, but a defensive
        // check here ensures clicks landing on the wrapper still trigger
        // the drawer.
        const target = e.target as HTMLElement;
        if (target.closest("[data-installed-action]")) return;
        onOpen(row);
      }
    : undefined;

  return (
    <div
      className={cn(
        "group block focus-visible:outline-none",
        !reduced && "lg-gel hover:-translate-y-0.5",
      )}
      data-testid={`installed-card-${row.name}`}
      data-origin={badge.kind}
    >
      <GlassPanel
        variant="soft"
        lively
        role={onOpen ? "button" : undefined}
        tabIndex={onOpen ? 0 : undefined}
        aria-label={
          onOpen
            ? t("skills.installed.cardAria", { name: row.name })
            : undefined
        }
        onClick={handleCardClick}
        onKeyDown={
          onOpen
            ? (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onOpen(row);
                }
              }
            : undefined
        }
        className={cn(
          "flex h-full flex-col gap-3 p-4",
          onOpen && "cursor-pointer",
          "transition-[box-shadow,border-color] duration-200 ease-sg-ease-out",
          "group-hover:shadow-sg-primary",
          "focus-visible:shadow-sg-primary focus-visible:ring-2 focus-visible:ring-sg-accent/50",
        )}
      >
        {/* Row 1 — name + origin badge */}
        <div className="flex items-start gap-2.5">
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[15px] font-medium leading-tight text-sg-ink">
              {row.name}
            </h3>
            <div className="mt-1 flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
              {row.pinned ? (
                <Pin className="h-3 w-3 text-sg-accent" aria-hidden />
              ) : null}
              <span>v{row.version}</span>
              <span aria-hidden>·</span>
              <span>{row.state}</span>
            </div>
          </div>
          <OriginBadgePill badge={badge} />
        </div>

        {/* Row 2 — description */}
        <p className="line-clamp-2 text-[12.5px] leading-[1.5] text-sg-ink-2">
          {row.description || (
            <span className="text-sg-ink-4">
              {t("skills.installed.noDescription")}
            </span>
          )}
        </p>

        {/* Row 3 — actions */}
        <div className="mt-auto flex items-center justify-end gap-1 pt-1">
          <button
            type="button"
            data-installed-action
            data-testid={`installed-pin-${row.name}`}
            aria-label={
              row.pinned
                ? t("skills.installed.unpin", { name: row.name })
                : t("skills.installed.pin", { name: row.name })
            }
            disabled={pinBusy}
            onClick={(e) => {
              e.stopPropagation();
              onPin(row, !row.pinned);
            }}
            className={cn(
              "inline-flex h-7 w-7 items-center justify-center rounded-md",
              "border border-sg-border bg-sg-inset",
              "text-sg-ink-3 transition-colors",
              "hover:bg-sg-inset-hover hover:text-sg-accent",
              "disabled:opacity-50",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40",
            )}
          >
            {row.pinned ? (
              <PinOff className="h-3.5 w-3.5" aria-hidden />
            ) : (
              <Pin className="h-3.5 w-3.5" aria-hidden />
            )}
          </button>
          {isBundled ? (
            <button
              type="button"
              data-installed-action
              data-testid={`installed-delete-disabled-${row.name}`}
              disabled
              title={t("skills.installed.bundledTooltip")}
              aria-label={t("skills.installed.bundledTooltip")}
              className={cn(
                "inline-flex h-7 w-7 items-center justify-center rounded-md",
                "border border-sg-border bg-sg-inset",
                "text-sg-ink-4 opacity-50 cursor-not-allowed",
              )}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden />
            </button>
          ) : (
            <button
              type="button"
              data-installed-action
              data-testid={`installed-delete-${row.name}`}
              disabled={deleteBusy}
              aria-label={t("skills.installed.delete", { name: row.name })}
              onClick={(e) => {
                e.stopPropagation();
                onDelete(row);
              }}
              className={cn(
                "inline-flex h-7 w-7 items-center justify-center rounded-md",
                "border border-sg-border bg-sg-inset",
                "text-sg-ink-3 transition-colors",
                "hover:bg-sg-err-soft hover:text-sg-err",
                "disabled:opacity-50",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-err/40",
              )}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden />
            </button>
          )}
        </div>
      </GlassPanel>
    </div>
  );
}

function OriginBadgePill({ badge }: { badge: OriginBadge }) {
  return (
    <span
      data-testid={`origin-badge-${badge.kind}`}
      data-origin={badge.kind}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-[2px]",
        "font-mono text-[10.5px]",
        ORIGIN_TONE[badge.kind],
      )}
      title={
        badge.version
          ? `${badge.label} · ${badge.version}`
          : badge.label
      }
    >
      <span
        aria-hidden
        className={cn("h-[5px] w-[5px] rounded-full", ORIGIN_DOT[badge.kind])}
      />
      <span>{badge.label}</span>
      {badge.version ? (
        <span className="text-sg-ink-3">@{badge.version}</span>
      ) : null}
    </span>
  );
}

export default InstalledList;
