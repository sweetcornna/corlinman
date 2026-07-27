/**
 * Capability bindings — which model serves chat, image generation and speech.
 *
 * Mirrors `/admin/models/capabilities` in
 * `routes_admin_b/config_admin/models.py`. Read is composed from live
 * config; each capability is written back through its own endpoint so the
 * three stay decoupled (speech is owned by `/admin/voice/settings`, which
 * this module deliberately does not duplicate).
 *
 * Why this exists: chat had a visible default, but image generation was
 * implicit — the first provider flagged `image_capable`, else the chat
 * provider — and speech lived only on the voice page. There was no single
 * place to answer "what actually runs when the agent draws or speaks?".
 */

import { apiFetch } from "@/lib/api";

export interface TextCapability {
  model: string;
}

export interface ImageCapability {
  /** Provider slot name; empty = unbound (falls back to the chat provider). */
  provider: string;
  model: string;
  /** Slots declaring `image_capable` — a picker hint, not a constraint. */
  capable_providers: string[];
}

export interface VoiceCapability {
  enabled: boolean;
  backend: string;
  model: string;
  voice: string;
}

export interface ModelCapabilities {
  text: TextCapability;
  image: ImageCapability;
  voice: VoiceCapability;
  /** Alias names, offered as image-model suggestions. */
  aliases: string[];
}

export function getModelCapabilities(): Promise<ModelCapabilities> {
  return apiFetch<ModelCapabilities>("/admin/models/capabilities");
}

/** Bind the global image model. Empty strings clear the binding. */
export function putImageCapability(body: {
  provider: string;
  model: string;
}): Promise<{ status: string; provider: string; model: string }> {
  return apiFetch("/admin/models/capabilities/image", {
    method: "PUT",
    body,
  });
}
