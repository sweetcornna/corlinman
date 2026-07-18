"use client";

/**
 * Operator API Keys panel for /account/security.
 *
 * Lists active bearer tokens, mints new ones (showing the cleartext secret
 * exactly once), and revokes them. Backed by `lib/api/api-keys.ts` →
 * `/admin/api_keys*` on the gateway.
 *
 * Design notes:
 *   - The minted secret is shown in a one-time reveal block with a copy
 *     button. We never persist or re-fetch it — the list endpoint never
 *     returns the cleartext, mirroring the backend contract.
 *   - Revoke routes through the shared `ConfirmDialog` (destructive) so an
 *     accidental click can't nuke a live credential.
 *   - A 503 `tenants_disabled` gateway renders a non-fatal note instead of
 *     an error toast, matching how the credentials / tenants panels degrade.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Check, Copy, KeySquare, Trash2 } from "lucide-react";

import {
  type ApiKeyRow,
  listApiKeys,
  mintApiKey,
  revokeApiKey,
} from "@/lib/api/api-keys";
import { CorlinmanApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function fmtMs(ms: number | null): string {
  if (!ms) return "—";
  try {
    return formatDateTime(new Date(ms));
  } catch {
    return "—";
  }
}

export function ApiKeysCard() {
  const { t } = useTranslation();
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "ok"; keys: ApiKeyRow[] }
    | { kind: "disabled" }
    | { kind: "error"; message: string }
  >({ kind: "loading" });

  // Mint form.
  const [scope, setScope] = useState("");
  const [label, setLabel] = useState("");
  const [minting, setMinting] = useState(false);
  // The cleartext token from the most recent mint — shown once, never
  // re-fetchable. Cleared when the operator dismisses the callout.
  const [freshToken, setFreshToken] = useState<MintedCallout | null>(null);

  // Revoke flow.
  const [pendingRevoke, setPendingRevoke] = useState<ApiKeyRow | null>(null);
  const [revoking, setRevoking] = useState(false);

  const refresh = useCallback(async () => {
    const res = await listApiKeys();
    if (res.kind === "ok") setState({ kind: "ok", keys: res.keys });
    else if (res.kind === "disabled") setState({ kind: "disabled" });
    else setState({ kind: "error", message: res.message });
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onMint(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const trimmedScope = scope.trim();
    if (!trimmedScope) {
      toast.error(t("apiKeys.scopeRequired"));
      return;
    }
    setMinting(true);
    try {
      const res = await mintApiKey({
        scope: trimmedScope,
        label: label.trim() || undefined,
      });
      setFreshToken({ token: res.token, keyId: res.key_id });
      setScope("");
      setLabel("");
      toast.success(t("apiKeys.minted"));
      await refresh();
    } catch (err) {
      toast.error(mintError(err, t));
    } finally {
      setMinting(false);
    }
  }

  async function onRevokeConfirm() {
    if (!pendingRevoke) return;
    setRevoking(true);
    try {
      await revokeApiKey(pendingRevoke.key_id);
      toast.success(t("apiKeys.revoked"));
      setPendingRevoke(null);
      await refresh();
    } catch (err) {
      const msg =
        err instanceof CorlinmanApiError ? err.message : String(err);
      toast.error(t("apiKeys.revokeFailed", { msg }));
    } finally {
      setRevoking(false);
    }
  }

  return (
    <Card data-testid="card-api-keys">
      <CardHeader>
        <div className="flex items-center gap-2">
          <KeySquare className="h-4 w-4 text-sg-ink-3" aria-hidden />
          <CardTitle>{t("apiKeys.title")}</CardTitle>
        </div>
        <CardDescription>{t("apiKeys.subtitle")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {state.kind === "disabled" ? (
          <p
            className="rounded-sg-md border border-sg-border bg-sg-inset p-3 text-sm text-sg-ink-3"
            data-testid="api-keys-disabled"
          >
            {t("apiKeys.disabled")}
          </p>
        ) : (
          <>
            {/* Mint form */}
            <form onSubmit={onMint} className="space-y-4" noValidate>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="api-key-scope">
                    {t("apiKeys.scope")}
                  </Label>
                  <Input
                    id="api-key-scope"
                    data-testid="api-key-scope"
                    value={scope}
                    onChange={(e) => setScope(e.target.value)}
                    placeholder={t("apiKeys.scopePlaceholder")}
                    disabled={minting}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="api-key-label">
                    {t("apiKeys.label")}
                  </Label>
                  <Input
                    id="api-key-label"
                    data-testid="api-key-label"
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder={t("apiKeys.labelPlaceholder")}
                    disabled={minting}
                  />
                </div>
              </div>
              <div className="flex justify-end">
                <Button
                  type="submit"
                  disabled={minting}
                  data-testid="api-key-mint"
                >
                  {minting ? t("common.saving") : t("apiKeys.mint")}
                </Button>
              </div>
            </form>

            {/* One-time secret reveal */}
            {freshToken ? (
              <FreshTokenCallout
                token={freshToken.token}
                onDismiss={() => setFreshToken(null)}
              />
            ) : null}

            {/* Key list */}
            {state.kind === "error" ? (
              <p
                role="alert"
                className="text-sm text-destructive"
                data-testid="api-keys-error"
              >
                {t("apiKeys.loadFailed", { msg: state.message })}
              </p>
            ) : state.kind === "loading" ? (
              <p className="text-sm text-sg-ink-3">{t("common.loading")}</p>
            ) : state.keys.length === 0 ? (
              <p
                className="text-sm text-sg-ink-3"
                data-testid="api-keys-empty"
              >
                {t("apiKeys.empty")}
              </p>
            ) : (
              <Table data-testid="api-keys-table">
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("apiKeys.colKeyId")}</TableHead>
                    <TableHead>{t("apiKeys.colScope")}</TableHead>
                    <TableHead>{t("apiKeys.colLabel")}</TableHead>
                    <TableHead>{t("apiKeys.colCreated")}</TableHead>
                    <TableHead>{t("apiKeys.colLastUsed")}</TableHead>
                    <TableHead className="text-right">
                      {t("apiKeys.colActions")}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {state.keys.map((k) => (
                    <TableRow key={k.key_id} data-testid={`api-key-row-${k.key_id}`}>
                      <TableCell className="font-mono text-xs">
                        {k.key_id}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {k.scope}
                      </TableCell>
                      <TableCell className="text-sg-ink-2">
                        {k.label ?? "—"}
                      </TableCell>
                      <TableCell className="text-sg-ink-3">
                        {fmtMs(k.created_at_ms)}
                      </TableCell>
                      <TableCell className="text-sg-ink-3">
                        {fmtMs(k.last_used_at_ms)}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setPendingRevoke(k)}
                          data-testid={`api-key-revoke-${k.key_id}`}
                          aria-label={t("apiKeys.revoke")}
                        >
                          <Trash2 className="h-3.5 w-3.5" aria-hidden />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </>
        )}
      </CardContent>

      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRevoke(null);
        }}
        title={t("apiKeys.revokeTitle")}
        description={
          pendingRevoke
            ? t("apiKeys.revokeConfirm", { keyId: pendingRevoke.key_id })
            : ""
        }
        confirmLabel={t("apiKeys.revoke")}
        cancelLabel={t("common.cancel")}
        onConfirm={onRevokeConfirm}
        busy={revoking}
        testId="api-key-revoke-confirm"
      />
    </Card>
  );
}

interface MintedCallout {
  token: string;
  keyId: string;
}

function FreshTokenCallout({
  token,
  onDismiss,
}: {
  token: string;
  onDismiss: () => void;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  function onCopy() {
    void navigator.clipboard?.writeText(token).then(() => {
      setCopied(true);
      toast.success(t("apiKeys.copied"));
      window.setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div
      role="status"
      data-testid="api-key-fresh-token"
      className="space-y-3 rounded-sg-md border border-sg-accent/30 bg-sg-accent-soft p-4 text-sm"
    >
      <p className="font-medium text-sg-ink">{t("apiKeys.tokenOnce")}</p>
      <div className="flex items-center gap-2">
        <code
          data-testid="api-key-token-value"
          className="flex-1 overflow-x-auto rounded-md bg-sg-inset px-3 py-2 font-mono text-xs text-sg-ink"
        >
          {token}
        </code>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onCopy}
          data-testid="api-key-token-copy"
          aria-label={t("apiKeys.copy")}
        >
          {copied ? (
            <Check className="h-3.5 w-3.5" aria-hidden />
          ) : (
            <Copy className="h-3.5 w-3.5" aria-hidden />
          )}
        </Button>
      </div>
      <div className="flex justify-end">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={onDismiss}
          data-testid="api-key-token-dismiss"
        >
          {t("apiKeys.dismiss")}
        </Button>
      </div>
    </div>
  );
}

function mintError(
  err: unknown,
  t: ReturnType<typeof useTranslation>["t"],
): string {
  if (err instanceof CorlinmanApiError) {
    if (err.status === 503) return t("apiKeys.disabled");
    if (err.status === 400) return t("apiKeys.scopeRequired");
    return t("apiKeys.mintFailed", { msg: err.message });
  }
  return t("apiKeys.mintFailed", { msg: String(err) });
}
