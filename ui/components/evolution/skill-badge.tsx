"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import type {
  CuratorSkillOrigin,
  CuratorSkillState,
} from "@/lib/api";

/**
 * Tiny presentational badge used in the W4.6 skill list.
 *
 * Two flavours via the `kind` prop:
 *
 *   - `state`  → ok / warn / muted for active / stale / archived
 *   - `origin` → accent / accent-2 / accent-3 for bundled / user / agent
 *
 * The colour palette maps onto the Spatial Glass status + accent tokens
 * so the whole page reads consistently (and flips light/dark on its own
 * — no `dark:` variants needed). The label is read from the i18n bundle
 * so the surface stays bilingual without per-call literal strings.
 */
export type SkillBadgeKind =
  | { kind: "state"; value: CuratorSkillState }
  | { kind: "origin"; value: CuratorSkillOrigin };

const STATE_CLASSES: Record<CuratorSkillState, string> = {
  active: "border-sg-ok/30 bg-sg-ok-soft text-sg-ok",
  stale: "border-sg-warn/30 bg-sg-warn-soft text-sg-warn",
  archived: "border-sg-border bg-sg-inset text-sg-ink-3",
};

const ORIGIN_CLASSES: Record<CuratorSkillOrigin, string> = {
  bundled: "border-sg-accent/30 bg-sg-accent-soft text-sg-accent",
  "user-requested": "border-sg-accent-2/30 bg-sg-accent-2-soft text-sg-accent-2",
  "agent-created": "border-sg-accent-3/30 bg-sg-accent-3/12 text-sg-accent-3",
};

const ORIGIN_LABEL_KEYS: Record<CuratorSkillOrigin, string> = {
  bundled: "evolution.skill.origin.bundled",
  "user-requested": "evolution.skill.origin.userRequested",
  "agent-created": "evolution.skill.origin.agentCreated",
};

export function SkillBadge(
  props: SkillBadgeKind & { className?: string },
) {
  const { t } = useTranslation();
  const base =
    "inline-flex items-center rounded-sg-sm border px-2 py-0.5 text-[11px] font-medium";

  if (props.kind === "state") {
    return (
      <span
        data-testid={`skill-state-${props.value}`}
        className={cn(base, STATE_CLASSES[props.value], props.className)}
      >
        {t(`evolution.skill.state.${props.value}`)}
      </span>
    );
  }

  return (
    <span
      data-testid={`skill-origin-${props.value}`}
      className={cn(base, ORIGIN_CLASSES[props.value], props.className)}
    >
      {t(ORIGIN_LABEL_KEYS[props.value])}
    </span>
  );
}
