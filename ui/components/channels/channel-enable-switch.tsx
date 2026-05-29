"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { Switch } from "@/components/ui/switch";
import {
  fetchConfig,
  patchChannelEnabled,
  readChannelEnabled,
  setChannelEnabled,
  type ChannelName,
  type ConfigGetResponse,
  type ConfigPostResponse,
} from "@/lib/api";

export interface ChannelEnableSwitchProps {
  channel: ChannelName;
  /** Extra react-query keys to invalidate on success (e.g. `[["qq-status"]]`). */
  invalidateOnSuccess?: ReadonlyArray<readonly unknown[]>;
  className?: string;
}

/**
 * Small pill-style toggle that flips `channels.<channel>.enabled` via
 * `POST /admin/config`. Fetches the current config to derive the initial
 * state (so the switch reflects on-disk truth, not an in-memory echo).
 *
 * On validation failure (e.g. enabling Telegram without a `bot_token`
 * configured), we surface the backend's `issues[]` as a toast and leave
 * the on-disk state unchanged — the switch snaps back to its previous
 * position on the next query refetch.
 */
export function ChannelEnableSwitch({
  channel,
  invalidateOnSuccess,
  className,
}: ChannelEnableSwitchProps) {
  const queryClient = useQueryClient();

  const configQuery = useQuery<ConfigGetResponse>({
    queryKey: ["admin-config"],
    queryFn: fetchConfig,
    staleTime: 10_000,
  });

  const enabled = configQuery.data
    ? readChannelEnabled(configQuery.data.toml, channel)
    : false;

  const mutation = useMutation<
    { response: ConfigPostResponse; next: boolean },
    Error,
    boolean
  >({
    mutationFn: async (next) => {
      const response = await setChannelEnabled(channel, next);
      return { response, next };
    },
    onSuccess: async ({ response, next }) => {
      if (response.status === "invalid") {
        const errorIssues = response.issues.filter((i) => i.level === "error");
        const description =
          errorIssues.length > 0
            ? errorIssues.map((i) => `${i.path}: ${i.message}`).join("\n")
            : "Unknown validation error";
        toast.error(
          `Cannot ${next ? "enable" : "disable"} ${labelFor(channel)}`,
          { description },
        );
        // Optimistically reconcile from source of truth
        await queryClient.invalidateQueries({ queryKey: ["admin-config"] });
        return;
      }

      // Optimistic cache update so the switch doesn't flicker
      queryClient.setQueryData<ConfigGetResponse>(["admin-config"], (prev) =>
        prev
          ? { ...prev, toml: patchChannelEnabled(prev.toml, channel, next) }
          : prev,
      );

      const restartNote =
        response.requires_restart.length > 0
          ? `Restart scope: ${response.requires_restart.join(", ")}`
          : undefined;
      toast.success(
        `${labelFor(channel)} ${next ? "enabled" : "disabled"}`,
        restartNote ? { description: restartNote } : undefined,
      );

      await queryClient.invalidateQueries({ queryKey: ["admin-config"] });
      if (invalidateOnSuccess) {
        for (const key of invalidateOnSuccess) {
          await queryClient.invalidateQueries({ queryKey: [...key] });
        }
      }
    },
    onError: (err) => {
      toast.error(`Toggle failed: ${err.message}`);
    },
  });

  const disabled = mutation.isPending || configQuery.isLoading;
  const idRoot = React.useId();
  const switchId = `${idRoot}-${channel}-enable-switch`;
  const labelId = `${idRoot}-${channel}-enable-label`;

  return (
    <div
      className={cn(
        "inline-flex items-center gap-2 rounded-lg border border-tp-glass-edge bg-tp-glass-inner px-3 py-1.5",
        className,
      )}
      data-testid={`${channel}-enable-toggle`}
    >
      <Switch
        checked={enabled}
        disabled={disabled}
        onCheckedChange={(next) => mutation.mutate(next)}
        id={switchId}
        aria-labelledby={labelId}
        data-testid={`${channel}-enable-switch`}
      />
      <label
        htmlFor={switchId}
        id={labelId}
        className="select-none text-[13px] font-medium text-tp-ink-2"
      >
        {mutation.isPending ? (
          <span className="inline-flex items-center gap-1.5">
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
            {enabled ? "Disabling…" : "Enabling…"}
          </span>
        ) : enabled ? (
          "Enabled"
        ) : (
          "Disabled"
        )}
      </label>
    </div>
  );
}

function labelFor(channel: ChannelName): string {
  const labels: Record<ChannelName, string> = {
    qq: "QQ channel",
    telegram: "Telegram channel",
    discord: "Discord channel",
    slack: "Slack channel",
    feishu: "Feishu channel",
    wechat_official: "WeChat Official channel",
    qq_official: "QQ Official channel",
  };
  return labels[channel] ?? `${channel} channel`;
}

export default ChannelEnableSwitch;
