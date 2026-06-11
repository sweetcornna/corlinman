"use client";

import * as React from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Send } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Drawer } from "@/components/ui/drawer";
import { Label } from "@/components/ui/label";
import type {
  FullInboxSendRequest,
  FullInboxSendResponse,
} from "@/lib/api/full-inbox-channel";

/**
 * Shared "send test message" drawer for the full-inbox channels (Discord /
 * Slack / Feishu). Mirrors the Telegram `SendTestDrawer` but binds the
 * channel-specific `sendFn` + i18n namespace passed by the page.
 *
 * The target field maps to `target_id` on the wire — the backend resolves
 * `target_id` / `chat_id` / `channel_id` interchangeably.
 */
export function InboxSendTestDrawer({
  open,
  onOpenChange,
  nsKey,
  testIdPrefix,
  sendFn,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  nsKey: string;
  testIdPrefix: string;
  sendFn: (body: FullInboxSendRequest) => Promise<FullInboxSendResponse>;
}) {
  const { t } = useTranslation();
  const [target, setTarget] = React.useState("");
  const [text, setText] = React.useState("");

  React.useEffect(() => {
    if (open) {
      setTarget("");
      setText("");
    }
  }, [open]);

  const mutation = useMutation({
    mutationFn: (body: FullInboxSendRequest) => sendFn(body),
    onSuccess: (res) => {
      if (res.ok) {
        toast.success(t(`${nsKey}.sendTestSuccess`));
        onOpenChange(false);
      } else {
        toast.error(t(`${nsKey}.sendTestFailed`));
      }
    },
    onError: (err) => {
      const msg =
        err instanceof Error ? err.message : t(`${nsKey}.sendTestFailed`);
      toast.error(msg);
    },
  });

  const trimmedTarget = target.trim();
  const trimmedText = text.trim();
  const sendDisabled =
    trimmedTarget.length === 0 ||
    trimmedText.length === 0 ||
    mutation.isPending;

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (sendDisabled) return;
    mutation.mutate({ target_id: trimmedTarget, text });
  };

  const fieldClass = cn(
    "w-full rounded-lg border border-sg-border bg-sg-inset",
    "px-3 py-2 text-[13px] text-sg-ink placeholder:text-sg-ink-4",
    "transition-colors hover:bg-sg-inset-hover",
    "focus:outline-none focus:ring-2 focus:ring-sg-accent/40",
  );

  const formId = `${testIdPrefix}-send-test-form`;

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      title={t(`${nsKey}.sendTestTitle`)}
      description={t(`${nsKey}.sendTestDescription`)}
      width="sm"
      footer={
        <>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onOpenChange(false)}
          >
            {t("common.cancel")}
          </Button>
          <Button
            type="submit"
            form={formId}
            size="sm"
            disabled={sendDisabled}
            data-testid={`${testIdPrefix}-send-test-submit`}
          >
            <Send className="h-3.5 w-3.5" aria-hidden="true" />
            {mutation.isPending
              ? t(`${nsKey}.sendTestSubmitting`)
              : t(`${nsKey}.sendTestSubmit`)}
          </Button>
        </>
      }
    >
      <form
        id={formId}
        onSubmit={handleSubmit}
        className="flex flex-col gap-4 p-5"
      >
        <div className="space-y-1.5">
          <Label
            htmlFor={`${testIdPrefix}-send-target`}
            className="text-sg-ink-2"
          >
            {t(`${nsKey}.sendTestTarget`)}
          </Label>
          <input
            id={`${testIdPrefix}-send-target`}
            type="text"
            placeholder={t(`${nsKey}.sendTestTargetPlaceholder`)}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            autoComplete="off"
            data-testid={`${testIdPrefix}-send-target`}
            required
            className={fieldClass}
          />
          <p className="text-[11px] text-sg-ink-4">
            {t(`${nsKey}.sendTestTargetHint`)}
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor={`${testIdPrefix}-send-text`} className="text-sg-ink-2">
            {t(`${nsKey}.sendTestMessage`)}
          </Label>
          <textarea
            id={`${testIdPrefix}-send-text`}
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={5}
            placeholder={t(`${nsKey}.sendTestMessagePlaceholder`)}
            className={cn(fieldClass, "resize-y")}
            data-testid={`${testIdPrefix}-send-text`}
          />
        </div>
      </form>
    </Drawer>
  );
}

export default InboxSendTestDrawer;
