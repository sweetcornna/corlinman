import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import * as React from "react";

import { QqFiltersPanel } from "./qq-filters-panel";

afterEach(cleanup);

function Harness({
  initial,
  onDraftChange,
}: {
  initial: Record<string, string[]>;
  onDraftChange?: (next: Record<string, string[]>) => void;
}) {
  const [draft, setDraft] = React.useState(initial);
  return (
    <QqFiltersPanel
      draft={draft}
      saving={false}
      dirty
      onChange={(next) => {
        setDraft(next);
        onDraftChange?.(next);
      }}
      onSave={() => {}}
    />
  );
}

function keywordInput(): HTMLInputElement {
  // One tag input per group row; single-row harness → single match.
  return screen.getByPlaceholderText("添加关键词...") as HTMLInputElement;
}

describe("QqFiltersPanel keyword tag input", () => {
  it("adds multiple keywords via Enter", () => {
    const seen: Record<string, string[]>[] = [];
    render(<Harness initial={{ "42": [] }} onDraftChange={(n) => seen.push(n)} />);
    const input = keywordInput();
    fireEvent.change(input, { target: { value: "报错" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.change(input, { target: { value: "帮忙" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(seen.at(-1)).toEqual({ "42": ["报错", "帮忙"] });
    expect(input.value).toBe("");
  });

  it("ignores the Enter that commits an IME composition", () => {
    const seen: Record<string, string[]>[] = [];
    render(<Harness initial={{ "42": [] }} onDraftChange={(n) => seen.push(n)} />);
    const input = keywordInput();
    // Mid-composition state: the input holds the raw pinyin buffer and
    // the user presses Enter to COMMIT the composition, not the tag.
    fireEvent.change(input, { target: { value: "bangmang" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(seen).toEqual([]); // nothing added, buffer untouched
    // Composition resolves to the real text; a plain Enter then lands it.
    fireEvent.change(input, { target: { value: "帮忙" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(seen.at(-1)).toEqual({ "42": ["帮忙"] });
  });

  it("commits pending input on blur so Save cannot drop it", () => {
    const seen: Record<string, string[]>[] = [];
    render(<Harness initial={{ "42": ["已有"] }} onDraftChange={(n) => seen.push(n)} />);
    const input = keywordInput();
    fireEvent.change(input, { target: { value: "新词" } });
    fireEvent.blur(input); // e.g. clicking the 保存 button
    expect(seen.at(-1)).toEqual({ "42": ["已有", "新词"] });
  });

  it("blur with an empty buffer is a no-op", () => {
    const seen: Record<string, string[]>[] = [];
    render(<Harness initial={{ "42": ["已有"] }} onDraftChange={(n) => seen.push(n)} />);
    fireEvent.blur(keywordInput());
    expect(seen).toEqual([]);
  });
});
