"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import { Search } from "lucide-react";

import { cn } from "@/lib/utils";
import { useMotionVariants } from "@/lib/motion";
import { GlassPanel } from "@/components/ui/glass-panel";
import { StatChip } from "@/components/ui/stat-chip";
import {
  FilterChipGroup,
  type FilterChipOption,
} from "@/components/ui/filter-chip-group";
import { type Skill } from "@/lib/mocks/skills";
import { listProfileSkills, type SkillSummary } from "@/lib/api";
import { useActiveProfile } from "@/lib/context/active-profile";
import { SkillCard } from "@/components/skills/skill-card";
import { SkillDrawer } from "@/components/skills/skill-drawer";
import { SkillsHeader } from "@/components/skills/skills-header";

/**
 * Skills admin page — Tidepool cutover.
 *
 * Layout mirrors the Plugins / Approvals rhythm:
 *   ┌─────────── glass-strong hero ──────────────┐
 *   │ lead pill · title · prose · ⌘K CTA         │
 *   └────────────────────────────────────────────┘
 *   [ StatChip × 4 — total · ready · requires · with-tools ]
 *   [ SearchInput ]  [ FilterChipGroup — all|ready|requires|tagged ]
 *   ┌─ card grid — minmax(280px,1fr) ────────────┐
 *   │ <SkillCard> × N (GlassPanel soft)          │
 *   └────────────────────────────────────────────┘
 *   <SkillDrawer> opens on card click (modal, right-anchored)
 *
 * The gateway endpoint (/admin/skills) isn't wired yet — today we serve from
 * a static mock module. The query still uses `retry: false` so the offline
 * branch paints immediately when the real endpoint lands but is missing.
 */

const SPARK_TOTAL =
  "M0 28 L30 24 L60 26 L90 20 L120 22 L150 16 L180 18 L210 12 L240 14 L270 8 L300 10 L300 36 L0 36 Z";
const SPARK_READY =
  "M0 22 L30 22 L60 20 L90 22 L120 18 L150 20 L180 18 L210 20 L240 16 L270 18 L300 16 L300 36 L0 36 Z";
const SPARK_REQUIRES =
  "M0 10 L30 14 L60 16 L90 20 L120 22 L150 24 L180 26 L210 28 L240 30 L270 30 L300 32 L300 36 L0 36 Z";
const SPARK_TOOLS =
  "M0 28 L30 26 L60 24 L90 22 L120 20 L150 16 L180 14 L210 10 L240 8 L270 6 L300 4 L300 36 L0 36 Z";

type FilterValue = "all" | "ready" | "requires" | "tagged";
const EMPTY_SKILLS: Skill[] = [];

/**
 * Map the curator summary shape onto the richer Skill shape this page
 * was originally built around. Fields the summary doesn't carry
 * (`install`, `allowed_tools`, `body_markdown`, …) get safe empty
 * defaults — they show as "ready" with no tools attached, which is
 * truthful for skills the gateway hasn't enriched yet.
 */
function summaryToSkill(s: SkillSummary): Skill {
  return {
    name: s.name,
    description: s.description ?? "",
    emoji: "",
    allowed_tools: [],
    requires: [],
    install: "",
    source_path: "",
    body_markdown: "",
  };
}

async function fetchSkills(slug: string): Promise<Skill[]> {
  const res = await listProfileSkills(slug);
  return res.skills.map(summaryToSkill);
}

/**
 * Clip an install blurb down to a one-line pill label. We take the first
 * non-empty word the reader actually cares about — typically an env var
 * (`SERPER_API_KEY`) or a CLI (`playwright install`). Falls back to "install".
 */
function requiresLabelFor(skill: Skill): string | undefined {
  const install = skill.install.trim();
  if (install.length === 0) return undefined;
  const firstLine = install.split(/\r?\n/)[0]?.trim() ?? "";
  const match = firstLine.match(/`([^`]+)`/);
  if (match && match[1]) return match[1];
  // Otherwise take the first couple of words so the pill stays compact.
  const words = firstLine.split(/\s+/).slice(0, 3).join(" ");
  return words.length > 0 ? words : "install";
}

export default function SkillsPage() {
  const { t } = useTranslation();
  const variants = useMotionVariants();
  const [search, setSearch] = React.useState("");
  const [filter, setFilter] = React.useState<FilterValue>("all");
  const [drawerSkill, setDrawerSkill] = React.useState<Skill | null>(null);

  const { slug } = useActiveProfile();
  const query = useQuery<Skill[]>({
    queryKey: ["admin", "skills", slug],
    queryFn: () => fetchSkills(slug),
    retry: false,
  });

  const skills = query.data ?? EMPTY_SKILLS;
  // Real backend now — treat the query as "offline" when it raises.
  const offline = query.isError;

  const counts = React.useMemo(() => {
    const c = { total: skills.length, ready: 0, requires: 0, withTools: 0 };
    for (const s of skills) {
      const hasInstall = s.install.trim().length > 0;
      if (hasInstall) c.requires += 1;
      else c.ready += 1;
      if (s.allowed_tools.length > 0) c.withTools += 1;
    }
    return c;
  }, [skills]);

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return skills.filter((s) => {
      const hasInstall = s.install.trim().length > 0;
      if (filter === "ready" && hasInstall) return false;
      if (filter === "requires" && !hasInstall) return false;
      if (filter === "tagged" && s.allowed_tools.length === 0) return false;
      if (!q) return true;
      return (
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q) ||
        s.allowed_tools.some((tt) => tt.toLowerCase().includes(q))
      );
    });
  }, [skills, search, filter]);

  const filterOptions: FilterChipOption[] = [
    { value: "all", label: t("skills.tp.filterAll"), count: counts.total },
    {
      value: "ready",
      label: t("skills.tp.filterReady"),
      count: counts.ready,
      tone: "ok",
    },
    {
      value: "requires",
      label: t("skills.tp.filterRequires"),
      count: counts.requires,
      tone: "warn",
    },
    {
      value: "tagged",
      label: t("skills.tp.filterTagged"),
      count: counts.withTools,
      tone: "info",
    },
  ];

  const handleOpen = React.useCallback((skill: Skill) => {
    setDrawerSkill(skill);
  }, []);

  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={variants.fadeUp}
      initial="hidden"
      animate="visible"
    >
      <SkillsHeader counts={offline ? undefined : counts} offline={offline} />

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
          label={t("skills.tp.statTotal")}
          value={offline ? "—" : counts.total}
          foot={offline ? t("skills.tp.offlineTitle") : t("skills.tp.statFootTotal")}
          sparkPath={SPARK_TOTAL}
          sparkTone="amber"
        />
        <StatChip
          label={t("skills.tp.statReady")}
          value={offline ? "—" : counts.ready}
          delta={
            !offline && counts.total > 0
              ? {
                  label: `${counts.ready} / ${counts.total}`,
                  tone: counts.requires === 0 ? "up" : "flat",
                }
              : undefined
          }
          foot={offline ? t("skills.tp.offlineTitle") : t("skills.tp.statFootReady")}
          sparkPath={SPARK_READY}
          sparkTone="ember"
        />
        <StatChip
          label={t("skills.tp.statRequires")}
          value={offline ? "—" : counts.requires}
          foot={offline ? t("skills.tp.offlineTitle") : t("skills.tp.statFootRequires")}
          sparkPath={SPARK_REQUIRES}
          sparkTone="ember"
        />
        <StatChip
          label={t("skills.tp.statWithTools")}
          value={offline ? "—" : counts.withTools}
          foot={offline ? t("skills.tp.offlineTitle") : t("skills.tp.statFootWithTools")}
          sparkPath={SPARK_TOOLS}
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
            placeholder={t("skills.tp.searchPlaceholder")}
            aria-label={t("skills.tp.searchPlaceholder")}
            className="h-9 w-full rounded-lg border border-tp-glass-edge bg-tp-glass-inner pl-8 pr-3 text-[13px] text-tp-ink placeholder:text-tp-ink-4 transition-colors hover:bg-tp-glass-inner-hover focus:outline-none focus:ring-2 focus:ring-tp-amber/40"
          />
        </label>
        <FilterChipGroup
          options={filterOptions}
          value={filter}
          onChange={(next) => setFilter(next as FilterValue)}
          label={t("skills.tp.filterLabel")}
        />
      </section>

      {/* Card grid / offline / empty */}
      {query.isPending ? (
        <CardGridSkeleton />
      ) : offline ? (
        <OfflineBlock message={(query.error as Error | undefined)?.message} />
      ) : filtered.length === 0 ? (
        <EmptyBlock hasAnySkills={skills.length > 0} />
      ) : (
        <section
          aria-label={t("nav.skills")}
          className={cn(
            "grid gap-3",
            "grid-cols-[repeat(auto-fill,minmax(280px,1fr))]",
          )}
          data-testid="skills-grid"
        >
          {filtered.map((skill) => (
            <SkillCard
              key={skill.name}
              skill={skill}
              onOpen={handleOpen}
              requiresLabel={requiresLabelFor(skill)}
            />
          ))}
        </section>
      )}

      <SkillDrawer
        skill={drawerSkill}
        open={drawerSkill !== null}
        onOpenChange={(next) => {
          if (!next) setDrawerSkill(null);
        }}
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
        {t("skills.tp.offlineTitle")}
      </div>
      <p className="max-w-prose text-[13px] text-tp-ink-2">
        {t("skills.tp.offlineHint")}
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

function EmptyBlock({ hasAnySkills }: { hasAnySkills: boolean }) {
  const { t } = useTranslation();
  return (
    <GlassPanel variant="subtle" className="flex flex-col items-center gap-2 p-8 text-center">
      <div className="text-[14px] font-medium text-tp-ink">
        {hasAnySkills
          ? t("skills.tp.emptyTitle")
          : t("skills.tp.emptyInstalledTitle")}
      </div>
      <p className="text-[13px] text-tp-ink-3">
        {hasAnySkills
          ? t("skills.tp.emptyHint")
          : t("skills.tp.emptyInstalledHint")}
      </p>
    </GlassPanel>
  );
}
