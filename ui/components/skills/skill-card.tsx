"use client";

import * as React from "react";
import {
  Database,
  Globe,
  Plug,
  Search,
  Wrench,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { GlassPanel } from "@/components/ui/glass-panel";
import { useMotion } from "@/components/ui/motion-safe";
import type { Skill } from "@/lib/mocks/skills";

/**
 * `<SkillCard>` — Tidepool skill grid cell.
 *
 * A `GlassPanel variant="soft"` styled as a button. Click anywhere (or Enter /
 * Space) to open the detail drawer. Under pointer hover, the card lifts 2px
 * and escalates to `shadow-sg-primary`. Under `prefers-reduced-motion: reduce`
 * the translate is suppressed.
 *
 * Layout (three rows + optional pill):
 *   row 1 — emoji (amber-soft circle) + skill name (h3, medium)
 *   row 2 — description, `line-clamp-2`
 *   row 3 — up to 3 tool chips (mono) + `+N more` overflow
 *   footer — `requires X` pill in amber-soft if the skill declares `install`
 *
 * The card preserves the public API of the pre-cutover version:
 *   - `categorize()` and `CATEGORY_META` still exported for any callers that
 *     classify skills outside the card (legacy filters, telemetry, etc.).
 *   - `role="button"`, `aria-label="Open {name} skill details"`, `data-testid`,
 *     and the `data-category` attribute are stable so the existing test suite
 *     continues to pass.
 */

// ---------- categorization (unchanged public API) --------------------------

export type SkillCategory = "dev-tools" | "integrations" | "search" | "memory" | "other";

interface CategoryMeta {
  id: SkillCategory;
  label: string;
  /** Lucide icon used as fallback glyph when the skill carries no emoji. */
  icon: LucideIcon;
}

export const CATEGORY_META: Record<SkillCategory, CategoryMeta> = {
  "dev-tools": { id: "dev-tools", label: "Dev tools", icon: Wrench },
  integrations: { id: "integrations", label: "Integrations", icon: Plug },
  search: { id: "search", label: "Search", icon: Search },
  memory: { id: "memory", label: "Memory", icon: Database },
  other: { id: "other", label: "Other", icon: Globe },
};

const DEV_TOOL_PREFIXES = ["file_ops", "canvas", "coding_agent", "browser"];
const INTEGRATION_PREFIXES = [
  "discord",
  "gh_issues",
  "bear_notes",
  "1password",
  "gemini",
  "clawhub",
];
const SEARCH_PREFIXES = ["web_search"];
const MEMORY_PREFIXES = ["memory"];

/**
 * Derive a category from a skill's name + tool list. Pure function; no React
 * dependencies so it can be unit-tested in isolation.
 */
export function categorize(skillName: string, tools: string[]): SkillCategory {
  const name = skillName.toLowerCase();
  if (MEMORY_PREFIXES.some((p) => name.startsWith(p))) return "memory";
  if (SEARCH_PREFIXES.some((p) => name.startsWith(p))) return "search";
  if (DEV_TOOL_PREFIXES.some((p) => name.startsWith(p))) return "dev-tools";
  if (INTEGRATION_PREFIXES.some((p) => name.startsWith(p))) return "integrations";

  // Fallback: look at tool prefixes if the name wasn't decisive.
  const prefixes = tools.map((t) => t.split(".")[0]?.toLowerCase() ?? "");
  if (prefixes.some((p) => MEMORY_PREFIXES.includes(p))) return "memory";
  if (prefixes.some((p) => SEARCH_PREFIXES.includes(p))) return "search";
  if (prefixes.some((p) => DEV_TOOL_PREFIXES.includes(p))) return "dev-tools";
  if (prefixes.some((p) => INTEGRATION_PREFIXES.includes(p))) return "integrations";

  return "other";
}

// ---------- card ------------------------------------------------------------

export interface SkillCardProps {
  skill: Skill;
  /** Extra classes — typically not needed since the grid owns layout. */
  className?: string;
  onOpen: (skill: Skill) => void;
  /**
   * Truncated "requires" target (e.g. the first package / `install` line) to
   * render as a pill. The parent page supplies this so the card stays
   * presentation-only.
   */
  requiresLabel?: string;
}

export function SkillCard({
  skill,
  className,
  onOpen,
  requiresLabel,
}: SkillCardProps) {
  const { reduced } = useMotion();
  const category = React.useMemo(
    () => categorize(skill.name, skill.allowed_tools),
    [skill.name, skill.allowed_tools],
  );
  const meta = CATEGORY_META[category];
  const CategoryIcon = meta.icon;

  const visibleTools = skill.allowed_tools.slice(0, 3);
  const overflowCount = skill.allowed_tools.length - visibleTools.length;

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onOpen(skill);
    }
  };

  return (
    <div
      className={cn(
        "group block focus-visible:outline-none",
        !reduced &&
          "transition-transform duration-200 ease-tp-ease-out hover:-translate-y-0.5",
        className,
      )}
      data-testid={`skill-card-${skill.name}`}
      data-category={category}
    >
      <GlassPanel
        variant="soft"
        role="button"
        tabIndex={0}
        aria-label={`Open ${skill.name} skill details`}
        onClick={() => onOpen(skill)}
        onKeyDown={handleKeyDown}
        className={cn(
          "flex h-full cursor-pointer flex-col gap-3 p-4",
          "transition-[box-shadow,border-color] duration-200 ease-tp-ease-out",
          "group-hover:shadow-sg-primary",
          "focus-visible:shadow-sg-primary focus-visible:ring-2 focus-visible:ring-sg-accent/50",
        )}
      >
        {/* Row 1 — emoji/glyph badge + name + version-ish meta line */}
        <div className="flex items-start gap-2.5">
          <div
            aria-hidden
            className={cn(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-full",
              "border border-sg-accent/25 bg-sg-accent-soft text-[17px] leading-none",
            )}
          >
            {skill.emoji ? (
              // Subtle tint so the legacy emoji reads as accent, not as a
              // dominant visual element.
              <span className="opacity-85">{skill.emoji}</span>
            ) : (
              <CategoryIcon className="h-4 w-4 text-sg-accent" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[15px] font-medium leading-tight text-sg-ink">
              {skill.name}
            </h3>
            <div className="mt-1 flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.08em] text-sg-ink-4">
              <CategoryIcon className="h-3 w-3" aria-hidden />
              <span>{meta.label}</span>
            </div>
          </div>
        </div>

        {/* Row 2 — description, clamped */}
        <p className="line-clamp-2 text-[12.5px] leading-[1.5] text-sg-ink-2">
          {skill.description}
        </p>

        {/* Row 3 — tool chips */}
        <div className="mt-auto flex flex-wrap items-center gap-1.5 pt-1">
          {visibleTools.map((tool) => (
            <span
              key={tool}
              className="inline-flex items-center rounded-md border border-sg-border bg-sg-inset px-1.5 py-0.5 font-mono text-[10.5px] text-sg-ink-3"
            >
              {tool}
            </span>
          ))}
          {overflowCount > 0 ? (
            <span
              className="inline-flex items-center rounded-md border border-sg-border bg-sg-inset px-1.5 py-0.5 font-mono text-[10.5px] text-sg-ink-4"
              aria-label={`${overflowCount} more tools`}
            >
              +{overflowCount} more
            </span>
          ) : null}
          {visibleTools.length === 0 && overflowCount === 0 ? (
            <span className="font-mono text-[10.5px] text-sg-ink-4">
              no allowed-tools
            </span>
          ) : null}
        </div>

        {/* Optional "requires install" pill */}
        {requiresLabel ? (
          <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
            <span
              className="inline-flex items-center gap-1.5 rounded-full border border-sg-accent/30 bg-sg-accent-soft px-2 py-[2px] font-mono text-[10.5px] text-sg-accent"
              title={requiresLabel}
            >
              <span aria-hidden className="h-[5px] w-[5px] rounded-full bg-sg-accent" />
              <span className="max-w-[220px] truncate">requires {requiresLabel}</span>
            </span>
          </div>
        ) : null}
      </GlassPanel>
    </div>
  );
}

export default SkillCard;
