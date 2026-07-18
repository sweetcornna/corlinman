// GENERATED barrel — Eclipse icon components over the inline sprite.
// Drop-in replacement for the former lucide-react imports: same PascalCase
// names, same className/size ergonomics; stroke geometry lives in the
// sprite symbols (uniform 1.8 per the design language), so per-call-site
// strokeWidth props are intentionally ignored.
import * as React from "react";

export type IconName =
  | "activity"
  | "archive"
  | "archive-restore"
  | "arrow-down"
  | "arrow-left"
  | "arrow-right"
  | "arrow-up"
  | "arrow-up-right"
  | "at-sign"
  | "beaker"
  | "book-open"
  | "bot"
  | "boxes"
  | "brain"
  | "branch"
  | "building-2"
  | "calendar"
  | "check"
  | "chevron-down"
  | "chevron-left"
  | "chevron-right"
  | "chevron-up"
  | "chevrons-left"
  | "chevrons-right"
  | "circle"
  | "circle-alert"
  | "circle-arrow-up"
  | "circle-check"
  | "circle-check-big"
  | "circle-dot"
  | "circle-play"
  | "circle-plus"
  | "circle-x"
  | "clipboard-check"
  | "clock"
  | "coins"
  | "copy"
  | "corner-up-left"
  | "database"
  | "dot"
  | "download"
  | "ellipsis-vertical"
  | "external-link"
  | "eye"
  | "eye-off"
  | "file-code"
  | "file-code-corner"
  | "file-headphone"
  | "file-play"
  | "file-plus-corner"
  | "file-terminal"
  | "file-text"
  | "film"
  | "fingerprint-pattern"
  | "funnel-x"
  | "git-fork"
  | "git-pull-request"
  | "globe"
  | "hammer"
  | "hash"
  | "history"
  | "hourglass"
  | "image"
  | "image-plus"
  | "inbox"
  | "info"
  | "key"
  | "key-round"
  | "key-square"
  | "languages"
  | "leaf"
  | "link"
  | "loader-circle"
  | "lock"
  | "log-in"
  | "log-out"
  | "maximize-2"
  | "menu"
  | "merge"
  | "message-circle"
  | "message-square"
  | "message-square-dashed"
  | "message-square-plus"
  | "message-square-text"
  | "messages-square"
  | "mic"
  | "monitor-cog"
  | "moon"
  | "more-h"
  | "music"
  | "network"
  | "newspaper"
  | "octagon"
  | "octagon-alert"
  | "octagon-x"
  | "palette"
  | "panel-right-close"
  | "panel-right-open"
  | "paperclip"
  | "pause"
  | "pencil"
  | "pin"
  | "pin-off"
  | "play"
  | "plug"
  | "plus"
  | "power"
  | "power-off"
  | "qr-code"
  | "radio"
  | "refresh-ccw"
  | "refresh-cw"
  | "repeat"
  | "reply"
  | "rocket"
  | "rotate-ccw"
  | "rotate-cw"
  | "route"
  | "save"
  | "search"
  | "send"
  | "server"
  | "settings"
  | "settings-2"
  | "shield"
  | "shield-alert"
  | "shield-check"
  | "signal"
  | "smile"
  | "sparkles"
  | "sprout"
  | "square"
  | "star"
  | "store"
  | "sun"
  | "tag"
  | "terminal"
  | "timer"
  | "trash-2"
  | "triangle-alert"
  | "unplug"
  | "user"
  | "user-cog"
  | "users"
  | "wifi"
  | "wifi-off"
  | "wrench"
  | "x"
  | "zap";

export interface IconProps extends Omit<React.SVGProps<SVGSVGElement>, "ref"> {
  size?: number | string;
}

/** Component signature shared by every icon (lucide-compatible). */
export type LucideIcon = React.FC<IconProps>;

export function Icon({
  name,
  size = 24,
  ...rest
}: IconProps & { name: IconName }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden={rest["aria-label"] ? undefined : true}
      {...rest}
    >
      <use href={`#i-${name}`} />
    </svg>
  );
}

function makeIcon(name: IconName, displayName: string): LucideIcon {
  const Comp: LucideIcon = (props) => <Icon {...props} name={name} />;
  Comp.displayName = displayName;
  return Comp;
}

export const Activity = makeIcon("activity", "Activity");
export const AlertCircle = makeIcon("circle-alert", "AlertCircle");
export const AlertTriangle = makeIcon("triangle-alert", "AlertTriangle");
export const Archive = makeIcon("archive", "Archive");
export const ArchiveRestore = makeIcon("archive-restore", "ArchiveRestore");
export const ArrowDown = makeIcon("arrow-down", "ArrowDown");
export const ArrowLeft = makeIcon("arrow-left", "ArrowLeft");
export const ArrowRight = makeIcon("arrow-right", "ArrowRight");
export const ArrowUpCircle = makeIcon("circle-arrow-up", "ArrowUpCircle");
export const ArrowUpRight = makeIcon("arrow-up-right", "ArrowUpRight");
export const AtSign = makeIcon("at-sign", "AtSign");
export const Beaker = makeIcon("beaker", "Beaker");
export const BookOpen = makeIcon("book-open", "BookOpen");
export const Bot = makeIcon("bot", "Bot");
export const Boxes = makeIcon("boxes", "Boxes");
export const Brain = makeIcon("brain", "Brain");
export const Building2 = makeIcon("building-2", "Building2");
export const Calendar = makeIcon("calendar", "Calendar");
export const Check = makeIcon("check", "Check");
export const CheckCircle = makeIcon("circle-check-big", "CheckCircle");
export const CheckCircle2 = makeIcon("circle-check", "CheckCircle2");
export const ChevronDown = makeIcon("chevron-down", "ChevronDown");
export const ChevronLeft = makeIcon("chevron-left", "ChevronLeft");
export const ChevronRight = makeIcon("chevron-right", "ChevronRight");
export const ChevronsLeft = makeIcon("chevrons-left", "ChevronsLeft");
export const ChevronsRight = makeIcon("chevrons-right", "ChevronsRight");
export const ChevronUp = makeIcon("chevron-up", "ChevronUp");
export const Circle = makeIcon("circle", "Circle");
export const CircleCheck = makeIcon("circle-check", "CircleCheck");
export const CircleDot = makeIcon("circle-dot", "CircleDot");
export const CircleX = makeIcon("circle-x", "CircleX");
export const ClipboardCheck = makeIcon("clipboard-check", "ClipboardCheck");
export const Clock = makeIcon("clock", "Clock");
export const Coins = makeIcon("coins", "Coins");
export const Copy = makeIcon("copy", "Copy");
export const CornerUpLeft = makeIcon("corner-up-left", "CornerUpLeft");
export const Database = makeIcon("database", "Database");
export const Download = makeIcon("download", "Download");
export const ExternalLink = makeIcon("external-link", "ExternalLink");
export const Eye = makeIcon("eye", "Eye");
export const EyeOff = makeIcon("eye-off", "EyeOff");
export const FileAudio = makeIcon("file-headphone", "FileAudio");
export const FileCode = makeIcon("file-code", "FileCode");
export const FileCode2 = makeIcon("file-code-corner", "FileCode2");
export const FilePlus2 = makeIcon("file-plus-corner", "FilePlus2");
export const FileTerminal = makeIcon("file-terminal", "FileTerminal");
export const FileText = makeIcon("file-text", "FileText");
export const FileVideo = makeIcon("file-play", "FileVideo");
export const Film = makeIcon("film", "Film");
export const FilterX = makeIcon("funnel-x", "FilterX");
export const Fingerprint = makeIcon("fingerprint-pattern", "Fingerprint");
export const GitFork = makeIcon("git-fork", "GitFork");
export const GitPullRequest = makeIcon("git-pull-request", "GitPullRequest");
export const Globe = makeIcon("globe", "Globe");
export const Hammer = makeIcon("hammer", "Hammer");
export const Hash = makeIcon("hash", "Hash");
export const History = makeIcon("history", "History");
export const Hourglass = makeIcon("hourglass", "Hourglass");
export const Image = makeIcon("image", "Image");
export const ImageIcon = makeIcon("image", "ImageIcon");
export const ImagePlus = makeIcon("image-plus", "ImagePlus");
export const Inbox = makeIcon("inbox", "Inbox");
export const Info = makeIcon("info", "Info");
export const Key = makeIcon("key", "Key");
export const KeyRound = makeIcon("key-round", "KeyRound");
export const KeySquare = makeIcon("key-square", "KeySquare");
export const Languages = makeIcon("languages", "Languages");
export const Leaf = makeIcon("leaf", "Leaf");
export const LinkIcon = makeIcon("link", "LinkIcon");
export const Loader2 = makeIcon("loader-circle", "Loader2");
export const Lock = makeIcon("lock", "Lock");
export const LogIn = makeIcon("log-in", "LogIn");
export const LogOut = makeIcon("log-out", "LogOut");
export const Maximize2 = makeIcon("maximize-2", "Maximize2");
export const Menu = makeIcon("menu", "Menu");
export const Merge = makeIcon("merge", "Merge");
export const MessageCircle = makeIcon("message-circle", "MessageCircle");
export const MessageSquare = makeIcon("message-square", "MessageSquare");
export const MessageSquareDashed = makeIcon("message-square-dashed", "MessageSquareDashed");
export const MessageSquarePlus = makeIcon("message-square-plus", "MessageSquarePlus");
export const MessageSquareText = makeIcon("message-square-text", "MessageSquareText");
export const MessagesSquare = makeIcon("messages-square", "MessagesSquare");
export const Mic = makeIcon("mic", "Mic");
export const MonitorCog = makeIcon("monitor-cog", "MonitorCog");
export const Moon = makeIcon("moon", "Moon");
export const MoreVertical = makeIcon("ellipsis-vertical", "MoreVertical");
export const Music = makeIcon("music", "Music");
export const Network = makeIcon("network", "Network");
export const Newspaper = makeIcon("newspaper", "Newspaper");
export const Octagon = makeIcon("octagon", "Octagon");
export const OctagonAlert = makeIcon("octagon-alert", "OctagonAlert");
export const OctagonX = makeIcon("octagon-x", "OctagonX");
export const Palette = makeIcon("palette", "Palette");
export const PanelRightClose = makeIcon("panel-right-close", "PanelRightClose");
export const PanelRightOpen = makeIcon("panel-right-open", "PanelRightOpen");
export const Paperclip = makeIcon("paperclip", "Paperclip");
export const Pause = makeIcon("pause", "Pause");
export const Pencil = makeIcon("pencil", "Pencil");
export const Pin = makeIcon("pin", "Pin");
export const PinOff = makeIcon("pin-off", "PinOff");
export const Play = makeIcon("play", "Play");
export const PlayCircle = makeIcon("circle-play", "PlayCircle");
export const Plug = makeIcon("plug", "Plug");
export const Plus = makeIcon("plus", "Plus");
export const PlusCircle = makeIcon("circle-plus", "PlusCircle");
export const Power = makeIcon("power", "Power");
export const PowerOff = makeIcon("power-off", "PowerOff");
export const QrCode = makeIcon("qr-code", "QrCode");
export const Radio = makeIcon("radio", "Radio");
export const RefreshCcw = makeIcon("refresh-ccw", "RefreshCcw");
export const RefreshCw = makeIcon("refresh-cw", "RefreshCw");
export const Repeat = makeIcon("repeat", "Repeat");
export const Reply = makeIcon("reply", "Reply");
export const Rocket = makeIcon("rocket", "Rocket");
export const RotateCcw = makeIcon("rotate-ccw", "RotateCcw");
export const RotateCw = makeIcon("rotate-cw", "RotateCw");
export const Route = makeIcon("route", "Route");
export const Save = makeIcon("save", "Save");
export const Search = makeIcon("search", "Search");
export const Send = makeIcon("send", "Send");
export const Server = makeIcon("server", "Server");
export const Settings = makeIcon("settings", "Settings");
export const Settings2 = makeIcon("settings-2", "Settings2");
export const Shield = makeIcon("shield", "Shield");
export const ShieldAlert = makeIcon("shield-alert", "ShieldAlert");
export const ShieldCheck = makeIcon("shield-check", "ShieldCheck");
export const Smile = makeIcon("smile", "Smile");
export const Sparkles = makeIcon("sparkles", "Sparkles");
export const Sprout = makeIcon("sprout", "Sprout");
export const Square = makeIcon("square", "Square");
export const Star = makeIcon("star", "Star");
export const Store = makeIcon("store", "Store");
export const Sun = makeIcon("sun", "Sun");
export const Tag = makeIcon("tag", "Tag");
export const Terminal = makeIcon("terminal", "Terminal");
export const Timer = makeIcon("timer", "Timer");
export const Trash2 = makeIcon("trash-2", "Trash2");
export const Unplug = makeIcon("unplug", "Unplug");
export const User = makeIcon("user", "User");
export const UserCog = makeIcon("user-cog", "UserCog");
export const Users = makeIcon("users", "Users");
export const WifiOff = makeIcon("wifi-off", "WifiOff");
export const Wrench = makeIcon("wrench", "Wrench");
export const X = makeIcon("x", "X");
export const XCircle = makeIcon("circle-x", "XCircle");
export const XOctagon = makeIcon("octagon-x", "XOctagon");
export const Zap = makeIcon("zap", "Zap");
