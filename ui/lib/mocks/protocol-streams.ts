/**
 * Empty stream stubs for `/playground/protocol` page (B3 prototype).
 * Both stream functions yield nothing — the page renders an empty
 * transcript instead of fake assistant output.
 *
 * TODO(B3-BE1/BE2): swap to real SSE hits against
 *   `GET /admin/playground/protocol?variant=block`
 *   `GET /admin/playground/protocol?variant=function-call`
 * once the gateway bridge ships.
 */

export type ProtocolVariant = "block" | "function-call";

export interface StreamOptions {
  tokenDelayMs?: number;
  signal?: AbortSignal;
}

async function* emptyStream(): AsyncGenerator<string, void, unknown> {
  // Intentionally empty — yields no tokens so the page paints a clean
  // empty state until a real SSE endpoint lands.
}

export function streamBlockProtocol(
  _prompt: string,
  _opts: StreamOptions = {},
): AsyncGenerator<string, void, unknown> {
  void _opts;
  return emptyStream();
}

export function streamFunctionCall(
  _prompt: string,
  _opts: StreamOptions = {},
): AsyncGenerator<string, void, unknown> {
  void _opts;
  return emptyStream();
}
