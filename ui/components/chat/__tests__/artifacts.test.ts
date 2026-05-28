import { describe, expect, it } from "vitest";
import { renderHook, act } from "@testing-library/react";

import {
  deriveArtifactKind,
  deriveArtifactTitle,
  isPreviewableLanguage,
  useArtifacts,
} from "@/lib/chat/artifacts";

describe("artifact helpers", () => {
  it("deriveArtifactKind maps known languages", () => {
    expect(deriveArtifactKind("html")).toBe("html");
    expect(deriveArtifactKind("SVG")).toBe("svg");
    expect(deriveArtifactKind("mermaid")).toBe("mermaid");
    expect(deriveArtifactKind("md")).toBe("markdown");
    expect(deriveArtifactKind("Markdown")).toBe("markdown");
    expect(deriveArtifactKind("python")).toBe("code");
    expect(deriveArtifactKind("")).toBe("code");
  });

  it("isPreviewableLanguage is case-insensitive", () => {
    expect(isPreviewableLanguage("HTML")).toBe(true);
    expect(isPreviewableLanguage("rust")).toBe(false);
  });

  it("deriveArtifactTitle truncates long first lines", () => {
    const long = "a".repeat(80);
    const title = deriveArtifactTitle("py", long);
    expect(title.length).toBeLessThan(80);
    expect(title).toContain("py:");
  });
});

describe("useArtifacts", () => {
  it("opens an artifact, activates it, and reflects panelOpen", () => {
    const { result } = renderHook(() => useArtifacts());
    expect(result.current.artifacts).toEqual([]);
    expect(result.current.panelOpen).toBe(false);

    act(() => {
      result.current.open({
        id: "a",
        kind: "code",
        title: "py",
        language: "py",
        source: "x",
        messageId: "m1",
      });
    });

    expect(result.current.artifacts).toHaveLength(1);
    expect(result.current.activeId).toBe("a");
    expect(result.current.panelOpen).toBe(true);
  });

  it("opening the same id with different source records a new version", () => {
    const { result } = renderHook(() => useArtifacts());
    act(() =>
      result.current.open({
        id: "a",
        kind: "code",
        title: "py",
        language: "py",
        source: "v1",
        messageId: "m1",
      }),
    );
    act(() =>
      result.current.open({
        id: "a",
        kind: "code",
        title: "py",
        language: "py",
        source: "v2",
        messageId: "m1",
      }),
    );
    expect(result.current.artifacts).toHaveLength(1);
    expect(result.current.artifacts[0].source).toBe("v2");
    expect(result.current.artifacts[0].versions).toEqual(["v1", "v2"]);
  });

  it("remove drops the artifact and clears active if it was selected", () => {
    const { result } = renderHook(() => useArtifacts());
    act(() =>
      result.current.open({
        id: "a",
        kind: "code",
        title: "py",
        language: "py",
        source: "x",
        messageId: "m1",
      }),
    );
    act(() => result.current.remove("a"));
    expect(result.current.artifacts).toHaveLength(0);
    expect(result.current.activeId).toBeNull();
  });

  it("close hides the panel but keeps the artifact list", () => {
    const { result } = renderHook(() => useArtifacts());
    act(() =>
      result.current.open({
        id: "a",
        kind: "code",
        title: "py",
        language: "py",
        source: "x",
        messageId: "m1",
      }),
    );
    act(() => result.current.close());
    expect(result.current.panelOpen).toBe(false);
    expect(result.current.artifacts).toHaveLength(1);
  });
});
