/**
 * Tiny presentational helpers shared by the model-hub sections
 * (providers table + custom providers). Extracted verbatim from
 * `app/(admin)/providers/page.tsx` (PR4 model-hub consolidation).
 */

import { Plug } from "lucide-react";

export function EmptyProviders({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-md border border-dashed border-sg-border py-10 text-center">
      <Plug className="h-6 w-6 text-sg-ink-3/60" />
      <p className="text-sm font-medium">{title}</p>
      <p className="max-w-sm text-xs text-sg-ink-3">{hint}</p>
    </div>
  );
}

export function BackendPendingBanner({ label }: { label: string }) {
  return (
    <div
      className="rounded-md border border-dashed border-sg-border bg-sg-card px-4 py-6 text-center text-xs text-sg-ink-3"
      data-testid="backend-pending"
    >
      {label}
    </div>
  );
}
