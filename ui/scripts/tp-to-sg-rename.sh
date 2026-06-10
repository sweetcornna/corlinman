#!/bin/zsh
# Phase 3B one-shot: burn down the tp-* alias layer — rename every legacy
# Tidepool class/var consumer to the canonical sg-* name per the alias table
# in globals.css. Longest-match-first ordering matters. macOS sed (-i '').
# Scope: source + tests only; globals.css/tailwind.config.ts are edited by
# hand afterwards (alias block + tp entries removal).
set -euo pipefail
cd "$(dirname "$0")/.."

FILES=$(grep -rlE '\btp-|"tp-|tp_bg_root' app components lib tests \
  --include='*.tsx' --include='*.ts' | grep -v 'globals.css' || true)

if [ -z "$FILES" ]; then
  echo "no tp-* consumers found"
  exit 0
fi

echo "$FILES" | wc -l | xargs echo "rewriting files:"

run_sed() {
  echo "$FILES" | while IFS= read -r f; do
    sed -i '' -E "$1" "$f"
  done
}

# ── glass tiers (longest first) ─────────────────────────────────────
run_sed 's/tp-glass-inner-strong/sg-inset-strong/g'
run_sed 's/tp-glass-inner-hover/sg-inset-hover/g'
run_sed 's/tp-glass-inner/sg-inset/g'
run_sed 's/tp-glass-edge-strong/sg-border-strong/g'
run_sed 's/tp-glass-edge/sg-border/g'
run_sed 's/tp-glass-hl/sg-highlight/g'
run_sed 's/tp-glass-2/sg-card-strong/g'
run_sed 's/tp-glass-3/sg-card-weak/g'
run_sed 's/tp-glass/sg-card/g'

# ── accents & status ────────────────────────────────────────────────
run_sed 's/tp-amber-soft/sg-accent-soft/g'
run_sed 's/tp-amber-glow/sg-accent-glow/g'
run_sed 's/tp-amber/sg-accent/g'
run_sed 's/tp-ember/sg-accent-2/g'
run_sed 's/tp-peach/sg-accent-3/g'
run_sed 's/tp-ok-soft/sg-ok-soft/g'
run_sed 's/tp-ok/sg-ok/g'
run_sed 's/tp-warn-soft/sg-warn-soft/g'
run_sed 's/tp-warn/sg-warn/g'
run_sed 's/tp-err-soft/sg-err-soft/g'
run_sed 's/tp-err/sg-err/g'

# ── ink scale ───────────────────────────────────────────────────────
run_sed 's/tp-ink-2/sg-ink-2/g'
run_sed 's/tp-ink-3/sg-ink-3/g'
run_sed 's/tp-ink-4/sg-ink-4/g'
run_sed 's/tp-ink-5/sg-ink-5/g'
run_sed 's/tp-ink/sg-ink/g'

# ── shadows / gradients / misc tokens ───────────────────────────────
run_sed 's/shadow-tp-panel/shadow-sg-2/g'
run_sed 's/shadow-tp-hero/shadow-sg-3/g'
run_sed 's/shadow-tp-primary/shadow-sg-primary/g'
run_sed 's/tp-grad-text/sg-grad-text/g'
run_sed 's/tp-grad-border/sg-grad-border/g'
run_sed 's/tp-row-alt/sg-row-alt/g'
run_sed 's/tp-aurora/sg-aurora/g'

# ── animations / keyframe classes ───────────────────────────────────
run_sed 's/tp-breathe-amber/sg-breathe-accent/g'
run_sed 's/tp-breathe/sg-breathe/g'
run_sed 's/tp-draw-in/sg-draw-in/g'
run_sed 's/tp-just-now/sg-just-now/g'
run_sed 's/tp-badge-pulse/sg-badge-pulse/g'
run_sed 's/tp-tick-up/sg-tick-up/g'
run_sed 's/tp-palette-in/sg-palette-in/g'
run_sed 's/tp-ease-out/sg-ease-out/g'

# ── markers / json tokens / legacy utility classes ──────────────────
run_sed 's/tp-json-/sg-json-/g'
run_sed 's/tp-bg-root/sg-bg-root/g'
run_sed 's/emboss-inset/sg-inset/g'
run_sed 's/([" ])emboss([" ])/\1sg-card\2/g'
run_sed 's/pattern-active-sm//g'
run_sed 's/pattern-active/bg-sg-accent-soft shadow-[0_0_0_1px_var(--sg-accent-glow)]/g'

echo "done. residual check:"
grep -rnE '\btp-' app components lib tests --include='*.tsx' --include='*.ts' | grep -v 'globals.css' || echo "  zero tp- consumers remain"
