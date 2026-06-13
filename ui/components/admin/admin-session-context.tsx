"use client";

import * as React from "react";

import type { AdminSession } from "@/lib/auth";

const AdminSessionContext = React.createContext<AdminSession | null>(null);

export function AdminSessionProvider({
  session,
  children,
}: {
  session: AdminSession | null;
  children: React.ReactNode;
}) {
  return (
    <AdminSessionContext.Provider value={session}>
      {children}
    </AdminSessionContext.Provider>
  );
}

export function useAdminSession(): AdminSession | null {
  return React.useContext(AdminSessionContext);
}
