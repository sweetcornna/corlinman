"use client";

import * as React from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Send } from "@/components/icons";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Drawer } from "@/components/ui/drawer";
import { Label } from "@/components/ui/label";
import {
  sendTelegramTestMessage,
  type TelegramSendRequest,
} from "@/lib/api/telegram";

/**
 * "Send test message" drawer — Phase 5e Tidepool retoken.
 *
 * Fields reuse Tidepool's warm-glass inputs (matches the scheduler search +
 * approvals deny-reason drawer dialect). The mutation calls the live
 * `POST /admin/channels/telegram/send` route; transport errors surface as
 * toast notifications via `onError`.
 */
export function SendTestDrawer({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const [chatId, setChatId] = React.useState("");
  const [text, setText] = React.useState("");

  // Reset fields when the drawer opens so stale values don't linger.
  React.useEffect(() => {
    if (open) {
      setChatId("");
      setText("");
    }
  }, [open]);

  const mutation = useMutation({
    mutationFn: (body: TelegramSendRequest) => sendTelegramTestMessage(body),
    onSuccess: (res) => {
      if (res.status === "ok") {
        toast.success(t("channels.telegram.tp.sendTestSuccess"));
        onOpenChange(false);
      } else {
        toast.error(res.error ?? t("channels.telegram.tp.sendTestFailed"));
      }
    },
    onError: (err) => {
      const msg =
        err instanceof Error
          ? err.message
          : t("channels.telegram.tp.sendTestFailed");
      toast.error(msg);
    },
  });

  const trimmedChatId = chatId.trim();
  const sendDisabled = trimmedChatId.length === 0 || mutation.isPending;

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (sendDisabled) return;
    mutation.mutate({ chat_id: trimmedChatId, text });
  };

  const fieldClass = cn(
    "w-full rounded-lg border border-sg-border bg-sg-inset",
    "px-3 py-2 text-[13px] text-sg-ink placeholder:text-sg-ink-4",
    "transition-colors hover:bg-sg-inset-hover",
    "focus:outline-none focus:ring-2 focus:ring-sg-accent/40",
  );

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      title={t("channels.telegram.tp.sendTestTitle")}
      description={t("channels.telegram.tp.sendTestDescription")}
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
            form="tg-send-test-form"
            size="sm"
            disabled={sendDisabled}
            data-testid="tg-send-test-submit"
          >
            <Send className="h-3.5 w-3.5" aria-hidden="true" />
            {mutation.isPending
              ? t("channels.telegram.tp.sendTestSubmitting")
              : t("channels.telegram.tp.sendTestSubmit")}
          </Button>
        </>
      }
    >
      <form
        id="tg-send-test-form"
        onSubmit={handleSubmit}
        className="flex flex-col gap-4 p-5"
      >
        <div className="space-y-1.5">
          <Label htmlFor="tg-send-chat-id" className="text-sg-ink-2">
            {t("channels.telegram.tp.sendTestChatId")}
          </Label>
          <input
            id="tg-send-chat-id"
            type="text"
            placeholder={t("channels.telegram.tp.sendTestChatIdPlaceholder")}
            value={chatId}
            onChange={(e) => setChatId(e.target.value)}
            autoComplete="off"
            data-testid="tg-send-chat-id"
            required
            className={fieldClass}
          />
          <p className="text-[11px] text-sg-ink-4">
            {t("channels.telegram.tp.sendTestChatIdHint")}
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="tg-send-text" className="text-sg-ink-2">
            {t("channels.telegram.tp.sendTestMessage")}
          </Label>
          <textarea
            id="tg-send-text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={5}
            placeholder={t("channels.telegram.tp.sendTestMessagePlaceholder")}
            className={cn(fieldClass, "resize-y")}
            data-testid="tg-send-text"
          />
        </div>
      </form>
    </Drawer>
  );
}

export default SendTestDrawer;
