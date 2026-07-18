"use client";

import { Menu } from "lucide-react";

import { Breadcrumbs } from "./breadcrumbs";
import { HealthDot } from "./health-dot";
import { LanguageToggle } from "./language-toggle";
import { ProfileSwitcher } from "./profile-switcher";
import { SearchTrigger } from "./search-trigger";
import { TenantSwitcher } from "./tenant-switcher";
import { useMobileDrawer } from "./mobile-drawer-context";
import { UpdateBubble } from "@/components/system/update-bubble";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { AccentPicker } from "@/components/ui/accent-picker";
import { cn } from "@/lib/utils";

/**
 * Spatial Glass topbar. Floating glass panel (shell tier — real blur allowed)
 * — matches the sidebar's gutter treatment. Left: breadcrumbs. Right: search
 * (⌘K), health dot, language, theme. Logout + user info live in the sidebar.
 *
 * Mobile (<md): leading slot carries a hamburger that opens the sidebar
 * drawer (the sidebar itself is hidden off-canvas until then).
 */
export function TopNav() {
  const { toggle, open } = useMobileDrawer();
  return (
    <header
      className={cn(
        "sticky top-2 md:top-4 z-40 flex h-14 items-center justify-between gap-2 md:gap-4 rounded-sg-lg border border-sg-border bg-sg-shell px-3 md:px-4",
        "shadow-sg-2",
        // Liquid Glass optics — light-aware edge ring + chromatic inner
        // lensing, matching the sidebar rail so the shell reads as one
        // continuous bent-light material. Blur-free.
        "",
      )}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2 md:gap-3">
        <button
          type="button"
          onClick={toggle}
          aria-label="Toggle navigation drawer"
          aria-expanded={open}
          aria-controls="admin-sidebar"
          data-testid="mobile-nav-trigger"
          className=" -ml-1 inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-sg-sm text-sg-ink-2 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40 md:hidden"
        >
          <Menu className="h-5 w-5" aria-hidden />
        </button>
        <Breadcrumbs />
      </div>
      <div className="flex shrink-0 items-center gap-1.5 md:gap-2">
        <SearchTrigger />
        <div className="hidden h-5 w-px bg-sg-border md:block" />
        <ProfileSwitcher className="hidden md:inline-flex" />
        <TenantSwitcher className="hidden md:inline-flex" />
        <HealthDot className="hidden md:inline-flex" />
        <UpdateBubble className="hidden md:inline-flex" />
        <LanguageToggle />
        <AccentPicker />
        <ThemeToggle />
      </div>
    </header>
  );
}
