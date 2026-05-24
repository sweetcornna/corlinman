/**
 * Developer-mode toggle (Operator-default simplification).
 *
 * Persists a single boolean to `localStorage` so power-user pages — Config /
 * Tenants / Credentials / Agents / Characters / Skills / Plugins / Embedding /
 * Federation / Hooks / RAG / Profiles / Nodes / Evolution / TagMemo / Diary /
 * Canvas — can be hidden from the sidebar by default. They remain reachable
 * via the `/admin/dev-settings` dashboard regardless of this flag.
 *
 * SSR-safe: returns `false` during server-render (the dashboard hydrates on
 * the client before the user can flip the switch, so the initial mismatch is
 * a no-op).
 */

import * as React from "react";

/** localStorage key. Bumping the suffix invalidates previous opt-ins. */
export const DEV_MODE_KEY = "corlinman.devMode.v1";

/* ------------------------------------------------------------------ */
/*                       SSR-safe accessors                           */
/* ------------------------------------------------------------------ */

function readDevMode(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(DEV_MODE_KEY) === "1";
  } catch {
    return false;
  }
}

function writeDevMode(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DEV_MODE_KEY, value ? "1" : "0");
  } catch {
    /* localStorage may be unavailable in private mode / sandboxed iframes. */
  }
}

/* ------------------------------------------------------------------ */
/*                  Cross-instance subscription bus                   */
/* ------------------------------------------------------------------ */
//
// Multiple components mount `useDevMode()` (sidebar + dev-settings dashboard
// at minimum). Each consumer needs to see the same value, so we maintain a
// tiny pub/sub broadcast plus also listen to the native `storage` event so
// changes from another tab propagate.

type Listener = (next: boolean) => void;
const listeners = new Set<Listener>();

function emit(value: boolean): void {
  listeners.forEach((l) => l(value));
}

/* ------------------------------------------------------------------ */
/*                            Public hook                             */
/* ------------------------------------------------------------------ */

export interface UseDevMode {
  /** `true` when developer-only pages should appear in the sidebar. */
  enabled: boolean;
  /** Flip the current value. Persists to localStorage + notifies subscribers. */
  toggle: () => void;
  /** Explicit setter — used by the dashboard toggle. */
  setEnabled: (value: boolean) => void;
}

/**
 * React hook that returns the current dev-mode state.
 *
 * The first render returns `false` (SSR-safe default). After the effect
 * runs we read from `localStorage` and broadcast the real value to all
 * consumers — this matches the pattern used by the sidebar `collapsed`
 * preference, which deliberately renders the operator-default UI on the
 * server and then hydrates the persisted choice on the client.
 */
export function useDevMode(): UseDevMode {
  const [enabled, setEnabledState] = React.useState<boolean>(false);

  // Hydrate from localStorage + subscribe to peer updates.
  React.useEffect(() => {
    setEnabledState(readDevMode());

    const listener: Listener = (next) => setEnabledState(next);
    listeners.add(listener);

    // Also react to cross-tab changes through the native event.
    const onStorage = (e: StorageEvent) => {
      if (e.key === DEV_MODE_KEY) {
        setEnabledState(readDevMode());
      }
    };
    if (typeof window !== "undefined") {
      window.addEventListener("storage", onStorage);
    }

    return () => {
      listeners.delete(listener);
      if (typeof window !== "undefined") {
        window.removeEventListener("storage", onStorage);
      }
    };
  }, []);

  const setEnabled = React.useCallback((value: boolean) => {
    writeDevMode(value);
    setEnabledState(value);
    emit(value);
  }, []);

  const toggle = React.useCallback(() => {
    setEnabled(!readDevMode());
  }, [setEnabled]);

  return { enabled, toggle, setEnabled };
}

/* ------------------------------------------------------------------ */
/*                       Test-only utilities                          */
/* ------------------------------------------------------------------ */

/** Resets the persisted dev-mode flag. Used by tests. */
export function __resetDevModeForTests(): void {
  if (typeof window !== "undefined") {
    try {
      window.localStorage.removeItem(DEV_MODE_KEY);
    } catch {
      /* ignore */
    }
  }
  emit(false);
}
