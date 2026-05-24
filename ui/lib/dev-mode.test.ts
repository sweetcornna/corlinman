/**
 * useDevMode tests.
 *
 * Covers:
 *   - default-off when localStorage is empty
 *   - round-trip: setEnabled(true) → readback is true
 *   - toggle flips the value and persists
 *   - cross-instance broadcast: a second hook sees the update
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  DEV_MODE_KEY,
  __resetDevModeForTests,
  useDevMode,
} from "./dev-mode";

beforeEach(() => {
  __resetDevModeForTests();
});

afterEach(() => {
  __resetDevModeForTests();
});

describe("useDevMode", () => {
  it("defaults to disabled when localStorage is empty", () => {
    const { result } = renderHook(() => useDevMode());
    expect(result.current.enabled).toBe(false);
  });

  it("persists setEnabled(true) to localStorage", () => {
    const { result } = renderHook(() => useDevMode());
    act(() => {
      result.current.setEnabled(true);
    });
    expect(result.current.enabled).toBe(true);
    expect(window.localStorage.getItem(DEV_MODE_KEY)).toBe("1");
  });

  it("persists setEnabled(false) to localStorage", () => {
    const { result } = renderHook(() => useDevMode());
    act(() => {
      result.current.setEnabled(true);
    });
    act(() => {
      result.current.setEnabled(false);
    });
    expect(result.current.enabled).toBe(false);
    expect(window.localStorage.getItem(DEV_MODE_KEY)).toBe("0");
  });

  it("toggle flips the value", () => {
    const { result } = renderHook(() => useDevMode());
    expect(result.current.enabled).toBe(false);
    act(() => {
      result.current.toggle();
    });
    expect(result.current.enabled).toBe(true);
    act(() => {
      result.current.toggle();
    });
    expect(result.current.enabled).toBe(false);
  });

  it("rehydrates persisted value on a fresh hook mount", () => {
    window.localStorage.setItem(DEV_MODE_KEY, "1");
    const { result } = renderHook(() => useDevMode());
    // First render is the SSR-safe default, the effect updates state
    // synchronously inside renderHook so result.current already reflects it.
    expect(result.current.enabled).toBe(true);
  });

  it("broadcasts updates to a second hook instance", () => {
    const a = renderHook(() => useDevMode());
    const b = renderHook(() => useDevMode());

    expect(a.result.current.enabled).toBe(false);
    expect(b.result.current.enabled).toBe(false);

    act(() => {
      a.result.current.setEnabled(true);
    });

    expect(a.result.current.enabled).toBe(true);
    expect(b.result.current.enabled).toBe(true);
  });
});
