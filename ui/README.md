# corlinman UI

Next.js admin console for the corlinman gateway.

## Dev

```bash
pnpm install
pnpm dev
```

## API source: mock vs real gateway

The admin pages talk to an API through `lib/api.ts`. Three switches control
where calls land:

| env var | effect |
| --- | --- |
| `NEXT_PUBLIC_GATEWAY_URL` | Real gateway base URL. Default: empty string (use current origin so nginx proxies `/admin/*` through). Set to `http://localhost:6005` for local dev without a proxy. |
| `NEXT_PUBLIC_MOCK_API_URL` | If set, *all* calls go here instead of the gateway (the standalone mock server in `ui/mock/server.ts`). |
| `NEXT_PUBLIC_MOCK_MODE` | `"1"` enables per-call inline mock payloads (offline dev with no mock server and no gateway). Anything else disables them. |

### Run against the real gateway (M6 default)

```bash
# 1. start the gateway (it reads ~/.corlinman/config.toml)
cargo run -p corlinman-gateway

# 2. run the UI against it
NEXT_PUBLIC_MOCK_API_URL= NEXT_PUBLIC_MOCK_MODE= pnpm dev
```

Admin routes (`/admin/*`) require HTTP Basic against
`config.admin.username` + `config.admin.password_hash` (argon2id).
For browser testing, visit `http://localhost:6005/admin/plugins` directly — the
browser prompts for credentials and then the UI at `http://localhost:3000`
picks up the stored creds via `credentials: "include"`.

### Run fully offline (no gateway, inline mocks)

```bash
NEXT_PUBLIC_MOCK_MODE=1 pnpm dev
```

### Run against the standalone mock server

```bash
pnpm mock &    # starts ui/mock/server.ts on :7777
NEXT_PUBLIC_MOCK_API_URL=http://127.0.0.1:7777 pnpm dev
```

## Tests

```bash
pnpm typecheck
pnpm lint
pnpm test        # vitest
pnpm build
```

## Known a11y debt

The full `tests/a11y-audit.test.tsx` runs axe-core against every admin page in jsdom. Two cases are currently skipped — both jsdom infrastructure limits, not real a11y violations:

- **`approvals`** — React 19 + react-query + SSE `setTimeout` cleanup interact to produce `destroy is not a function` on unmount in jsdom. The page itself renders fine in a real browser.
- **`canvas`** — axe cannot descend into a sandboxed iframe when the frame lives in a detached jsdom tree ("Respondable target must be a frame in the current window"). The iframe body is static placeholder HTML; the chrome around it is audited.

Both are covered by the real axe browser CLI in CI. See `tests/a11y-audit.test.tsx` for per-case `skip` reasons.

---

## Eclipse Minimal v2 design system

Since v1.31 the admin UI runs on **Eclipse Minimal v2** — the corlinman
design language from the claude.ai/design project: a pure-black canvas with
a moonrise halo, matte charcoal surfaces, a five-step moon-white ink scale,
and a **tint pipeline** that colors only the "light" (eclipse pearl, live
dots, streaming thread, caret, solid primary buttons, selected states) —
never the skeleton. `backdrop-filter` is banned app-wide and font weights
stop at 500; both are vitest-enforced (`app/globals-glass-vars.test.ts`,
`tailwind.config.test.ts`).

### Theme attribute + boot sequence

- `<html data-theme="light|dark">` drives token resolution. The `.dark`
  class is kept in lockstep for Tailwind's `dark:` variant. Dark ("deep
  night") is the first-class theme; light is the monochrome "Paper"
  inversion.
- Boot order (matters for no-FOUC):
  1. Inline script in `app/layout.tsx` reads `localStorage["corlinman-theme"]`
     (fallback: `?theme=` URL param → legacy `theme` key → `dark` default),
     sets `data-theme` + `.dark` before React hydrates, purges the retired
     Spatial Glass theme keys, and applies the persisted tint
     (`corlinman-tint`: preset via `data-tint` attribute, custom hue via an
     injected style block — its generator MUST stay in sync with
     `lib/tint.ts#buildTintCss`).
  2. `<ThemeProvider>` from `next-themes` (in `components/providers.tsx`)
     is configured with `attribute={["class","data-theme"]}` +
     `storageKey="corlinman-theme"`.
  3. `<ThemeToggle>` writes through the same key; the tint picker
     (`components/ui/accent-picker.tsx`) writes `corlinman-tint` through
     `lib/tint.ts`.

### Primitives

Under `components/ui/` and `components/admin/`. Every page consumes these;
don't recreate variants inline.

| Primitive | Purpose |
|---|---|
| `<GlassPanel variant="soft"\|"strong"\|"subtle"\|"primary">` | Core matte surface. All variants are opaque charcoal + moon edge; `primary` adds the selected treatment (inset tint glow via `shadow-sg-selected`). |
| `<PresenceOrb size="sm"\|"md"\|"hero" active>` | The eclipse pearl — the design language's signature element. `active` spins the corona (reduced-motion freezes it via CSS). |
| `<ThemeToggle>` | Sun/moon pill. |
| `<StatChip>` | Label + value + optional sparkline + delta. `variant="primary"` gets the selected treatment + `live` badge. |
| `<FilterChipGroup>` | Pill-style filter tabs — single-select or multi via a discriminated union. |
| `<StreamPill>` | Live/paused/throttled indicator with breathing dot + optional rate suffix. |
| `<LogRow variant="dense"\|"comfortable">` | Shared row primitive for log streams and activity feeds. |
| `<DetailDrawer>` | Inline (non-modal) right-side detail pane with `<DetailDrawer.Section>`. For modal variants use `components/ui/drawer.tsx` (Radix-Dialog). |
| `<JsonView>` | Hand-rolled syntax highlighter for JSON payloads — key/string/number/boolean/comment spans. |
| `<MiniSparkline>` | 6-bar availability/trend viz. |
| `<CommandPalette>` | Configurable ⌘K modal over cmdk. Takes `groups: PaletteGroup[]`; consumers inject actions from any page. |
| `<UptimeStreak>` | Dashboard/health big-number card with 30-bar history. |

Icons come from the self-drawn sprite (`public/icons-sprite.svg`, 24 grid /
1.8 stroke / round caps) via `components/icons` — the barrel exports
lucide-compatible PascalCase components; importing `lucide-react` is
test-forbidden.

### Tokens

All `--sg-*` variables live in `app/globals.css` (`:root` = Paper light,
`.dark` = Eclipse dark), mapped into Tailwind by `tailwind.config.ts`:
surfaces (`bg-sg-card`/`bg-sg-inset`/`bg-sg-opaque`), the ink scale
(`text-sg-ink` … `text-sg-ink-5`), light grammar shadows (`shadow-sg-edge`,
`shadow-sg-well`, `shadow-sg-1..4`, `shadow-sg-lift/scrim/selected`,
`shadow-sg-bloom-1..3`), and the tint family (`bg-sg-tint`,
`text-sg-tint-ink`, `ring-sg-tint`). `bg-sg-card-grad` is a single
background-image stack that carries BOTH the matte fill and the sheen — do
not pair it with a separate `bg-*` color class (tailwind-merge would drop
one).

Solid tint fills always pair with `text-sg-tint-ink` (auto-derived black or
white) so contrast holds for any preset or custom hue. Status colors
(`sg-ok/warn/err`) are muted and never tinted — semantics beat
personalization.

### Motion

- **Continuous** (pearl rotation, streaming thread, breathing dots): CSS
  keyframes in `globals.css` (`eclipse-turn`, `thread-pulse`,
  `sg-breathe*`), all frozen under `prefers-reduced-motion`. Budget: ≤ 3
  lively elements per screen.
- **Transient entrance**: Framer variants in `lib/motion.ts`, paired with
  instant copies returned by `useMotionVariants()` when reduced-motion is
  on.

### Adding a new page

1. Scaffold with `<GlassPanel variant="strong" as="section">` for the hero.
2. Stat row: `<StatChip variant="primary" live>` for the most active
   metric, `<StatChip>` defaults for the rest.
3. List / activity surface: `<GlassPanel soft>` + `<LogRow>`.
4. Detail pane: `<DetailDrawer>` + `<JsonView>`.
5. Offline state: copy the `OfflineBlock` pattern from
   `app/(admin)/plugins/page.tsx`.
6. i18n: add keys in both `lib/locales/zh-CN.ts` and `lib/locales/en.ts`.

### Design-language history

Tidepool (v0.4) → Spatial Glass + Liquid Glass (v1.19, PR #84) → **Eclipse Minimal v2** (v1.31, PR #145). Original Tidepool commit trail, for archaeology:

- `phase 0` — tokens + fonts + motion primitives
- `phase 1 wave A/B/C/D` — 12 new primitive components (+51 tests)
- `phase 2` — shell cutover (layout / sidebar / topbar / boot script)
- `phase 3` — Dashboard content
- `phase 3.5` — palette migration
- `phase 4` — Logs + virtualised stream + detail drawer
- `phase 5a–5f` — all 14 remaining admin pages + Login
- `phase 6` — a11y audit (23 pages × 0 serious), contrast hardening, docs

See `_design/migration-plan.md` for the scoping document and `_design/direction-f-tidepool.html` for the original HTML prototype.
