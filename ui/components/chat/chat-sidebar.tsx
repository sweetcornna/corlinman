"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { motion, useReducedMotion } from "framer-motion";
import {
  Archive,
  ArchiveRestore,
  ChevronLeft,
  ChevronRight,
  MessageSquarePlus,
  Pencil,
  Pin,
  PinOff,
  Search,
  Trash2,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { springs } from "@/lib/motion";
import type { ChatConversation } from "@/lib/chat/types";

interface ChatSidebarProps {
  conversations: ChatConversation[];
  activeSessionKey: string | null;
  onNew: () => void;
  onRename: (sessionKey: string, title: string) => void;
  onTogglePin: (sessionKey: string) => void;
  onToggleArchive: (sessionKey: string) => void;
  onDelete: (sessionKey: string) => void;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
}

interface GroupedConversation {
  labelKey: string;
  rows: ChatConversation[];
}

const MS_PER_DAY = 86_400_000;

function groupConversations(
  conversations: ChatConversation[],
  now: number,
): GroupedConversation[] {
  const pinned: ChatConversation[] = [];
  const today: ChatConversation[] = [];
  const yesterday: ChatConversation[] = [];
  const week: ChatConversation[] = [];
  const month: ChatConversation[] = [];
  const older: ChatConversation[] = [];
  const archived: ChatConversation[] = [];

  for (const c of conversations) {
    if (c.archived) {
      archived.push(c);
      continue;
    }
    if (c.pinned) {
      pinned.push(c);
      continue;
    }
    const ageDays = (now - c.lastMessageAt) / MS_PER_DAY;
    if (ageDays < 1) today.push(c);
    else if (ageDays < 2) yesterday.push(c);
    else if (ageDays < 7) week.push(c);
    else if (ageDays < 30) month.push(c);
    else older.push(c);
  }

  return [
    { labelKey: "chat.groupPinned", rows: pinned },
    { labelKey: "chat.groupToday", rows: today },
    { labelKey: "chat.groupYesterday", rows: yesterday },
    { labelKey: "chat.group7Days", rows: week },
    { labelKey: "chat.group30Days", rows: month },
    { labelKey: "chat.groupOlder", rows: older },
    { labelKey: "chat.groupArchived", rows: archived },
  ].filter((g) => g.rows.length > 0);
}

function fuzzyMatch(needle: string, hay: string): boolean {
  if (!needle) return true;
  const n = needle.toLowerCase();
  const h = hay.toLowerCase();
  let i = 0;
  for (const ch of h) {
    if (ch === n[i]) i += 1;
    if (i === n.length) return true;
  }
  return false;
}

export function ChatSidebar({
  conversations,
  activeSessionKey,
  onNew,
  onRename,
  onTogglePin,
  onToggleArchive,
  onDelete,
  collapsed,
  onToggleCollapsed,
}: ChatSidebarProps) {
  const { t } = useTranslation();
  const [query, setQuery] = React.useState("");
  const [renamingKey, setRenamingKey] = React.useState<string | null>(null);
  const [renameValue, setRenameValue] = React.useState("");

  // Hide legacy rows whose session_key is empty — they were created
  // before v1.8.5 fixed session_key threading and aren't individually
  // resumable (they all collapsed into a single aggregate row). They
  // still live in the journal for audit; the sidebar just stops trying
  // to route to them.
  const navigable = React.useMemo(
    () => conversations.filter((c) => c.sessionKey.trim().length > 0),
    [conversations],
  );
  const filtered = React.useMemo(() => {
    if (!query) return navigable;
    return navigable.filter((c) =>
      fuzzyMatch(query, c.title ?? c.sessionKey),
    );
  }, [navigable, query]);

  const grouped = React.useMemo(
    () => groupConversations(filtered, Date.now()),
    [filtered],
  );

  if (collapsed) {
    return (
      <aside
        className={cn(
          "flex w-12 shrink-0 flex-col items-center gap-2 overflow-hidden",
          "rounded-sg-lg sg-card py-3",
        )}
        data-testid="chat-sidebar-collapsed"
      >
        <button
          type="button"
          onClick={onToggleCollapsed}
          className="rounded-sg-sm p-1.5 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink"
          aria-label={t("chat.expandSidebar")}
        >
          <ChevronRight className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          onClick={onNew}
          className="rounded-sg-sm p-1.5 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink"
          aria-label={t("chat.newChat")}
        >
          <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
        </button>
      </aside>
    );
  }

  return (
    <aside
      className={cn(
        "flex w-64 shrink-0 flex-col overflow-hidden",
        "rounded-sg-lg sg-card",
      )}
      data-testid="chat-sidebar"
    >
      <div className="flex items-center gap-1 border-b border-sg-border px-2 py-2">
        <button
          type="button"
          onClick={onNew}
          className={cn(
            "flex flex-1 items-center justify-center gap-1.5 rounded-sg-md",
            "border border-sg-accent/40 bg-sg-accent px-2 py-1.5 text-[12px] text-white",
            "shadow-sg-1 hover:bg-sg-accent/90",
          )}
          data-testid="chat-sidebar-new"
        >
          <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden="true" />
          {t("chat.newChat")}
        </button>
        {onToggleCollapsed ? (
          <button
            type="button"
            onClick={onToggleCollapsed}
            className="rounded-sg-sm p-1.5 text-sg-ink-4 hover:bg-sg-inset hover:text-sg-ink"
            aria-label={t("chat.collapseSidebar")}
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
          </button>
        ) : null}
      </div>

      <div className="border-b border-sg-border px-2 py-2">
        <div className="flex items-center gap-1.5 rounded-full bg-sg-inset px-2.5 py-1.5">
          <Search className="h-3.5 w-3.5 text-sg-ink-4" aria-hidden="true" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("chat.searchPlaceholder")}
            className="flex-1 bg-transparent text-[12px] text-sg-ink placeholder:text-sg-ink-5 focus:outline-none"
            data-testid="chat-sidebar-search"
          />
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto px-1 py-2" aria-label="conversations">
        {grouped.length === 0 ? (
          <div className="px-2 py-6 text-center text-[12px] text-sg-ink-4">
            {t("chat.noConversations")}
          </div>
        ) : null}
        {grouped.map((group) => (
          <div key={group.labelKey} className="mb-2">
            <div className="px-2 pb-1 text-[11px] font-medium uppercase tracking-wider text-sg-ink-5">
              {t(group.labelKey)}
            </div>
            <ul className="flex flex-col gap-0.5">
              {group.rows.map((c) => (
                <SidebarRow
                  key={c.sessionKey}
                  conv={c}
                  active={c.sessionKey === activeSessionKey}
                  renaming={renamingKey === c.sessionKey}
                  renameValue={renameValue}
                  onStartRename={() => {
                    setRenamingKey(c.sessionKey);
                    setRenameValue(c.title ?? "");
                  }}
                  onConfirmRename={(v) => {
                    onRename(c.sessionKey, v);
                    setRenamingKey(null);
                  }}
                  onCancelRename={() => setRenamingKey(null)}
                  onRenameValueChange={setRenameValue}
                  onTogglePin={() => onTogglePin(c.sessionKey)}
                  onToggleArchive={() => onToggleArchive(c.sessionKey)}
                  onDelete={() => onDelete(c.sessionKey)}
                />
              ))}
            </ul>
          </div>
        ))}
      </nav>
    </aside>
  );
}

interface SidebarRowProps {
  conv: ChatConversation;
  active: boolean;
  renaming: boolean;
  renameValue: string;
  onStartRename: () => void;
  onConfirmRename: (v: string) => void;
  onCancelRename: () => void;
  onRenameValueChange: (v: string) => void;
  onTogglePin: () => void;
  onToggleArchive: () => void;
  onDelete: () => void;
}

function SidebarRow({
  conv,
  active,
  renaming,
  renameValue,
  onStartRename,
  onConfirmRename,
  onCancelRename,
  onRenameValueChange,
  onTogglePin,
  onToggleArchive,
  onDelete,
}: SidebarRowProps) {
  const router = useRouter();
  const { t } = useTranslation();
  const reducedMotion = useReducedMotion();

  const fallbackTitle = `Session ${conv.sessionKey.slice(0, 8)}`;
  const title = conv.title ?? fallbackTitle;
  const subtitle = `${t("chat.sessionRowSubtitleMsg", { count: conv.messageCount })} · ${formatRelative(conv.lastMessageAt, t)}`;

  return (
    <motion.li
      className={cn(
        "lg-gel group relative isolate flex items-center gap-1 rounded-sg-md border px-2 py-1.5 text-[12px]",
        active
          ? "border-sg-accent/25 text-sg-ink"
          : "border-transparent text-sg-ink-3 hover:bg-sg-inset-hover hover:text-sg-ink",
      )}
      data-testid="chat-sidebar-row"
      data-active={active ? "true" : undefined}
      data-session-key={conv.sessionKey}
    >
      {/* Shared-layout active pill — glides between rows with a snappy
          spring rather than snapping the background on/off. */}
      {active ? (
        <motion.span
          layoutId={reducedMotion ? undefined : "chat-sidebar-active"}
          transition={springs.snappy}
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 -z-10 rounded-sg-md bg-sg-accent-soft"
        />
      ) : null}
      {conv.pinned ? (
        <Pin className="h-3 w-3 shrink-0 text-sg-accent" aria-hidden="true" />
      ) : null}

      {renaming ? (
        <input
          autoFocus
          value={renameValue}
          onChange={(e) => onRenameValueChange(e.target.value)}
          onBlur={() => onConfirmRename(renameValue)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onConfirmRename(renameValue);
            if (e.key === "Escape") onCancelRename();
          }}
          className="flex-1 rounded-sg-sm border border-sg-accent/40 bg-sg-inset px-1 py-0.5 text-[12px] text-sg-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-sg-accent/40"
          data-testid="chat-rename-input"
        />
      ) : (
        <Link
          href={`/chat?session=${encodeURIComponent(conv.sessionKey)}`}
          className="flex flex-1 flex-col overflow-hidden"
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey) return;
            router.push(`/chat?session=${encodeURIComponent(conv.sessionKey)}`);
          }}
        >
          <span className="truncate" title={title}>
            {title}
          </span>
          <span className="truncate text-[10px] text-sg-ink-5">{subtitle}</span>
        </Link>
      )}

      {!renaming ? (
        <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
          <RowAction
            label={conv.pinned ? t("chat.unpin") : t("chat.pin")}
            onClick={onTogglePin}
            Icon={conv.pinned ? PinOff : Pin}
          />
          <RowAction label={t("chat.rename")} onClick={onStartRename} Icon={Pencil} />
          <RowAction
            label={conv.archived ? t("chat.unarchive") : t("chat.archive")}
            onClick={onToggleArchive}
            Icon={conv.archived ? ArchiveRestore : Archive}
          />
          <RowAction label={t("chat.delete")} onClick={onDelete} Icon={Trash2} danger />
        </div>
      ) : null}
    </motion.li>
  );
}

function RowAction({
  label,
  onClick,
  Icon,
  danger,
}: {
  label: string;
  onClick: () => void;
  Icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        onClick();
      }}
      className={cn(
        "rounded-sg-sm p-1 text-sg-ink-4 hover:bg-sg-inset",
        danger ? "hover:text-sg-err" : "hover:text-sg-ink",
      )}
      aria-label={label}
      title={label}
    >
      <Icon className="h-3 w-3" aria-hidden />
    </button>
  );
}

function formatRelative(ms: number, t: (key: string, opts?: Record<string, unknown>) => string): string {
  const diff = Date.now() - ms;
  if (diff < 60_000) return t("chat.relativeJustNow");
  if (diff < 3_600_000) return t("chat.relativeMinutes", { n: Math.floor(diff / 60_000) });
  if (diff < 86_400_000) return t("chat.relativeHours", { n: Math.floor(diff / 3_600_000) });
  return t("chat.relativeDays", { n: Math.floor(diff / 86_400_000) });
}
