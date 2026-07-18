import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { QuestionCard } from "@/components/chat/question-card";

const RADIO_Q = {
  question: "侧重哪方面?",
  options: ["代码质量", "安全", "性能"],
  multiple: false,
};

describe("QuestionCard", () => {
  it("radio: clicking an option sends it immediately and latches", () => {
    const onAnswer = vi.fn();
    render(<QuestionCard question={RADIO_Q} interactive onAnswer={onAnswer} />);

    fireEvent.click(screen.getByRole("button", { name: "安全" }));
    expect(onAnswer).toHaveBeenCalledWith("安全");

    // Latched — a second click must not double-send.
    fireEvent.click(screen.getByRole("button", { name: /代码质量/ }));
    expect(onAnswer).toHaveBeenCalledTimes(1);
  });

  it("multiple: toggles picks, submit sends them joined", () => {
    const onAnswer = vi.fn();
    render(
      <QuestionCard
        question={{ ...RADIO_Q, multiple: true }}
        interactive
        onAnswer={onAnswer}
      />,
    );

    const submit = screen.getByTestId("question-submit");
    expect(submit).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "代码质量" }));
    fireEvent.click(screen.getByRole("button", { name: /安全/ }));
    expect(onAnswer).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("question-submit"));
    expect(onAnswer).toHaveBeenCalledWith("代码质量、安全");
  });

  it("multiple: latin-only labels join with a comma", () => {
    const onAnswer = vi.fn();
    render(
      <QuestionCard
        question={{
          question: "Which?",
          options: ["Red", "Blue"],
          multiple: true,
        }}
        interactive
        onAnswer={onAnswer}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Red" }));
    fireEvent.click(screen.getByRole("button", { name: /Blue/ }));
    fireEvent.click(screen.getByTestId("question-submit"));
    expect(onAnswer).toHaveBeenCalledWith("Red, Blue");
  });


  it("non-interactive history renders inert options", () => {
    const onAnswer = vi.fn();
    render(
      <QuestionCard question={RADIO_Q} interactive={false} onAnswer={onAnswer} />,
    );
    const option = screen.getAllByTestId("question-option")[0]!;
    expect(option).toBeDisabled();
    fireEvent.click(option);
    expect(onAnswer).not.toHaveBeenCalled();
    expect(screen.queryByTestId("question-submit")).toBeNull();
  });

  it("shows the question line only when asked to", () => {
    const { rerender } = render(
      <QuestionCard question={RADIO_Q} interactive showQuestion />,
    );
    expect(screen.getByText("侧重哪方面?")).toBeInTheDocument();

    rerender(<QuestionCard question={RADIO_Q} interactive />);
    expect(screen.queryByText("侧重哪方面?")).toBeNull();
  });

  it("renders nothing for a question with no options and no question line", () => {
    const { container } = render(
      <QuestionCard
        question={{ question: "q", options: [], multiple: false }}
        interactive
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});
