"use client";

/**
 * Composed providers admin surface — the provider registry table + the
 * shared add/edit editor dialog.
 *
 * This is the same `ProvidersAdminContent` that used to live in
 * `app/(admin)/providers/page.tsx`; PR4 (model-hub consolidation) moved it
 * here so the canonical `/models` page can host it as its "Providers & Keys"
 * tab while `/providers` and `/credentials` shrink to redirect stubs.
 *
 * The separate "Custom providers" section was REMOVED here (1.28.1): it was
 * a parallel add-flow writing the same `[providers.*]` registry (just with a
 * `params.custom=true` marker), and every custom provider already shows in
 * the main table above — so the section duplicated the table and confused
 * users ("是不是重复了"). The main table + editor add/edit/delete any
 * provider, custom-marked or not; the `/admin/providers/custom` endpoint
 * stays for back-compat but no longer has a redundant UI. `onCustomProvidersChanged`
 * is kept in the props (no-op) so hosts don't need to change.
 */

import * as React from "react";

import type { ProviderView } from "@/lib/api";
import { ProvidersTable } from "./providers-table";
import { ProviderEditorDialog } from "./provider-editor-dialog";

export type ProvidersAdminContentProps = {
  /** Retained for source-compat; the custom-providers section that used
   * this is gone, so it never fires now. */
  onCustomProvidersChanged?: () => void;
};

export function ProvidersAdminContent(
  _props: ProvidersAdminContentProps = {},
) {
  const [editorOpen, setEditorOpen] = React.useState(false);
  const [editing, setEditing] = React.useState<ProviderView | null>(null);

  return (
    <>
      <ProvidersTable
        onAdd={() => {
          setEditing(null);
          setEditorOpen(true);
        }}
        onEdit={(p) => {
          setEditing(p);
          setEditorOpen(true);
        }}
      />

      <ProviderEditorDialog
        open={editorOpen}
        onOpenChange={(o) => {
          setEditorOpen(o);
          if (!o) setEditing(null);
        }}
        editing={editing}
      />
    </>
  );
}
