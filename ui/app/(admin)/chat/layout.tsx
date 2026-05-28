/**
 * /chat layout — full-bleed two-column shell.
 *
 * The admin shell's outer layout (sidebar + topnav) is already provided by
 * `(admin)/layout.tsx`. This layout occupies the main-content slot and just
 * stretches its child to fill the viewport so the chat sidebar + thread
 * can manage their own scroll independently.
 */

import * as React from "react";

export default function ChatLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div
      className="flex h-[calc(100dvh-4rem)] min-h-0 w-full gap-3 overflow-hidden p-3 sm:gap-4 sm:p-4"
      data-testid="chat-layout"
    >
      {children}
    </div>
  );
}
