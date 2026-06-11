"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import {
  AtSign,
  Beaker,
  Bot,
  Boxes,
  Building2,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  ClipboardCheck,
  Database,
  FileTerminal,
  Fingerprint,
  GitFork,
  Hash,
  KeyRound,
  Leaf,
  LogOut,
  MessageCircle,
  MessageSquare,
  MessageSquareText,
  MessagesSquare,
  MonitorCog,
  Network,
  Newspaper,
  Plug,
  Radio,
  Route,
  Send,
  Settings,
  Sparkles,
  Store,
  Terminal,
  Timer,
  Users,
  Wrench,
  Zap,
} from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { logout } from "@/lib/auth";
import { springs } from "@/lib/motion";
import { useDevMode } from "@/lib/dev-mode";
import { useMotion } from "@/components/ui/motion-safe";
import { useMobileDrawer } from "./mobile-drawer-context";
import { BrandMark } from "./brand-mark";
import { ChangePasswordDialog } from "./change-password-dialog";

interface NavItem {
  kind?: "item";
  href: string;
  labelKey: string;
  icon: React.ComponentType<{ className?: string }>;
  /**
   * When `true`, the item is hidden in the default Operator sidebar and only
   * appears when `useDevMode().enabled === true`. Power-user pages — raw
   * config TOML, tenants, credentials, agents, skills, plugins, hooks, RAG,
   * profiles, nodes, evolution — set this flag so a fresh install isn't
   * drowning the operator in 25+ entries.
   */
  isDeveloper?: boolean;
}

interface NavGroup {
  kind: "group";
  /** Stable id (used for local-storage + keyboard nav). */
  id: string;
  labelKey: string;
  icon: React.ComponentType<{ className?: string }>;
  children: NavItem[];
  /** Mirrors `NavItem.isDeveloper` for the whole group. */
  isDeveloper?: boolean;
}

type NavEntry = NavItem | NavGroup;

/**
 * Visible-by-default Operator surface. 9 page entries (with Channels as a
 * collapsible group exposing 7 channel leaves — QQ, Telegram, Discord,
 * Slack, Feishu, WeChat-Official, QQ-Official) + the always-visible
 * Developer Settings link at the bottom — 10 sidebar rows total.
 *
 * Order: top-down by frequency of use during a normal operator shift
 * (chat → approvals → audit) then configuration (providers / models /
 * channels / scheduler / identity).
 */
const OPERATOR_ITEMS: NavEntry[] = [
  { href: "/chat", labelKey: "nav.chat", icon: MessageSquareText },
  { href: "/playground", labelKey: "nav.playground", icon: Beaker },
  { href: "/approvals", labelKey: "nav.approvals", icon: ClipboardCheck },
  { href: "/sessions", labelKey: "nav.sessions", icon: MessagesSquare },
  { href: "/logs", labelKey: "nav.logs", icon: FileTerminal },
  // W2.2 — live activity panel for background sub-agents. Operational
  // adjacency to logs (auditing the in-flight surface) sits it above
  // credentials.
  { href: "/subagents", labelKey: "subagents.sidebarLabel", icon: GitFork },
  { href: "/credentials", labelKey: "nav.credentials", icon: KeyRound },
  { href: "/models", labelKey: "nav.models", icon: Route },
  // Persona — humanlike-mode operator surface; sits between Models and
  // Scheduler because it's a chat-personality knob that pairs with model
  // configuration, not a developer concern.
  { href: "/persona", labelKey: "nav.persona", icon: Sparkles },
  // Marketplace — unified browse + install hub for skills, MCP servers, and
  // plugins (all GitHub-backed). Operator-facing discovery surface, so it
  // lives in the default sidebar rather than behind dev-mode.
  { href: "/marketplace", labelKey: "nav.marketplace", icon: Store },
  {
    kind: "group",
    id: "channels",
    labelKey: "nav.channels",
    icon: Radio,
    children: [
      {
        href: "/channels/qq",
        labelKey: "nav.channelQq",
        icon: MessageCircle,
      },
      {
        href: "/channels/telegram",
        labelKey: "nav.channelTelegram",
        icon: Send,
      },
      {
        href: "/channels/discord",
        labelKey: "nav.channelDiscord",
        icon: Hash,
      },
      {
        href: "/channels/slack",
        labelKey: "nav.channelSlack",
        icon: AtSign,
      },
      {
        href: "/channels/feishu",
        labelKey: "nav.channelFeishu",
        icon: MessageSquareText,
      },
      {
        href: "/channels/wechat_official",
        labelKey: "nav.channelWechatOfficial",
        icon: MessageSquare,
      },
      {
        href: "/channels/qq_official",
        labelKey: "nav.channelQqOfficial",
        icon: Bot,
      },
    ],
  },
  { href: "/scheduler", labelKey: "nav.scheduler", icon: Timer },
  // QZone daily-publishing scheduler surface — operator-facing companion to
  // the persona life layer (a persona's daily 说说 pipeline), so it sits
  // right under the generic Scheduler entry.
  {
    href: "/scheduler/qzone",
    labelKey: "nav.schedulerQzone",
    icon: Newspaper,
  },
  { href: "/identity", labelKey: "nav.identity", icon: Fingerprint },
];

/**
 * Developer-only entries — hidden until `useDevMode().enabled === true`.
 * Always reachable through `/admin/dev-settings` regardless of the flag.
 */
const DEV_ITEMS: NavEntry[] = [
  { href: "/config", labelKey: "nav.config", icon: Settings, isDeveloper: true },
  { href: "/tenants", labelKey: "nav.tenants", icon: Building2, isDeveloper: true },
  { href: "/agents", labelKey: "nav.agents", icon: Bot, isDeveloper: true },
  { href: "/skills", labelKey: "nav.skills", icon: Wrench, isDeveloper: true },
  { href: "/plugins", labelKey: "nav.plugins", icon: Boxes, isDeveloper: true },
  {
    href: "/marketplace/acceleration",
    labelKey: "nav.marketplaceAcceleration",
    icon: Zap,
    isDeveloper: true,
  },
  {
    href: "/marketplace/contribute",
    labelKey: "nav.marketplaceContribute",
    icon: GitFork,
    isDeveloper: true,
  },
  { href: "/hooks", labelKey: "nav.hooks", icon: Zap, isDeveloper: true },
  { href: "/rag", labelKey: "nav.rag", icon: Database, isDeveloper: true },
  { href: "/profiles", labelKey: "nav.profiles", icon: Users, isDeveloper: true },
  { href: "/nodes", labelKey: "nav.nodes", icon: Network, isDeveloper: true },
  { href: "/evolution", labelKey: "nav.evolution", icon: Leaf, isDeveloper: true },
];

/**
 * Always-visible "Updates" entry — /admin/system surfaces version info,
 * update banner, and copy-paste upgrade commands (W2.1). The URL keeps the
 * `/system` slug for back-compat with existing routes/bookmarks; only the
 * sidebar label is renamed to "更新 / Updates" to make it crystal clear
 * this is the version-update surface, not a generic settings page (W3
 * first-run-wizard polish). Placed alongside the dev-settings footer link
 * so it's reachable in operator mode too.
 */
const SYSTEM_ENTRY: NavItem = {
  href: "/system",
  labelKey: "sidebar.updatesLabel",
  icon: MonitorCog,
};

/** Always-visible footer link to the dev-settings dashboard. */
const DEV_SETTINGS_ENTRY: NavItem = {
  href: "/dev-settings",
  labelKey: "nav.devSettings",
  icon: Terminal,
};

/**
 * Returns the ordered sidebar entries for the current dev-mode state.
 * Operator mode = OPERATOR_ITEMS + dev-settings link; dev mode appends the
 * full DEV_ITEMS list above the dev-settings link.
 */
export function resolveSidebarEntries(devModeEnabled: boolean): NavEntry[] {
  if (devModeEnabled) {
    return [...OPERATOR_ITEMS, ...DEV_ITEMS, SYSTEM_ENTRY, DEV_SETTINGS_ENTRY];
  }
  return [...OPERATOR_ITEMS, SYSTEM_ENTRY, DEV_SETTINGS_ENTRY];
}

/** Exposed for tests + the dev-settings dashboard's card grid. */
export const SIDEBAR_OPERATOR_ITEMS = OPERATOR_ITEMS;
export const SIDEBAR_DEV_ITEMS = DEV_ITEMS;
export const SIDEBAR_SYSTEM_ENTRY = SYSTEM_ENTRY;
export const SIDEBAR_DEV_SETTINGS_ENTRY = DEV_SETTINGS_ENTRY;

/** Every navigable href in the rail — used for longest-match arbitration. */
const ALL_NAV_HREFS: string[] = [
  ...OPERATOR_ITEMS.flatMap((i) => ("children" in i ? i.children.map((c) => c.href) : [i.href])),
  ...DEV_ITEMS.flatMap((i) => ("children" in i ? i.children.map((c) => c.href) : [i.href])),
  SYSTEM_ENTRY.href,
  DEV_SETTINGS_ENTRY.href,
];

function matchesHref(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * Active = this href matches AND no other nav item matches more
 * specifically — so /scheduler/qzone lights only "QZone 发布", never
 * its /scheduler sibling as well.
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
  const entries = React.useMemo(
    () => resolveSidebarEntries(devModeEnabled),
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
        // Spatial Glass: floating glass rail (shell tier — real blur allowed).
        // On desktop it's a sticky flex column in the admin layout row; on
        // mobile (<md) it slides in from the left over a backdrop driven by
        // <MobileDrawerProvider>.
        "flex flex-col overflow-hidden rounded-[24px]",
        "bg-sg-shell border border-sg-border",
        "backdrop-blur-sg-shell backdrop-saturate-sg-shell",
        "shadow-sg-3",
        // Liquid Glass optics — light-aware edge ring + chromatic inner
        // lensing so the rail reads as a bent-light material, not a tinted
        // panel. Blur-free, composes on top of the shell recipe above.
        "lg-edge lg-refract",
        // Desktop ≥md: sticky inline flex member.
        "md:relative md:sticky md:top-4 md:self-start md:max-h-[calc(100dvh-2rem)]",
        "md:shrink-0 md:translate-x-0",
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
        {entries.map((entry) => {
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
              key={entry.href}
              item={entry}
              pathname={pathname}
              collapsed={collapsed && hydrated}
            />
          );
        })}
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
            <div
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold text-primary-foreground"
              style={{
                background:
                  "linear-gradient(135deg, var(--sg-accent), var(--sg-accent-2))",
                boxShadow: "0 0 10px -3px var(--sg-accent-glow)",
              }}
            >
              {(user ?? "a").slice(0, 1).toUpperCase()}
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
  item: NavItem;
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
        // Springy press physics on tap (lg-gel composes its own transform
        // transition; transition-colors above keeps the hue change).
        "lg-gel",
        // Active: full accent-tinted glass pill with a hairline accent border.
        // Inactive: text lift + sunken hover well.
        active
          ? "border border-sg-accent/30 bg-sg-accent-soft text-sg-ink"
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
          className="absolute left-[-6px] top-1/2 h-3.5 w-[3px] -translate-y-1/2 rounded-[2px] bg-sg-accent shadow-sg-glow"
          transition={springs.snappy}
        />
      ) : (
        // Dim accent tick that appears on hover only — previews the active
        // indicator without the layoutId dance (kept separate so it doesn't
        // fight the animated bar when the user hovers a sibling).
        <span
          aria-hidden
          className={cn(
            "pointer-events-none absolute left-[-6px] top-1/2 h-3 w-[2px] -translate-y-1/2 rounded-[2px] bg-sg-accent",
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
  group: NavGroup;
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
          "lg-gel",
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
