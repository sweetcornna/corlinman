"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";
import { Check, Mic, Play, Sparkles } from "@/components/icons";

import { cn } from "@/lib/utils";
import { Input } from "@/components/ui/input";
import type { VoiceBackend, VoiceDef } from "@/lib/api/voice";

interface VoicePickerProps {
  backend: VoiceBackend;
  value: string;
  onChange: (voiceId: string) => void;
  /** Audition one voice without committing it as the default. */
  onPreview: (voiceId: string) => void;
  previewingId: string | null;
  disabled?: boolean;
}

/**
 * Voice selector for one backend.
 *
 * Two shapes, chosen by the backend rather than by id — a catalog
 * backend (OpenAI Realtime, OpenAI, Gemini) renders selectable cards, while a
 * clone backend (`free_form_voices`: Fish, ElevenLabs, MiniMax) has no
 * fixed catalog and gets a free-text field for the handle. Anything the
 * server adds later, including operator-defined backends, lands in one
 * of these two branches without a code change here.
 */
export function VoicePicker({
  backend,
  value,
  onChange,
  onPreview,
  previewingId,
  disabled = false,
}: VoicePickerProps) {
  const { t } = useTranslation();

  if (backend.free_form_voices) {
    return (
      <div className="space-y-2" data-testid="voice-picker-freeform">
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          placeholder={t(
            "voice.picker.freeformPlaceholder",
            "reference_id / voice_id",
          )}
          data-testid="voice-freeform-input"
        />
        <p className="text-xs text-sg-ink-3">
          {t(
            "voice.picker.freeformHint",
            "This provider identifies a voice by a clone handle you created in its console. Paste that id here.",
          )}
        </p>
      </div>
    );
  }

  if (backend.voices.length === 0) {
    return (
      <p className="text-xs text-sg-ink-3" data-testid="voice-picker-empty">
        {t("voice.picker.noVoices", "This backend exposes no voice catalog.")}
      </p>
    );
  }

  return (
    <ul
      className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3"
      data-testid="voice-picker-grid"
    >
      {backend.voices.map((voice) => (
        <VoiceCard
          key={voice.id}
          voice={voice}
          selected={voice.id === value}
          previewing={previewingId === voice.id}
          disabled={disabled}
          onSelect={() => onChange(voice.id)}
          onPreview={() => onPreview(voice.id)}
        />
      ))}
    </ul>
  );
}

function VoiceCard({
  voice,
  selected,
  previewing,
  disabled,
  onSelect,
  onPreview,
}: {
  voice: VoiceDef;
  selected: boolean;
  previewing: boolean;
  disabled: boolean;
  onSelect: () => void;
  onPreview: () => void;
}) {
  const { t } = useTranslation();
  return (
    <li>
      <div
        className={cn(
          "group flex h-full items-start gap-2 rounded-sg-md border p-3 transition-colors",
          selected
            ? "border-sg-tint/60 bg-sg-inset"
            : "border-sg-border bg-sg-card hover:bg-sg-inset-hover",
        )}
        data-testid="voice-card"
        data-selected={selected}
      >
        <button
          type="button"
          onClick={onSelect}
          disabled={disabled}
          className="min-w-0 flex-1 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/50"
          aria-pressed={selected}
          data-testid={`voice-card-select-${voice.id}`}
        >
          <span className="flex items-center gap-1.5">
            <span className="truncate text-sm text-sg-ink">{voice.label}</span>
            {voice.recommended ? (
              <Sparkles
                className="h-3 w-3 shrink-0 text-sg-tint"
                aria-label={t("voice.picker.recommended", "Recommended")}
              />
            ) : null}
            {selected ? (
              <Check className="h-3.5 w-3.5 shrink-0 text-sg-tint" aria-hidden="true" />
            ) : null}
          </span>
          {voice.tone ? (
            <span className="mt-0.5 block text-[10px] text-sg-ink-4">
              {voice.tone}
            </span>
          ) : null}
          {voice.description ? (
            <span className="mt-1 block text-xs leading-snug text-sg-ink-3">
              {voice.description}
            </span>
          ) : null}
        </button>
        <button
          type="button"
          onClick={onPreview}
          disabled={disabled || previewing}
          className="shrink-0 rounded-full p-1.5 text-sg-ink-3 transition-colors hover:bg-sg-ink/5 hover:text-sg-ink disabled:opacity-50"
          aria-label={t("voice.picker.previewVoice", "Preview {{name}}", {
            name: voice.label,
          })}
          data-testid={`voice-card-preview-${voice.id}`}
        >
          {previewing ? (
            <Mic className="h-3.5 w-3.5 animate-pulse" aria-hidden="true" />
          ) : (
            <Play className="h-3.5 w-3.5" aria-hidden="true" />
          )}
        </button>
      </div>
    </li>
  );
}
