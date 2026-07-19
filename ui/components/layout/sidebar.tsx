"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import {
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  KeyRound,
  LogOut,
} from "@/components/icons";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { PresenceOrb } from "@/components/ui/presence-orb";
import { logout } from "@/lib/auth";
import { springs } from "@/lib/motion";
import { useDevMode } from "@/lib/dev-mode";
import {
  navHrefs,
  sidebarSections,
  type NavPageDef,
  type SidebarEntry,
} from "@/lib/nav-registry";
import { useMotion } from "@/components/ui/motion-safe";
import { useMobileDrawer } from "./mobile-drawer-context";
import { BrandMark } from "./brand-mark";
import { ChangePasswordDialog } from "./change-password-dialog";

/**
 * The page inventory lives in `@/lib/nav-registry` (single source of truth
 * shared with the command palette, the dev-settings grid and breadcrumbs).
 * This component only renders `sidebarSections(devMode)`: uppercase
 * non-collapsible section headers (对话/运营/配置/系统 + 开发者 in dev
 * mode) with Channels as the sole collapsible group. The old Credentials
 * row is gone — /credentials is a redirect stub into /models (PR4
 * model-hub consolidation).
 */
type SidebarGroupEntry = Extract<SidebarEntry, { kind: "group" }>;

/** Every navigable href in the rail — used for longest-match arbitration. */
const ALL_NAV_HREFS: string[] = navHrefs();

function matchesHref(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * Active = this href matches AND no other nav item matches more
 * specifically — so a nested route (e.g. /channels/qq/…) lights only
 * its own entry, never a shorter-prefix sibling as well.
 */
function isActiveHref(pathname: string, href: string): boolean {
  if (!matchesHref(pathname, href)) return false;
  return !ALL_NAV_HREFS.some(
    (other) =>
      other !== href && other.length > href.length && matchesHref(pathname, other),
  );
}

const COLLAPSE_KEY = "corlinman.sidebar.collapsed.v1";

function readCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false;
  }
}
function writeCollapsed(v: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(COLLAPSE_KEY, v ? "1" : "0");
  } catch {
    /* ignore */
  }
}

function useIsMobileSidebar(): boolean {
  const [isMobile, setIsMobile] = React.useState(false);

  React.useEffect(() => {
    const query = window.matchMedia("(max-width: 767px)");
    const update = () => setIsMobile(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  return isMobile;
}

interface SidebarProps {
  user?: string;
}

export function Sidebar({ user }: SidebarProps) {
  const pathname = usePathname() ?? "/";
  const router = useRouter();
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = React.useState(false);
  const [hydrated, setHydrated] = React.useState(false);
  const [loggingOut, setLoggingOut] = React.useState(false);
  const [changePasswordOpen, setChangePasswordOpen] = React.useState(false);
  const { open: drawerOpen } = useMobileDrawer();
  const mobileDrawerHidden = useIsMobileSidebar() && !drawerOpen;
  const { enabled: devModeEnabled } = useDevMode();
  const sections = React.useMemo(
    () => sidebarSections(devModeEnabled),
    [devModeEnabled],
  );

  React.useEffect(() => {
    setCollapsed(readCollapsed());
    setHydrated(true);
  }, []);

  const toggle = () => {
    setCollapsed((prev) => {
      const next = !prev;
      writeCollapsed(next);
      return next;
    });
  };

  async function onLogout() {
    setLoggingOut(true);
    try {
      await logout();
      toast.success(t("auth.logoutSuccess"));
    } catch {
      /* idempotent */
    } finally {
      router.push("/login");
    }
  }

  // Mobile always uses the expanded 240px width (the 72px collapsed mode
  // is a desktop affordance; in a drawer there's plenty of horizontal
  // room). Desktop follows the persisted `collapsed` preference.
  const width = collapsed && hydrated ? "md:w-[72px]" : "md:w-[240px]";

  return (
    <aside
      className={cn(
        // Eclipse: matte charcoal rail with a moon edge — a resting panel,
        // no drop shadow, no blur. On desktop it's a sticky flex column in
        // the admin layout row; on mobile (<md) it slides in from the left
        // over a flat scrim driven by <MobileDrawerProvider> (elevation is
        // added by the drawer state below).
        "flex flex-col overflow-hidden rounded-[24px]",
        "bg-sg-shell border border-sg-border",
        "shadow-sg-edge",
        // Liquid Glass optics — light-aware edge ring + chromatic inner
        // lensing so the rail reads as a bent-light material, not a tinted
        // panel. Blur-free, composes on top of the shell recipe above.
        "",
        // Desktop ≥md: sticky inline flex member.
        "md:relative md:sticky md:top-4 md:self-start md:max-h-[calc(100dvh-2rem)]",
        "md:shrink-0 md:translate-x-0",
        // Mobile drawer is a floating layer while open — only then does the
        // rail earn a drop shadow.
        drawerOpen && "max-md:shadow-sg-4",
        // Spring the collapse/expand width change — springy overshoot curve
        // instead of a flat ease so the rail settles with a liquid feel.
        "md:transition-[width] md:duration-300 md:ease-[cubic-bezier(0.34,1.56,0.64,1)]",
        // Mobile <md: fixed slide-in drawer at 240px.
        "fixed inset-y-2 left-2 z-50 w-[240px] max-h-[calc(100dvh-16px)]",
        "transition-transform duration-200 ease-out",
        drawerOpen ? "translate-x-0" : "-translate-x-[calc(100%+12px)]",
        mobileDrawerHidden && "pointer-events-none",
        width,
      )}
      id="admin-sidebar"
      aria-label={t("nav.dashboard")}
      aria-hidden={mobileDrawerHidden ? true : undefined}
      inert={mobileDrawerHidden ? true : undefined}
    >
      {/* brand + collapse */}
      <div className="flex items-center justify-between gap-2 border-b border-sg-border px-3.5 py-3.5">
        <Link href="/" className="flex min-h-10 items-center gap-2 overflow-hidden">
          <BrandMarkNudge>
            <BrandMark compact={collapsed && hydrated} />
          </BrandMarkNudge>
        </Link>
        <button
          type="button"
          onClick={toggle}
          aria-label={
            collapsed ? t("nav.expandSidebar") : t("nav.collapseSidebar")
          }
          className="inline-flex h-9 w-9 items-center justify-center rounded-sg-sm text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
        >
          {collapsed ? (
            <ChevronsRight className="h-4 w-4" />
          ) : (
            <ChevronsLeft className="h-4 w-4" />
          )}
        </button>
      </div>

      <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto p-2">
        {sections.map((section, sectionIdx) => (
          <div
            key={section.id}
            role="group"
            aria-label={t(section.labelKey)}
            data-testid={`sidebar-section-${section.id}`}
            className="flex flex-col gap-0.5"
          >
            {collapsed && hydrated ? (
              sectionIdx > 0 ? (
                <div
                  aria-hidden
                  className="mx-2 my-1.5 h-px shrink-0 bg-sg-border"
                />
              ) : null
            ) : (
              <div
                className={cn(
                  "px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-sg-ink-3",
                  sectionIdx === 0 ? "pt-1" : "pt-3",
                )}
              >
                {t(section.labelKey)}
              </div>
            )}
            {section.entries.map((entry) => {
              if (entry.kind === "group") {
                return (
                  <SidebarGroup
                    key={entry.id}
                    group={entry}
                    pathname={pathname}
                    collapsed={collapsed && hydrated}
                  />
                );
              }
              return (
                <SidebarItem
                  key={entry.page.href}
                  item={entry.page}
                  pathname={pathname}
                  collapsed={collapsed && hydrated}
                />
              );
            })}
          </div>
        ))}
      </nav>

      {/* user chip + footer */}
      <div className="border-t border-sg-border p-3">
        {collapsed && hydrated ? (
          <div className="flex flex-col items-center gap-1">
            <button
              type="button"
              onClick={() => setChangePasswordOpen(true)}
              aria-label={t("auth.openChangePasswordDialog")}
              className="flex h-9 w-full items-center justify-center rounded-sg-sm text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
              data-testid="change-password-button"
            >
              <KeyRound className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={onLogout}
              aria-label={t("auth.logoutLabel")}
              disabled={loggingOut}
              className="flex h-9 w-full items-center justify-center rounded-sg-sm text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink disabled:opacity-50"
              data-testid="logout-button"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <div className="relative flex h-7 w-7 shrink-0 items-center justify-center">
              {/* Orb stays in-flow so `.presence-orb`'s own position:relative
                  anchors its bloom/pseudo-elements; it fills the 28px box via
                  !h-7 !w-7. The initial floats above as an absolute overlay —
                  do NOT put `absolute` on the orb (globals.css .presence-orb
                  overrides an unprefixed absolute and drops it back in-flow,
                  shoving the initial out of the pearl). */}
              <PresenceOrb size="md" className="!h-7 !w-7" />
              {/* The pearl's disc is always #000 — the initial must stay
                  white in BOTH themes (theme ink would vanish on Paper). */}
              <span
                data-testid="nav-user-initial"
                className="absolute inset-0 flex items-center justify-center text-[11px] font-medium text-white"
              >
                {(user ?? "a").slice(0, 1).toUpperCase()}
              </span>
            </div>
            <div className="min-w-0 flex-1 leading-tight">
              <div
                className="truncate text-xs font-medium text-sg-ink"
                data-testid="nav-user"
              >
                {user ?? "admin"}
              </div>
            </div>
            <button
              type="button"
              onClick={() => setChangePasswordOpen(true)}
              aria-label={t("auth.openChangePasswordDialog")}
              title={t("auth.openChangePasswordDialog")}
              className="inline-flex h-9 w-9 items-center justify-center rounded-sg-sm text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink"
              data-testid="change-password-button"
            >
              <KeyRound className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={onLogout}
              disabled={loggingOut}
              aria-label={t("auth.logoutLabel")}
              className="inline-flex h-9 w-9 items-center justify-center rounded-sg-sm text-sg-ink-3 transition-colors hover:bg-sg-inset-hover hover:text-sg-ink disabled:opacity-50"
              data-testid="logout-button"
            >
              <LogOut className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>
      <ChangePasswordDialog
        open={changePasswordOpen}
        onOpenChange={setChangePasswordOpen}
      />
    </aside>
  );
}

/**
 * Single leaf entry. Extracted so group children can reuse the same visual
 * treatment as top-level items.
 */
function SidebarItem({
  item,
  pathname,
  collapsed,
  nested = false,
  onRef,
  onKeyDown,
}: {
  item: NavPageDef;
  pathname: string;
  collapsed: boolean;
  nested?: boolean;
  onRef?: (el: HTMLAnchorElement | null) => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLAnchorElement>) => void;
}) {
  const { t } = useTranslation();
  const active = isActiveHref(pathname, item.href);
  const Icon = item.icon;
  const label = t(item.labelKey);
  return (
    <Link
      ref={onRef}
      href={item.href as never}
      onKeyDown={onKeyDown}
      className={cn(
        "group relative flex min-h-9 items-center gap-2.5 rounded-sg-md px-2.5 py-1.5 text-[13px] transition-colors",
        // Springy press physics on tap ( composes its own transform
        // transition; transition-colors above keeps the hue change).
        "",
        // Active: the Eclipse selected treatment (white 7% + moon edge +
        // inset tint glow). Inactive: text lift + sunken hover well.
        active
          ? "nav-active border text-sg-ink"
          : "border border-transparent text-sg-ink-2 hover:bg-sg-inset-hover hover:text-sg-ink",
        collapsed && "justify-center px-0",
        nested && !collapsed && "pl-8",
      )}
      aria-current={active ? "page" : undefined}
      title={collapsed ? label : undefined}
    >
      {active ? (
        <motion.span
          layoutId="sidebar-indicator"
          aria-hidden
          className="absolute left-[-6px] top-1/2 h-3.5 w-[3px] -translate-y-1/2 rounded-[2px] bg-sg-tint shadow-[var(--sg-bloom-1)]"
          transition={springs.snappy}
        />
      ) : (
        // Dim accent tick that appears on hover only — previews the active
        // indicator without the layoutId dance (kept separate so it doesn't
        // fight the animated bar when the user hovers a sibling).
        <span
          aria-hidden
          className={cn(
            "pointer-events-none absolute left-[-6px] top-1/2 h-3 w-[2px] -translate-y-1/2 rounded-[2px] bg-sg-tint",
            "opacity-0 transition-opacity duration-150 group-hover:opacity-60",
          )}
        />
      )}
      <Icon className="h-[14px] w-[14px] shrink-0 opacity-80" />
      {collapsed ? null : <span className="truncate">{label}</span>}
    </Link>
  );
}

/**
 * Collapsible group. Defaults to collapsed; auto-expands when the current
 * route matches one of its children. Keyboard:
 *   - Enter / Space on the toggle flips expanded.
 *   - ArrowDown on the toggle moves focus to the first child.
 *   - ArrowUp on the first child returns focus to the toggle.
 */
function SidebarGroup({
  group,
  pathname,
  collapsed,
}: {
  group: SidebarGroupEntry;
  pathname: string;
  collapsed: boolean;
}) {
  const { t } = useTranslation();
  const hasActiveChild = group.children.some((c) =>
    isActiveHref(pathname, c.href),
  );
  const [expanded, setExpanded] = React.useState<boolean>(hasActiveChild);

  // Auto-expand whenever the current route matches a child. Closing stays
  // user-driven — we don't force collapse when the route navigates away.
  React.useEffect(() => {
    if (hasActiveChild) setExpanded(true);
  }, [hasActiveChild]);

  const toggleRef = React.useRef<HTMLButtonElement | null>(null);
  const childRefs = React.useRef<Array<HTMLAnchorElement | null>>([]);

  const Icon = group.icon;
  const label = t(group.labelKey);

  const onToggleKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setExpanded((v) => !v);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!expanded) setExpanded(true);
      // Focus is deferred so the child list has a chance to mount.
      requestAnimationFrame(() => childRefs.current[0]?.focus());
    }
  };

  const onChildKeyDown = (
    e: React.KeyboardEvent<HTMLAnchorElement>,
    idx: number,
  ) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      childRefs.current[idx + 1]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (idx === 0) {
        toggleRef.current?.focus();
      } else {
        childRefs.current[idx - 1]?.focus();
      }
    }
  };

  // Collapsed rail: render children as flat icon entries so every channel
  // remains one click away.
  if (collapsed) {
    return (
      <>
        {group.children.map((child) => (
          <SidebarItem
            key={child.href}
            item={child}
            pathname={pathname}
            collapsed
          />
        ))}
      </>
    );
  }

  return (
    <div
      role="group"
      aria-label={label}
      data-testid={`sidebar-group-${group.id}`}
    >
      <button
        ref={toggleRef}
        type="button"
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={onToggleKeyDown}
        aria-expanded={expanded}
        aria-controls={`sidebar-group-${group.id}-list`}
        aria-label={label}
        data-testid={`sidebar-group-toggle-${group.id}`}
        className={cn(
          "relative flex min-h-9 w-full items-center gap-2.5 rounded-sg-md border border-transparent px-2.5 py-1.5 text-[13px] transition-colors",
          // Springy press physics on tap, matching SidebarItem.
          "",
          // Active child lifts the label to medium weight; inactive groups get
          // a sunken hover well, matching SidebarItem.
          hasActiveChild
            ? "font-medium text-sg-ink"
            : "text-sg-ink-2 hover:bg-sg-inset-hover hover:text-sg-ink",
        )}
      >
        <Icon className="h-[14px] w-[14px] shrink-0 opacity-80" />
        <span className="truncate">{label}</span>
        <motion.span
          aria-hidden
          className="ml-auto inline-flex"
          animate={{ rotate: expanded ? 90 : 0 }}
          transition={{ duration: 0.15, ease: "easeOut" }}
        >
          <ChevronRight className="h-3 w-3 text-sg-ink-3" />
        </motion.span>
      </button>
      {expanded ? (
        <ul
          id={`sidebar-group-${group.id}-list`}
          className="mt-0.5 flex flex-col gap-0.5"
          role="list"
        >
          {group.children.map((child, idx) => (
            <li key={child.href}>
              <SidebarItem
                item={child}
                pathname={pathname}
                collapsed={false}
                nested
                onRef={(el) => {
                  childRefs.current[idx] = el;
                }}
                onKeyDown={(e) => onChildKeyDown(e, idx)}
              />
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * Plays a 1° rotate + 2% scale nudge on the brand-mark whenever the route
 * changes. Visually tiny but signals "you moved" without competing with the
 * page-transition itself. Disabled under `prefers-reduced-motion`.
 */
function BrandMarkNudge({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const { reduced } = useMotion();
  // Monotonically increasing key drives the animate prop via the pathname.
  // `initial={false}` prevents a nudge on first mount.
  const animate = reduced
    ? { rotate: 0, scale: 1 }
    : { rotate: [0, 1, 0], scale: [1, 1.02, 1] };
  return (
    <motion.span
      key={pathname}
      className="inline-flex origin-center"
      initial={false}
      animate={animate}
      transition={{ duration: 0.3, ease: [0.34, 1.56, 0.64, 1] }}
    >
      {children}
    </motion.span>
  );
}
