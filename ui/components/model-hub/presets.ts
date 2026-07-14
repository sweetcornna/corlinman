/**
 * Hardcoded provider presets for step 1 of the guided setup flow
 * (`provider-setup-flow.tsx`). Keyed to the existing `KINDS` catalog in
 * `./alias-helpers.ts`; the OAuth-capable presets reference the SAME
 * provider identifiers the OAuth panel / login modal use
 * (`components/admin/oauth-login-modal.tsx`: anthropic / codex / gemini /
 * xai), so the flow can hand off to the existing PKCE surfaces.
 *
 * `suggestedEnvVar` mirrors each adapter's canonical env fallback in
 * `python/packages/corlinman-providers` (e.g. `DASHSCOPE_API_KEY` for
 * qwen, `ZHIPU_API_KEY` for glm) so an env-sourced key Just Works.
 */

import type { ProviderKind } from "@/lib/api";
import type { OAuthLoginProvider } from "@/components/admin/oauth-login-modal";

export interface SetupPreset {
  /** Stable preset id — doubles as the prefilled provider name. */
  id: string;
  kind: ProviderKind;
  /** i18n key under `setupFlow.preset.*`. */
  labelKey: string;
  /** Prefilled base_url (required for openai_compatible kinds). */
  defaultBaseUrl?: string;
  /** Canonical env var prefilled when the operator picks the env source. */
  suggestedEnvVar: string;
  /** True when the preset can authenticate via an existing OAuth flow. */
  oauth?: boolean;
}

export const SETUP_PRESETS: readonly SetupPreset[] = [
  {
    id: "anthropic",
    kind: "anthropic",
    labelKey: "setupFlow.preset.anthropic",
    suggestedEnvVar: "ANTHROPIC_API_KEY",
    oauth: true,
  },
  {
    id: "openai",
    kind: "openai",
    labelKey: "setupFlow.preset.openai",
    suggestedEnvVar: "OPENAI_API_KEY",
  },
  {
    id: "deepseek",
    kind: "deepseek",
    labelKey: "setupFlow.preset.deepseek",
    suggestedEnvVar: "DEEPSEEK_API_KEY",
  },
  {
    id: "qwen",
    kind: "qwen",
    labelKey: "setupFlow.preset.qwen",
    suggestedEnvVar: "DASHSCOPE_API_KEY",
  },
  {
    id: "glm",
    kind: "glm",
    labelKey: "setupFlow.preset.glm",
    suggestedEnvVar: "ZHIPU_API_KEY",
  },
  {
    id: "google",
    kind: "google",
    labelKey: "setupFlow.preset.google",
    suggestedEnvVar: "GOOGLE_API_KEY",
    oauth: true,
  },
  {
    // xAI has no first-class ProviderKind — its API is OpenAI-compatible,
    // so the preset pins the official base_url. OAuth rides the existing
    // xAI PKCE tile.
    id: "xai",
    kind: "openai_compatible",
    labelKey: "setupFlow.preset.xai",
    defaultBaseUrl: "https://api.x.ai/v1",
    suggestedEnvVar: "XAI_API_KEY",
    oauth: true,
  },
  {
    id: "custom",
    kind: "openai_compatible",
    labelKey: "setupFlow.preset.custom",
    suggestedEnvVar: "OPENAI_API_KEY",
  },
] as const;

/**
 * Preset id → OAuth flow id from `oauth-login-modal.tsx`. Only presets
 * with `oauth: true` appear here; the mapping is non-identity for google
 * (its PKCE surface is the "gemini" flow).
 */
export const PRESET_OAUTH_PROVIDER: Readonly<
  Record<string, OAuthLoginProvider>
> = {
  anthropic: "anthropic",
  google: "gemini",
  xai: "xai",
};
