import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { Composer } from "@/components/chat/composer";

function renderComposer(props: Partial<React.ComponentProps<typeof Composer>>) {
  return render(
    <Composer
      isStreaming={false}
      modelLabel="m"
      onSend={vi.fn()}
      onStop={vi.fn()}
      onReasoningEffortChange={vi.fn()}
      {...props}
    />,
  );
}

describe("Composer × reasoning tiers", () => {
  it("renders the model's real ladder (gpt-5.6: six tiers)", () => {
    renderComposer({
      reasoningTiers: ["none", "low", "medium", "high", "xhigh", "max"],
    });
    for (const tier of ["none", "low", "medium", "high", "xhigh", "max"]) {
      expect(
        screen.getByTestId(`composer-reasoning-${tier}`),
      ).toBeInTheDocument();
    }
    expect(screen.queryByTestId("composer-reasoning-minimal")).toBeNull();
  });

  it("renders a pure-toggle ladder (glm-4.6: off/on)", () => {
    renderComposer({ reasoningTiers: ["none", "on"] });
    expect(screen.getByTestId("composer-reasoning-none")).toBeInTheDocument();
    expect(screen.getByTestId("composer-reasoning-on")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-reasoning-medium")).toBeNull();
  });

  it("hides the control entirely for a no-knob model", () => {
    renderComposer({ reasoningTiers: [] });
    expect(screen.queryByTestId("composer-reasoning")).toBeNull();
  });

  it("highlights the clamped tier when the stored one is unsupported", () => {
    renderComposer({
      reasoningTiers: ["low", "high"],
      reasoningEffort: "medium",
    });
    // medium is equidistant → clamps down to low
    expect(
      screen.getByTestId("composer-reasoning-low").getAttribute("aria-checked"),
    ).toBe("true");
  });

  it("keeps the legacy 3/4-option set when the ladder is unknown", () => {
    renderComposer({ reasoningTiers: null });
    for (const tier of ["low", "medium", "high"]) {
      expect(
        screen.getByTestId(`composer-reasoning-${tier}`),
      ).toBeInTheDocument();
    }
    expect(screen.queryByTestId("composer-reasoning-xhigh")).toBeNull();
    expect(screen.queryByTestId("composer-reasoning-none")).toBeNull();
  });
});
