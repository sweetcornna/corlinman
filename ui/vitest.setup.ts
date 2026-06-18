import "@testing-library/jest-dom/vitest";

// Initialise i18n synchronously at setup time so tests that render
// components using `useTranslation()` find the zh-CN bundle on the first
// render pass. Setup runs before any test file is loaded, so this is the
// right spot for a module-level init.
import { initI18n, i18next } from "@/lib/i18n";

// Force zh-CN as the default test locale (jsdom's `navigator.language` is
// `en-US` otherwise, which would otherwise flip the LanguageDetector to
// English and break the Chinese test assertions).
initI18n();
void i18next.changeLanguage("zh-CN");

// jsdom ships no `EventSource`, so any component that opens an SSE stream on
// mount (the live sub-agent panels, status card, sessions timeline) would
// throw a ReferenceError under test. Install a no-op stub exposing the surface
// those components touch — they never receive frames in unit tests; the SSE
// wire behaviour is covered by the backend route tests.
if (typeof (globalThis as { EventSource?: unknown }).EventSource === "undefined") {
  class MockEventSource {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSED = 2;
    url: string;
    withCredentials: boolean;
    readyState = MockEventSource.CONNECTING;
    onmessage: ((ev: MessageEvent) => void) | null = null;
    onerror: ((ev: Event) => void) | null = null;
    onopen: ((ev: Event) => void) | null = null;
    constructor(url: string, init?: { withCredentials?: boolean }) {
      this.url = url;
      this.withCredentials = Boolean(init?.withCredentials);
    }
    addEventListener(): void {}
    removeEventListener(): void {}
    close(): void {
      this.readyState = MockEventSource.CLOSED;
    }
    dispatchEvent(): boolean {
      return false;
    }
  }
  (globalThis as { EventSource?: unknown }).EventSource = MockEventSource;
}
