/**
 * Navigation registry — the single source of truth for the admin page
 * inventory.
 *
 * Before this module existed the page list was duplicated (and drifting)
 * across four places:
 *
 *   1. `components/layout/sidebar.tsx`   — OPERATOR_ITEMS / DEV_ITEMS
 *   2. `components/cmdk-palette.tsx`     — NAV_CMDS
 *   3. `app/(admin)/dev-settings/page.tsx` — DEV_PAGE_KEYS / ROUTE_FOR_KEY
 *   4. `components/layout/breadcrumbs.tsx` — SEGMENT_KEY
 *
 * They now all derive from the ONE `NAV_PAGES` array below through four
 * views: `sidebarSections()`, `commandEntries()`, `devSettingsPages()` and
 * `segmentLabelKey()`. Adding a page = adding one entry here (plus its
 * `nav.*` label in BOTH locale bundles); every surface picks it up.
 *
 * Registry truth = the union of the four legacy lists. Pages that existed
 * in only some surfaces express that membership via fields on the def
 * (`section === undefined` → not in the sidebar, `developer` → dev-mode
 * gated everywhere, `devSettingsCard` → discovery-grid membership).
 */

import type { Route } from "next";
import type { LucideIcon } from "@/components/icons";
import {
  Activity,
  AtSign,
  Beaker,
  Bot,
  Boxes,
  Building2,
  ClipboardCheck,
  Database,
  FileTerminal,
  Fingerprint,
  GitFork,
  Hash,
  KeyRound,
  Leaf,
  MessageCircle,
  MessageSquare,
  MessageSquareText,
  MessagesSquare,
  MonitorCog,
  Network,
  Plug,
  Radio,
  Route as RouteIcon,
  Send,
  Settings,
  Sparkles,
  Store,
  Terminal,
  Timer,
  Users,
  Wrench,
  Zap,
} from "@/components/icons";

/* ------------------------------------------------------------------ */
/*                              Types                                 */
/* ------------------------------------------------------------------ */

/** Sidebar section ids, in render order. */
export type NavSectionId = "chat" | "ops" | "config" | "system" | "developer";

export interface NavPageDef {
  /** Stable id — also the `devSettings.pages.<id>.*` i18n key for cards. */
  id: string;
  /** typedRoutes-checked route — `next build` fails on a bad href. */
  href: Route;
  /** Existing i18n key for the page label (reused, never renamed). */
  labelKey: string;
  icon: LucideIcon;
  /**
   * Sidebar section. `undefined` = the page is not rendered in the sidebar
   * (only the Dashboard, whose entry point is the brand link).
   */
  section?: NavSectionId;
  /** Dev-mode gated: hidden from sidebar + palette unless dev mode is on. */
  developer?: boolean;
  /** Collapsible sidebar group this page belongs to (channels). */
  groupId?: string;
  /** Extra cmdk search terms (ported from the legacy NAV_CMDS list). */
  keywords?: string;
  /** Member of the /dev-settings discovery card grid. */
  devSettingsCard?: boolean;
}

/** Collapsible sidebar group (channels is the only one today). */
export interface NavGroupDef {
  id: string;
  labelKey: string;
  icon: LucideIcon;
}

/**
 * Palette-only alias — keeps muscle memory for routes that were merged
 * away (PR4 folded /providers + /credentials into /models). Not part of
 * `NAV_PAGES` so id/href uniqueness of real pages stays intact.
 */
export interface NavAliasDef {
  id: string;
  labelKey: string;
  href: Route;
  icon: LucideIcon;
  keywords?: string;
}

/* ------------------------------------------------------------------ */
/*                          The inventory                             */
/* ------------------------------------------------------------------ */

export const NAV_GROUPS: NavGroupDef[] = [
  { id: "channels", labelKey: "nav.channels", icon: Radio },
];

export const NAV_PAGES: NavPageDef[] = [
  // ── Dashboard (palette + breadcrumbs only — brand link covers the rail) ──
  {
    id: "dashboard",
    href: "/",
    labelKey: "nav.dashboard",
    icon: Activity,
    keywords: "overview home 仪表盘 dashboard",
  },

  // ── 对话 / Chat ────────────────────────────────────────────────────
  {
    id: "chat",
    href: "/chat",
    labelKey: "nav.chat",
    icon: MessageSquareText,
    section: "chat",
    keywords: "chat conversation talk 聊天 对话",
  },
  {
    id: "playground",
    href: "/playground",
    labelKey: "nav.playground",
    icon: Beaker,
    section: "chat",
    keywords: "playground prompt test 试验",
  },
  {
    id: "sessions",
    href: "/sessions",
    labelKey: "nav.sessions",
    icon: MessagesSquare,
    section: "chat",
    keywords: "sessions history turns 会话",
  },

  // ── 运营 / Operations ─────────────────────────────────────────────
  {
    id: "approvals",
    href: "/approvals",
    labelKey: "nav.approvals",
    icon: ClipboardCheck,
    section: "ops",
    keywords: "pending tool gate 审批",
  },
  {
    id: "logs",
    href: "/logs",
    labelKey: "nav.logs",
    icon: FileTerminal,
    section: "ops",
    keywords: "stream events trace 日志",
  },
  {
    id: "subagents",
    href: "/subagents",
    labelKey: "subagents.sidebarLabel",
    icon: GitFork,
    section: "ops",
    keywords: "subagents background tasks 子代理",
  },
  {
    id: "scheduler",
    href: "/scheduler",
    labelKey: "nav.scheduler",
    icon: Timer,
    section: "ops",
    keywords: "cron jobs 定时任务",
  },
  // NOTE: QZone publishing lives inside the QQ channel page now (it
  // borrows the NapCat login state); /scheduler/qzone redirects there.

  // ── 配置 / Configuration (models FIRST — PR4 canonical hub) ───────
  {
    id: "models",
    href: "/models",
    labelKey: "nav.models",
    icon: RouteIcon,
    section: "config",
    keywords: "providers aliases routing 模型",
  },
  {
    id: "persona",
    href: "/persona",
    labelKey: "nav.persona",
    icon: Sparkles,
    section: "config",
    keywords: "persona humanlike chat personality 拟人化 角色 grantley",
  },
  {
    id: "marketplace",
    href: "/marketplace",
    labelKey: "nav.marketplace",
    icon: Store,
    section: "config",
    keywords: "marketplace install skills mcp plugins 市场",
  },
  {
    id: "qq",
    href: "/channels/qq",
    labelKey: "nav.channelQq",
    icon: MessageCircle,
    section: "config",
    groupId: "channels",
    keywords: "channels messaging 通道 qq qzone publish 说说 空间",
  },
  {
    id: "telegram",
    href: "/channels/telegram",
    labelKey: "nav.channelTelegram",
    icon: Send,
    section: "config",
    groupId: "channels",
    keywords: "telegram channel 电报",
  },
  {
    id: "discord",
    href: "/channels/discord",
    labelKey: "nav.channelDiscord",
    icon: Hash,
    section: "config",
    groupId: "channels",
    keywords: "discord channel",
  },
  {
    id: "slack",
    href: "/channels/slack",
    labelKey: "nav.channelSlack",
    icon: AtSign,
    section: "config",
    groupId: "channels",
    keywords: "slack channel",
  },
  {
    id: "feishu",
    href: "/channels/feishu",
    labelKey: "nav.channelFeishu",
    icon: MessageSquareText,
    section: "config",
    groupId: "channels",
    keywords: "feishu lark channel 飞书",
  },
  {
    id: "wechat_official",
    href: "/channels/wechat_official",
    labelKey: "nav.channelWechatOfficial",
    icon: MessageSquare,
    section: "config",
    groupId: "channels",
    keywords: "wechat official account channel 微信公众号",
  },
  {
    id: "qq_official",
    href: "/channels/qq_official",
    labelKey: "nav.channelQqOfficial",
    icon: Bot,
    section: "config",
    groupId: "channels",
    keywords: "qq official bot channel 官方",
  },
  {
    id: "identity",
    href: "/identity",
    labelKey: "nav.identity",
    icon: Fingerprint,
    section: "config",
    keywords: "identity account name 身份",
  },

  // ── 系统 / System ─────────────────────────────────────────────────
  {
    id: "system",
    href: "/system",
    labelKey: "sidebar.updatesLabel",
    icon: MonitorCog,
    section: "system",
    keywords: "system updates upgrade version 更新 系统",
  },
  {
    id: "devSettings",
    href: "/dev-settings",
    labelKey: "nav.devSettings",
    icon: Terminal,
    section: "system",
    keywords: "developer settings advanced 开发者设置",
  },

  // ── 开发者 / Developer (dev-mode gated; order mirrors legacy DEV_ITEMS) ──
  {
    id: "config",
    href: "/config",
    labelKey: "nav.config",
    icon: Settings,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "toml settings 配置",
  },
  {
    id: "tenants",
    href: "/tenants",
    labelKey: "nav.tenants",
    icon: Building2,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "tenants multi-tenant quotas 租户",
  },
  {
    id: "agents",
    href: "/agents",
    labelKey: "nav.agents",
    icon: Bot,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "prompt editor agent",
  },
  {
    id: "skills",
    href: "/skills",
    labelKey: "nav.skills",
    icon: Wrench,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "skills gallery 技能",
  },
  {
    id: "plugins",
    href: "/plugins",
    labelKey: "nav.plugins",
    icon: Boxes,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "tools manifest 插件",
  },
  {
    id: "marketplaceAcceleration",
    href: "/marketplace/acceleration",
    labelKey: "nav.marketplaceAcceleration",
    icon: Zap,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "marketplace acceleration mirror github 加速",
  },
  {
    id: "marketplaceContribute",
    href: "/marketplace/contribute",
    labelKey: "nav.marketplaceContribute",
    icon: GitFork,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "marketplace contribute submit 贡献",
  },
  {
    id: "hooks",
    href: "/hooks",
    labelKey: "nav.hooks",
    icon: Zap,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "hooks events monitor",
  },
  {
    id: "rag",
    href: "/rag",
    labelKey: "nav.rag",
    icon: Database,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "retrieval chunks embeddings 向量",
  },
  {
    id: "profiles",
    href: "/profiles",
    labelKey: "nav.profiles",
    icon: Users,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "profiles preferences defaults",
  },
  {
    id: "nodes",
    href: "/nodes",
    labelKey: "nav.nodes",
    icon: Network,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "nodes topology distributed 节点",
  },
  {
    id: "evolution",
    href: "/evolution",
    labelKey: "nav.evolution",
    icon: Leaf,
    section: "developer",
    developer: true,
    devSettingsCard: true,
    keywords: "evolution proposals self-improve 演化 自我改进",
  },
];

/**
 * Legacy-route palette aliases. `/providers` + `/credentials` became
 * redirect stubs into `/models` (PR4) — these entries keep the old labels
 * and search keywords working in the palette, landing on the hub.
 */
export const NAV_ALIASES: NavAliasDef[] = [
  {
    id: "nav.providers",
    labelKey: "nav.providers",
    href: "/models",
    icon: Plug,
    keywords: "providers llm openai",
  },
  {
    id: "nav.credentials",
    labelKey: "nav.credentials",
    href: "/models",
    icon: KeyRound,
    keywords: "credentials api keys tokens secrets 凭证 密钥",
  },
];

/* ------------------------------------------------------------------ */
/*                       View 1 — sidebar                             */
/* ------------------------------------------------------------------ */

export type SidebarEntry =
  | { kind: "item"; page: NavPageDef }
  | {
      kind: "group";
      id: string;
      labelKey: string;
      icon: LucideIcon;
      children: NavPageDef[];
    };

export interface NavSection {
  id: NavSectionId;
  /** `nav.sections.*` i18n key for the uppercase section header. */
  labelKey: string;
  entries: SidebarEntry[];
}

const SECTION_ORDER: readonly NavSectionId[] = [
  "chat",
  "ops",
  "config",
  "system",
  "developer",
];

export const SECTION_LABEL_KEYS: Record<NavSectionId, string> = {
  chat: "nav.sections.chat",
  ops: "nav.sections.ops",
  config: "nav.sections.config",
  system: "nav.sections.system",
  developer: "nav.sections.developer",
};

/**
 * Ordered sidebar sections for the current dev-mode state. Grouped pages
 * (channels) collapse into a single group entry at the position of their
 * first member.
 */
export function sidebarSections(devMode: boolean): NavSection[] {
  const sections: NavSection[] = [];
  for (const sectionId of SECTION_ORDER) {
    if (sectionId === "developer" && !devMode) continue;
    const entries: SidebarEntry[] = [];
    const emittedGroups = new Set<string>();
    for (const page of NAV_PAGES) {
      if (page.section !== sectionId) continue;
      if (page.groupId) {
        if (emittedGroups.has(page.groupId)) continue;
        emittedGroups.add(page.groupId);
        const group = NAV_GROUPS.find((g) => g.id === page.groupId);
        if (!group) continue;
        entries.push({
          kind: "group",
          id: group.id,
          labelKey: group.labelKey,
          icon: group.icon,
          children: NAV_PAGES.filter((p) => p.groupId === group.id),
        });
      } else {
        entries.push({ kind: "item", page });
      }
    }
    if (entries.length > 0) {
      sections.push({
        id: sectionId,
        labelKey: SECTION_LABEL_KEYS[sectionId],
        entries,
      });
    }
  }
  return sections;
}

/** Every navigable page href — used for longest-match active arbitration. */
export function navHrefs(): string[] {
  return NAV_PAGES.map((p) => p.href as string);
}

/* ------------------------------------------------------------------ */
/*                       View 2 — command palette                     */
/* ------------------------------------------------------------------ */

export interface CommandEntry {
  id: string;
  labelKey: string;
  href: Route;
  icon: LucideIcon;
  keywords?: string;
}

/**
 * Palette navigation entries. Developer pages are included only when dev
 * mode is on (matching the sidebar gate). Legacy `/providers` +
 * `/credentials` aliases are always appended.
 *
 * Ids are `nav.<page.id>` so recents persisted by the legacy hardcoded
 * list (`nav.dashboard`, `nav.models`, `nav.qq`, …) keep resolving.
 */
export function commandEntries(devMode: boolean): CommandEntry[] {
  const pages = NAV_PAGES.filter((p) => devMode || !p.developer);
  return [
    ...pages.map((p) => ({
      id: `nav.${p.id}`,
      labelKey: p.labelKey,
      href: p.href,
      icon: p.icon,
      keywords: p.keywords,
    })),
    ...NAV_ALIASES.map((a) => ({
      id: a.id,
      labelKey: a.labelKey,
      href: a.href,
      icon: a.icon,
      keywords: a.keywords,
    })),
  ];
}

/* ------------------------------------------------------------------ */
/*                       View 3 — dev-settings grid                   */
/* ------------------------------------------------------------------ */

/**
 * Pages shown as discovery cards on `/dev-settings`. Exactly the
 * developer-gated registry pages that opted into the grid — the card's
 * title/description live at `devSettings.pages.<id>.*`.
 */
export function devSettingsPages(): NavPageDef[] {
  return NAV_PAGES.filter((p) => p.devSettingsCard === true);
}

/* ------------------------------------------------------------------ */
/*                       View 4 — breadcrumbs                         */
/* ------------------------------------------------------------------ */

/**
 * Non-page URL segments the breadcrumb still needs labels for: detail
 * sub-routes, the /account/security pages (not in any nav surface), and
 * the legacy /providers + /credentials redirect stubs.
 */
const EXTRA_SEGMENT_KEYS: Record<string, string> = {
  detail: "breadcrumbs.detail",
  account: "breadcrumbs.account",
  security: "breadcrumbs.security",
  providers: "breadcrumbs.providers",
  credentials: "breadcrumbs.credentials",
};

const SEGMENT_KEYS: Record<string, string> = (() => {
  const map: Record<string, string> = {};
  // Group container segments first (e.g. /channels has no page of its own).
  for (const group of NAV_GROUPS) map[group.id] = group.labelKey;
  // Each page labels the last segment of its href; parent segments are
  // covered by the parent page (/scheduler for /scheduler/qzone) or a
  // group above.
  for (const page of NAV_PAGES) {
    const segments = (page.href as string).split("/").filter(Boolean);
    const last = segments[segments.length - 1];
    if (last) map[last] = page.labelKey;
  }
  return { ...map, ...EXTRA_SEGMENT_KEYS };
})();

/** i18n key for a breadcrumb URL segment, or undefined for raw display. */
export function segmentLabelKey(segment: string): string | undefined {
  return SEGMENT_KEYS[segment];
}
