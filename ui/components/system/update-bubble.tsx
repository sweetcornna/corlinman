"use client";

/**
 * Compatibility shim — `<UpdateBubble>` grew into `<VersionBadge>` (an
 * always-visible version chip with a sub2api-style dropdown panel; see
 * ./version-badge.tsx). This file keeps the historical import paths
 * working: `nav.tsx` mounts `UpdateBubble`, and the system page imports
 * `DISMISS_KEY` from here.
 */

export {
  DISMISS_KEY,
  VersionBadge,
  VersionBadge as UpdateBubble,
  type VersionBadgeProps,
  type VersionBadgeProps as UpdateBubbleProps,
} from "./version-badge";
