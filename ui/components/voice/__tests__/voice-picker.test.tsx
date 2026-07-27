import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { VoicePicker } from "@/components/voice/voice-picker";
import type { VoiceBackend } from "@/lib/api/voice";

function backend(overrides: Partial<VoiceBackend> = {}): VoiceBackend {
  return {
    id: "openai",
    label: "OpenAI",
    kind: "http",
    description: "",
    models: ["gpt-4o-mini-tts"],
    default_model: "gpt-4o-mini-tts",
    voices: [
      {
        id: "marin",
        label: "Marin",
        description: "自然",
        tone: "自然",
        recommended: true,
      },
      { id: "alloy", label: "Alloy", description: "中性", tone: "中性", recommended: false },
    ],
    formats: ["mp3"],
    default_voice: "alloy",
    free_form_voices: false,
    supports_instructions: true,
    supports_speed: true,
    custom: false,
    credential_set: true,
    api_key_env: "OPENAI_API_KEY",
    base_url: "",
    ...overrides,
  };
}

describe("VoicePicker", () => {
  it("renders one card per catalogued voice and marks the selected one", () => {
    render(
      <VoicePicker
        backend={backend()}
        value="alloy"
        onChange={vi.fn()}
        onPreview={vi.fn()}
        previewingId={null}
      />,
    );
    expect(screen.getAllByTestId("voice-card")).toHaveLength(2);
    const selected = screen
      .getAllByTestId("voice-card")
      .filter((el) => el.getAttribute("data-selected") === "true");
    expect(selected).toHaveLength(1);
  });

  it("selecting and previewing are separate actions", () => {
    const onChange = vi.fn();
    const onPreview = vi.fn();
    render(
      <VoicePicker
        backend={backend()}
        value="alloy"
        onChange={onChange}
        onPreview={onPreview}
        previewingId={null}
      />,
    );

    fireEvent.click(screen.getByTestId("voice-card-select-marin"));
    expect(onChange).toHaveBeenCalledWith("marin");
    expect(onPreview).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("voice-card-preview-marin"));
    expect(onPreview).toHaveBeenCalledWith("marin");
  });

  it("falls back to a free-text field for clone-handle backends", () => {
    // Fish / ElevenLabs / MiniMax have no fixed catalog — the id is a
    // handle the operator created in the vendor console.
    const onChange = vi.fn();
    render(
      <VoicePicker
        backend={backend({ id: "fish", free_form_voices: true, voices: [] })}
        value="ref-1"
        onChange={onChange}
        onPreview={vi.fn()}
        previewingId={null}
      />,
    );
    expect(screen.queryByTestId("voice-picker-grid")).not.toBeInTheDocument();
    const input = screen.getByTestId("voice-freeform-input");
    expect(input).toHaveValue("ref-1");
    fireEvent.change(input, { target: { value: "ref-2" } });
    expect(onChange).toHaveBeenCalledWith("ref-2");
  });

  it("disables the preview button for the voice currently rendering", () => {
    render(
      <VoicePicker
        backend={backend()}
        value="alloy"
        onChange={vi.fn()}
        onPreview={vi.fn()}
        previewingId="marin"
      />,
    );
    expect(screen.getByTestId("voice-card-preview-marin")).toBeDisabled();
    expect(screen.getByTestId("voice-card-preview-alloy")).not.toBeDisabled();
  });

  it("shows an empty note for a catalog-less non-clone backend", () => {
    render(
      <VoicePicker
        backend={backend({ voices: [], free_form_voices: false })}
        value=""
        onChange={vi.fn()}
        onPreview={vi.fn()}
        previewingId={null}
      />,
    );
    expect(screen.getByTestId("voice-picker-empty")).toBeInTheDocument();
  });
});
