"use client";

/**
 * Composed providers admin surface — built-in providers table + custom
 * providers section + the shared add/edit editor dialog.
 *
 * This is the same `ProvidersAdminContent` that used to live in
 * `app/(admin)/providers/page.tsx`; PR4 (model-hub consolidation) moved it
 * here so the canonical `/models` page can host it as its "Providers & Keys"
 * tab while `/providers` and `/credentials` shrink to redirect stubs.
 */

import * as React from "react";

import type { ProviderView } from "@/lib/api";
import { ProvidersTable } from "./providers-table";
import { CustomProvidersSection } from "./custom-providers-section";
import { ProviderEditorDialog } from "./provider-editor-dialog";

export type ProvidersAdminContentProps = {
  onCustomProvidersChanged?: () => void;
};

export function ProvidersAdminContent({
  onCustomProvidersChanged,
}: ProvidersAdminContentProps = {}) {
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

      <CustomProvidersSection
        onCustomProvidersChanged={onCustomProvidersChanged}
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
