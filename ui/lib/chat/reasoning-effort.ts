import type { ReasoningEffort } from "@/lib/api/chat";

/** Canonical tiers in clamp order — mirrors the backend registry
 *  (corlinman_providers.reasoning_tiers), which is the source of truth:
 *  the models API sends each alias's real ladder as `reasoning_tiers`. */
export const CANONICAL_REASONING_TIERS: readonly ReasoningEffort[] = [
  "none",
  "minimal",
  "low",
  "on",
  "medium",
  "high",
  "xhigh",
  "max",
];

/** `on` ranks beside `medium` so graded↔toggle requests land sensibly. */
const TIER_RANK: Record<ReasoningEffort, number> = {
  none: 0,
  minimal: 1,
  low: 2,
  on: 3,
  medium: 3,
  high: 4,
  xhigh: 5,
  max: 6,
};

export function isReasoningTier(value: unknown): value is ReasoningEffort {
  return (
    typeof value === "string" &&
    (CANONICAL_REASONING_TIERS as readonly string[]).includes(value)
  );
}

/** Snap `requested` onto a model's supported ladder (nearest by rank,
 *  ties resolve downward). `undefined` when the ladder is empty. */
export function clampReasoningTier(
  tiers: readonly string[],
  requested: string,
): ReasoningEffort | undefined {
  if (!isReasoningTier(requested)) return undefined;
  const ladder = tiers.filter(isReasoningTier);
  if (ladder.length === 0) return undefined;
  if (ladder.includes(requested)) return requested;
  const want = TIER_RANK[requested];
  let best: ReasoningEffort = ladder[0]!;
  let bestScore = [Infinity, Infinity] as [number, number];
  for (const tier of ladder) {
    const score: [number, number] = [
      Math.abs(TIER_RANK[tier] - want),
      TIER_RANK[tier],
    ];
    if (
      score[0] < bestScore[0] ||
      (score[0] === bestScore[0] && score[1] < bestScore[1])
    ) {
      best = tier;
      bestScore = score;
    }
  }
  return best;
}

export function modelSupportsReasoningEffort(model: string): boolean {
  const id = model.trim().toLowerCase();
  if (!id) return false;
  return (
    id.includes("codex") ||
    id === "o1" ||
    id === "o3" ||
    id === "o4" ||
    /^o[134](?:-|$)/.test(id) ||
    /(?:^|[/_-])gpt-5(?:[.-]|$)/.test(id)
  );
}
