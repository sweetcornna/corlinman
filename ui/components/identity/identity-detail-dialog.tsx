"use client";

import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Copy, KeyRound, Merge } from "lucide-react";
import { toast } from "sonner";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime } from "@/lib/format";
import {
  fetchIdentityDetail,
  issueIdentityPhrase,
  mergeIdentities,
  type ChannelAlias,
  type UserSummary,
} from "@/lib/api/identity";

/**
 * Per-user identity detail panel. Shows aliases and lets the operator:
 *  1. Issue a verification phrase from a chosen alias (the user echoes
 *     it on a different channel to unify).
 *  2. Manually merge another `from_user_id` INTO this user.
 *
 * Both mutations call the gateway; on success the parent's
 * `onMutated()` hook refreshes the list view so alias_count stays
 * current.
 */
export function IdentityDetailDialog({
  user,
  open,
  onClose,
  onMutated,
}: {
  user: UserSummary;
  open: boolean;
  onClose: () => void;
  onMutated: () => void;
}) {
  const { t } = useTranslation();
  const detailQuery = useQuery({
    queryKey: ["admin", "identity", user.user_id],
    queryFn: () => fetchIdentityDetail(user.user_id),
    enabled: open,
  });

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>
            {t("identity.detail.title", "Identity")}
            <span className="ml-2 font-mono text-xs text-sg-ink-3">
              {user.user_id}
            </span>
          </DialogTitle>
          <DialogDescription>
            {t(
              "identity.detail.subtitle",
              "Aliases bound to this canonical user. Issue a verification phrase from one alias and have the human paste it on another channel to unify additional aliases under this user.",
            )}
          </DialogDescription>
        </DialogHeader>

        <section className="space-y-2">
          <h3 className="text-sm font-medium">
            {t("identity.detail.aliases", "Aliases")}
          </h3>
          {detailQuery.isLoading && <Skeleton className="h-24 w-full" />}
          {detailQuery.data?.kind === "not_found" && (
            <p className="text-sm text-sg-ink-3">
              {t(
                "identity.detail.not_found",
                "User not found — the row may have been merged out by a concurrent operator action.",
              )}
            </p>
          )}
          {detailQuery.data?.kind === "ok" && (
            <AliasList
              aliases={detailQuery.data.detail.aliases}
              onIssued={onMutated}
              userId={user.user_id}
            />
          )}
        </section>

        <section className="space-y-2 border-t pt-3">
          <h3 className="text-sm font-medium inline-flex items-center gap-2">
            <Merge className="h-4 w-4" aria-hidden />
            {t("identity.detail.merge.title", "Manual merge")}
          </h3>
          <MergeForm
            intoUserId={user.user_id}
            onMerged={() => {
              onMutated();
              onClose();
            }}
          />
        </section>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            {t("common.close", "Close")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AliasList({
  aliases,
  onIssued,
  userId,
}: {
  aliases: ChannelAlias[];
  onIssued: () => void;
  userId: string;
}) {
  const { t } = useTranslation();
  if (aliases.length === 0) {
    return (
      <p className="text-sm text-sg-ink-3">
        {t(
          "identity.detail.no_aliases",
          "No aliases bound — this user was created without auto-bind, possibly by an operator merge.",
        )}
      </p>
    );
  }
  return (
    <ul className="divide-y divide-sg-border rounded-md border border-sg-border">
      {aliases.map((a) => (
        <li
          key={`${a.channel}:${a.channel_user_id}`}
          data-testid={`alias-row-${a.channel}-${a.channel_user_id}`}
          className="flex items-center justify-between gap-3 px-3 py-2"
        >
          <div className="min-w-0 flex-1">
            <p className="font-mono text-xs">
              <span className="font-semibold">{a.channel}</span>
              <span className="mx-1 text-sg-ink-3">:</span>
              <span>{a.channel_user_id}</span>
            </p>
            <p className="text-[11px] text-sg-ink-3">
              {a.binding_kind} ·{" "}
              {formatDateTime(new Date(a.created_at))}
            </p>
          </div>
          <IssuePhraseButton
            userId={userId}
            channel={a.channel}
            channelUserId={a.channel_user_id}
            onIssued={onIssued}
          />
        </li>
      ))}
    </ul>
  );
}

function IssuePhraseButton({
  userId,
  channel,
  channelUserId,
  onIssued,
}: {
  userId: string;
  channel: string;
  channelUserId: string;
  onIssued: () => void;
}) {
  const { t } = useTranslation();
  const [phrase, setPhrase] = React.useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      issueIdentityPhrase(userId, {
        channel,
        channel_user_id: channelUserId,
      }),
    onSuccess: (res) => {
      if (res.kind === "ok") {
        setPhrase(res.response.phrase);
        toast.success(
          t("identity.phrase.issued", "Verification phrase issued"),
        );
        onIssued();
      } else if (res.kind === "disabled") {
        toast.error(
          t(
            "identity.phrase.disabled",
            "Identity service disabled — cannot issue phrase.",
          ),
        );
      } else {
        toast.error(res.message);
      }
    },
  });

  if (phrase) {
    return (
      <button
        type="button"
        onClick={() => {
          void navigator.clipboard.writeText(phrase);
          toast.success(t("common.copied", "Copied"));
        }}
        className="inline-flex items-center gap-1 rounded-md border border-sg-accent/30 bg-sg-accent-soft px-2 py-1 font-mono text-[11px] text-sg-accent hover:bg-[color-mix(in_oklch,var(--sg-accent)_22%,transparent)]"
        data-testid="phrase-copy"
      >
        {phrase}
        <Copy className="h-3 w-3" aria-hidden />
      </button>
    );
  }

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      onClick={() => mutation.mutate()}
      disabled={mutation.isPending}
      data-testid="phrase-issue-btn"
    >
      <KeyRound className="mr-1.5 h-3.5 w-3.5" aria-hidden />
      {mutation.isPending
        ? t("identity.phrase.issuing", "Issuing…")
        : t("identity.phrase.issue", "Issue phrase")}
    </Button>
  );
}

function MergeForm({
  intoUserId,
  onMerged,
}: {
  intoUserId: string;
  onMerged: () => void;
}) {
  const { t } = useTranslation();
  const [fromUserId, setFromUserId] = React.useState("");
  const [decidedBy, setDecidedBy] = React.useState("");

  const mutation = useMutation({
    mutationFn: () =>
      mergeIdentities({
        into_user_id: intoUserId,
        from_user_id: fromUserId,
        decided_by: decidedBy || "operator",
      }),
    onSuccess: (res) => {
      if (res.kind === "ok") {
        toast.success(
          t("identity.merge.ok", "Identities merged"),
        );
        setFromUserId("");
        onMerged();
      } else if (res.kind === "not_found") {
        toast.error(
          t(
            "identity.merge.not_found",
            "Source user_id not found — nothing to merge.",
          ),
        );
      } else if (res.kind === "disabled") {
        toast.error(
          t(
            "identity.merge.disabled",
            "Identity service disabled — cannot merge.",
          ),
        );
      } else {
        toast.error(res.message);
      }
    },
  });

  return (
    <form
      className="grid gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate();
      }}
    >
      <div className="grid gap-1">
        <Label htmlFor="merge-from">
          {t("identity.merge.from_label", "From user_id")}
        </Label>
        <Input
          id="merge-from"
          value={fromUserId}
          onChange={(e) => setFromUserId(e.target.value)}
          placeholder="01HV3K9PQRSTUVWXYZABCDEFGH"
          required
          data-testid="merge-from-input"
        />
      </div>
      <div className="grid gap-1">
        <Label htmlFor="merge-decided-by">
          {t("identity.merge.decided_by_label", "Decided by")}
        </Label>
        <Input
          id="merge-decided-by"
          value={decidedBy}
          onChange={(e) => setDecidedBy(e.target.value)}
          placeholder="operator"
          data-testid="merge-decided-by-input"
        />
      </div>
      <Button
        type="submit"
        size="sm"
        disabled={!fromUserId || mutation.isPending}
        data-testid="merge-submit-btn"
      >
        {mutation.isPending
          ? t("identity.merge.merging", "Merging…")
          : t("identity.merge.submit", "Merge into this user")}
      </Button>
    </form>
  );
}
