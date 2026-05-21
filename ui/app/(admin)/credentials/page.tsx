"use client";

/**
 * /credentials — provider-credential manager (Wave 2.3, extended W-A2 → W-A3).
 *
 * Builds on top of `/admin/credentials*` (see
 * `gateway/routes_admin_b/credentials.py`). Borrows hermes-agent's
 * EnvPage UX:
 *   - provider-grouped sections (collapsed-by-default when empty),
 *   - per-row eye-icon reveal of the "…last4" preview,
 *   - paste-only inputs with a soft "paste, don't type" nudge,
 *   - destructive ops gated behind a confirmation dialog,
 *   - toasts on every mutation.
 *
 * Plaintext values never leave the gateway; the page only ever asks the
 * server to redact + return previews. Reveal toggles the masked display
 * between "••••••••" and "…xyz9", never the full literal.
 *
 * The OAuth panel near the top renders five tiles:
 *   - Anthropic (PKCE) + Claude Code (one-shot import)  — W-A2
 *   - Codex + Gemini (external CLI detection, read-only) — W-A3
 *   - xAI (PKCE)                                         — W-A3
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  Download,
  KeyRound,
  LogIn,
  Plug,
  RefreshCw,
  Search,
  ShieldCheck,
  Unplug,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { EnvVarRow } from "@/components/credentials/env-var-row";
import {
  OAuthLoginModal,
  type OAuthLoginProvider,
} from "@/components/admin/oauth-login-modal";
import {
  cancelClaudeCodeLogin,
  CorlinmanApiError,
  deleteCredential,
  disconnectAnthropicOAuth,
  disconnectCodexOAuth,
  disconnectGeminiOAuth,
  disconnectXaiOAuth,
  getOAuthStatus,
  importClaudeCodeCredentials,
  launchClaudeCodeLogin,
  listCredentials,
  refreshAnthropicOAuth,
  refreshCodexOAuth,
  refreshGeminiOAuth,
  refreshXaiOAuth,
  submitClaudeCodeLogin,
  setCredential,
  setProviderEnabled,
  type CredentialProvider,
  type OAuthProviderStatus,
  type OAuthSource,
} from "@/lib/api";

const FIELD_LABEL_KEYS: Record<string, string> = {
  api_key: "credentials.fieldKeyApiKey",
  base_url: "credentials.fieldKeyBaseUrl",
  org_id: "credentials.fieldKeyOrgId",
  kind: "credentials.fieldKeyKind",
};

function isProviderConfigured(p: CredentialProvider): boolean {
  return p.fields.some((f) => f.set);
}

// --- OAuth section helpers --------------------------------------------------

const OAUTH_POLL_INTERVAL_MS = 30_000;
const EMPTY_CREDENTIAL_PROVIDERS: CredentialProvider[] = [];

/** Translation hook for the i18n `oauth.expiresIn*` keys. Pure — the
 * `t` instance is passed in so this stays unit-testable. */
function formatExpiresIn(
  t: (key: string, vars?: Record<string, unknown>) => string,
  seconds: number | null,
): string | null {
  if (seconds === null || seconds === undefined) return null;
  if (seconds <= 0) return t("oauth.expired");
  if (seconds < 60) return t("oauth.expiresInSeconds", { n: seconds });
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return t("oauth.expiresInMinutes", { n: mins });
  const hours = Math.floor(mins / 60);
  if (hours < 48) return t("oauth.expiresInHours", { n: hours });
  const days = Math.floor(hours / 24);
  return t("oauth.expiresInDays", { n: days });
}

function describeSource(
  t: (key: string) => string,
  source: OAuthSource | "external-cli",
): { label: string; tone: "ok" | "muted" | "warn" } {
  switch (source) {
    case "pkce":
      return { label: t("oauth.sourcePkce"), tone: "ok" };
    case "claude-code":
      return { label: t("oauth.sourceClaudeCode"), tone: "ok" };
    case "external-cli":
      return { label: t("oauth.sourceExternalCli"), tone: "ok" };
    case "env":
      return { label: t("oauth.sourceEnv"), tone: "muted" };
    case "api-key":
      return { label: t("oauth.sourceApiKey"), tone: "muted" };
    case "none":
    default:
      return { label: t("oauth.sourceNone"), tone: "warn" };
  }
}

function findProvider(
  status: { providers: OAuthProviderStatus[] } | undefined,
  id: string,
): OAuthProviderStatus | null {
  if (!status) return null;
  return status.providers.find((p) => p.id === id) ?? null;
}

export default function CredentialsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [search, setSearch] = React.useState("");
  const [showEmpty, setShowEmpty] = React.useState(true);
  const [pendingDelete, setPendingDelete] = React.useState<{
    provider: string;
    key: string;
  } | null>(null);

  const credentials = useQuery({
    queryKey: ["admin", "credentials"],
    queryFn: listCredentials,
    retry: false,
  });

  const saveField = useMutation({
    mutationFn: async (vars: {
      provider: string;
      key: string;
      value: string;
    }) => setCredential(vars.provider, vars.key, vars.value),
    onSuccess: (_data, vars) => {
      toast.success(t("credentials.fieldSaved", { key: vars.key }));
      qc.invalidateQueries({ queryKey: ["admin", "credentials"] });
    },
    onError: (err, vars) => {
      if (err instanceof CorlinmanApiError && err.status === 400) {
        toast.error(t("credentials.unknownField", { key: vars.key }));
        return;
      }
      toast.error(
        t("credentials.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const removeField = useMutation({
    mutationFn: async (vars: { provider: string; key: string }) =>
      deleteCredential(vars.provider, vars.key),
    onSuccess: (_data, vars) => {
      toast.success(t("credentials.fieldDeleted", { key: vars.key }));
      setPendingDelete(null);
      qc.invalidateQueries({ queryKey: ["admin", "credentials"] });
    },
    onError: (err) => {
      toast.error(
        t("credentials.deleteFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const toggleProvider = useMutation({
    mutationFn: async (vars: { provider: string; enabled: boolean }) =>
      setProviderEnabled(vars.provider, vars.enabled),
    onSuccess: (_data, vars) => {
      toast.success(
        t(
          vars.enabled
            ? "credentials.providerEnabled"
            : "credentials.providerDisabled",
          { provider: vars.provider },
        ),
      );
      qc.invalidateQueries({ queryKey: ["admin", "credentials"] });
    },
    onError: (err) => {
      toast.error(
        t("credentials.saveFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  // --- OAuth panel state ---------------------------------------------------
  /** Which provider's modal is open. `null` means closed. */
  const [oauthModalProvider, setOauthModalProvider] =
    React.useState<OAuthLoginProvider | null>(null);
  /** Disconnect dialog is keyed by provider id so we can re-use one
   * <Dialog> for both anthropic and xai. */
  const [pendingDisconnect, setPendingDisconnect] = React.useState<
    null | "anthropic" | "xai" | "codex" | "gemini"
  >(null);

  const oauthStatus = useQuery({
    queryKey: ["admin", "oauth", "status"],
    queryFn: ({ signal }) => getOAuthStatus({ signal }),
    retry: false,
    refetchInterval: OAUTH_POLL_INTERVAL_MS,
    refetchOnWindowFocus: false,
  });

  const importClaudeCode = useMutation({
    mutationFn: () => importClaudeCodeCredentials(),
    onSuccess: () => {
      toast.success(t("oauth.importSuccess"));
      qc.invalidateQueries({ queryKey: ["admin", "oauth", "status"] });
    },
    onError: (err) => {
      if (err instanceof CorlinmanApiError && err.status === 404) {
        toast.error(t("oauth.importNotFound"));
        return;
      }
      toast.error(
        t("oauth.importFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  // Claude Code subprocess-login modal state.
  const [claudeLogin, setClaudeLogin] = React.useState<{
    session_id: string;
    auth_url: string;
  } | null>(null);
  const [claudeLoginCode, setClaudeLoginCode] = React.useState("");

  const launchClaudeCode = useMutation({
    mutationFn: () => launchClaudeCodeLogin(),
    onSuccess: (res) => {
      setClaudeLogin(res);
      setClaudeLoginCode("");
    },
    onError: (err) => {
      toast.error(
        err instanceof Error ? err.message : String(err),
      );
    },
  });

  const submitClaudeCode = useMutation({
    mutationFn: (code: string) => {
      if (!claudeLogin) throw new Error("no_session");
      return submitClaudeCodeLogin({
        session_id: claudeLogin.session_id,
        code,
      });
    },
    onSuccess: () => {
      toast.success(t("oauth.importSuccess"));
      setClaudeLogin(null);
      setClaudeLoginCode("");
      qc.invalidateQueries({ queryKey: ["admin", "oauth", "status"] });
    },
    onError: (err) => {
      toast.error(
        err instanceof Error ? err.message : String(err),
      );
    },
  });

  const dismissClaudeLogin = React.useCallback(() => {
    if (claudeLogin) {
      void cancelClaudeCodeLogin({ session_id: claudeLogin.session_id });
    }
    setClaudeLogin(null);
    setClaudeLoginCode("");
  }, [claudeLogin]);

  const refreshAnthropic = useMutation({
    mutationFn: () => refreshAnthropicOAuth(),
    onSuccess: () => {
      toast.success(
        t("oauth.refreshSuccess", { provider: t("oauth.providerAnthropic") }),
      );
      qc.invalidateQueries({ queryKey: ["admin", "oauth", "status"] });
    },
    onError: (err) => {
      toast.error(
        t("oauth.refreshFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const disconnectAnthropic = useMutation({
    mutationFn: () => disconnectAnthropicOAuth(),
    onSuccess: () => {
      toast.success(
        t("oauth.disconnectSuccess", {
          provider: t("oauth.providerAnthropic"),
        }),
      );
      setPendingDisconnect(null);
      qc.invalidateQueries({ queryKey: ["admin", "oauth", "status"] });
    },
    onError: (err) => {
      toast.error(
        t("oauth.disconnectFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const refreshXai = useMutation({
    mutationFn: () => refreshXaiOAuth(),
    onSuccess: () => {
      toast.success(
        t("oauth.refreshSuccess", { provider: t("oauth.providerXai") }),
      );
      qc.invalidateQueries({ queryKey: ["admin", "oauth", "status"] });
    },
    onError: (err) => {
      toast.error(
        t("oauth.refreshFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  const disconnectXai = useMutation({
    mutationFn: () => disconnectXaiOAuth(),
    onSuccess: () => {
      toast.success(
        t("oauth.disconnectSuccess", { provider: t("oauth.providerXai") }),
      );
      setPendingDisconnect(null);
      qc.invalidateQueries({ queryKey: ["admin", "oauth", "status"] });
    },
    onError: (err) => {
      toast.error(
        t("oauth.disconnectFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  // Codex / Gemini PKCE — same shape as xAI/Anthropic.
  const oauthInvalidate = () => {
    qc.invalidateQueries({ queryKey: ["admin", "oauth", "status"] });
    qc.invalidateQueries({ queryKey: ["admin", "oauth", "codex"] });
    qc.invalidateQueries({ queryKey: ["admin", "oauth", "gemini"] });
  };
  const refreshCodex = useMutation({
    mutationFn: () => refreshCodexOAuth(),
    onSuccess: () => {
      toast.success(
        t("oauth.refreshSuccess", { provider: t("oauth.providerCodex") }),
      );
      oauthInvalidate();
    },
    onError: (err) =>
      toast.error(
        t("oauth.refreshFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });
  const disconnectCodex = useMutation({
    mutationFn: () => disconnectCodexOAuth(),
    onSuccess: () => {
      toast.success(
        t("oauth.disconnectSuccess", { provider: t("oauth.providerCodex") }),
      );
      setPendingDisconnect(null);
      oauthInvalidate();
    },
    onError: (err) =>
      toast.error(
        t("oauth.disconnectFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });
  const refreshGemini = useMutation({
    mutationFn: () => refreshGeminiOAuth(),
    onSuccess: () => {
      toast.success(
        t("oauth.refreshSuccess", { provider: t("oauth.providerGemini") }),
      );
      oauthInvalidate();
    },
    onError: (err) =>
      toast.error(
        t("oauth.refreshFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });
  const disconnectGemini = useMutation({
    mutationFn: () => disconnectGeminiOAuth(),
    onSuccess: () => {
      toast.success(
        t("oauth.disconnectSuccess", { provider: t("oauth.providerGemini") }),
      );
      setPendingDisconnect(null);
      oauthInvalidate();
    },
    onError: (err) =>
      toast.error(
        t("oauth.disconnectFailed", {
          msg: err instanceof Error ? err.message : String(err),
        }),
      ),
  });

  const anthropic = findProvider(oauthStatus.data, "anthropic");
  const xai = findProvider(oauthStatus.data, "xai");
  const codex = findProvider(oauthStatus.data, "codex");
  const gemini = findProvider(oauthStatus.data, "gemini");
  const anthropicLoggedIn = anthropic?.source === "pkce";
  const xaiLoggedIn = xai?.source === "pkce";
  // Codex / Gemini surface as `external-cli` once their auth files exist
  // on disk — regardless of whether `codex login` or our PKCE wrote them.
  const codexLoggedIn = codex?.source === "external-cli";
  const geminiLoggedIn = gemini?.source === "external-cli";

  const providers = credentials.data?.providers ?? EMPTY_CREDENTIAL_PROVIDERS;

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return providers.filter((p) => {
      if (!showEmpty && !isProviderConfigured(p)) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) || p.kind.toLowerCase().includes(q)
      );
    });
  }, [providers, search, showEmpty]);

  const total = providers.length;
  const configured = providers.filter(isProviderConfigured).length;

  const disconnectActive =
    pendingDisconnect === "anthropic"
      ? disconnectAnthropic
      : pendingDisconnect === "xai"
        ? disconnectXai
        : pendingDisconnect === "codex"
          ? disconnectCodex
          : pendingDisconnect === "gemini"
            ? disconnectGemini
            : null;
  const disconnectProviderLabel =
    pendingDisconnect === "anthropic"
      ? t("oauth.providerAnthropic")
      : pendingDisconnect === "xai"
        ? t("oauth.providerXai")
        : pendingDisconnect === "codex"
          ? t("oauth.providerCodex")
          : pendingDisconnect === "gemini"
            ? t("oauth.providerGemini")
            : "";

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <KeyRound className="h-5 w-5 text-tp-ink-3" aria-hidden />
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("credentials.title")}
          </h1>
        </div>
        <p className="text-sm text-tp-ink-3">{t("credentials.subtitle")}</p>
        <p
          className="text-xs text-tp-ink-3"
          data-testid="credentials-count-summary"
        >
          {t("credentials.countSummary", { total, configured })}
        </p>
      </header>

      <Card data-testid="oauth-panel">
        <CardHeader className="border-b border-tp-glass-edge">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-tp-ink-3" aria-hidden />
            <CardTitle className="text-base">{t("oauth.panelTitle")}</CardTitle>
          </div>
          <CardDescription>{t("oauth.panelDescription")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 pt-4 md:grid-cols-2">
          {/* --- Anthropic tile --- */}
          <OAuthPkceTile
            testId="oauth-tile-anthropic"
            providerLabel={t("oauth.providerAnthropic")}
            loading={oauthStatus.isPending}
            errored={oauthStatus.isError}
            status={anthropic}
            loggedIn={anthropicLoggedIn}
            t={t}
            onLogin={() => setOauthModalProvider("anthropic")}
            onRefresh={() => refreshAnthropic.mutate()}
            refreshing={refreshAnthropic.isPending}
            onDisconnect={() => setPendingDisconnect("anthropic")}
          />

          {/* --- Claude Code import tile --- */}
          <div
            className="flex flex-col gap-3 rounded-md border border-tp-glass-edge bg-tp-glass p-3 shadow-tp-panel backdrop-blur-glass backdrop-saturate-glass"
            data-testid="oauth-tile-claude-code"
          >
            <div className="flex flex-col gap-1">
              <span className="font-medium">
                {t("oauth.providerClaudeCode")}
              </span>
              <span className="text-[11px] text-tp-ink-3">
                {t("oauth.claudeCodeHint")}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                disabled={launchClaudeCode.isPending || submitClaudeCode.isPending}
                onClick={() => launchClaudeCode.mutate()}
                data-testid="oauth-tile-claude-code-login"
              >
                <LogIn className="h-4 w-4" aria-hidden />
                {t("oauth.actionLogin")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={importClaudeCode.isPending}
                onClick={() => importClaudeCode.mutate()}
                data-testid="oauth-tile-claude-code-import"
              >
                <Download className="h-4 w-4" aria-hidden />
                {t("oauth.actionImport")}
              </Button>
              {anthropic?.source === "claude-code" && (
                <Badge className="border-transparent bg-ok/15 text-ok">
                  {t("oauth.badgeDetected")}
                </Badge>
              )}
            </div>
          </div>

          {/* --- Codex (PKCE port of `codex login`) --- */}
          <OAuthPkceTile
            testId="oauth-tile-codex"
            providerLabel={t("oauth.providerCodex")}
            loading={oauthStatus.isPending}
            errored={oauthStatus.isError}
            status={codex}
            loggedIn={codexLoggedIn}
            t={t}
            onLogin={() => setOauthModalProvider("codex")}
            onRefresh={() => refreshCodex.mutate()}
            refreshing={refreshCodex.isPending}
            onDisconnect={() => setPendingDisconnect("codex")}
          />

          {/* --- Gemini (PKCE port of `gemini auth login`) --- */}
          <OAuthPkceTile
            testId="oauth-tile-gemini"
            providerLabel={t("oauth.providerGemini")}
            loading={oauthStatus.isPending}
            errored={oauthStatus.isError}
            status={gemini}
            loggedIn={geminiLoggedIn}
            t={t}
            onLogin={() => setOauthModalProvider("gemini")}
            onRefresh={() => refreshGemini.mutate()}
            refreshing={refreshGemini.isPending}
            onDisconnect={() => setPendingDisconnect("gemini")}
          />

          {/* --- xAI (PKCE) --- */}
          <OAuthPkceTile
            testId="oauth-tile-xai"
            providerLabel={t("oauth.providerXai")}
            loading={oauthStatus.isPending}
            errored={oauthStatus.isError}
            status={xai}
            loggedIn={xaiLoggedIn}
            t={t}
            onLogin={() => setOauthModalProvider("xai")}
            onRefresh={() => refreshXai.mutate()}
            refreshing={refreshXai.isPending}
            onDisconnect={() => setPendingDisconnect("xai")}
          />
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px] max-w-md">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tp-ink-3"
            aria-hidden
          />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("credentials.search")}
            className="pl-8"
            data-testid="credentials-search"
          />
        </div>
        <label className="flex items-center gap-2 text-xs text-tp-ink-2">
          <Switch
            checked={showEmpty}
            onCheckedChange={setShowEmpty}
            aria-label={t("credentials.showEmpty")}
            data-testid="credentials-show-empty"
          />
          <span>{t("credentials.showEmpty")}</span>
        </label>
      </div>

      {credentials.isPending ? (
        <Skeleton className="h-40 w-full" />
      ) : credentials.isError ? (
        <p className="text-xs text-destructive" data-testid="credentials-error">
          {t("credentials.loadFailed")}:{" "}
          {credentials.error instanceof Error
            ? credentials.error.message
            : String(credentials.error)}
        </p>
      ) : filtered.length === 0 ? (
        <Card data-testid="credentials-empty">
          <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
            <Plug className="h-6 w-6 text-tp-ink-3" aria-hidden />
            <p className="text-sm text-tp-ink-3">
              {t("credentials.emptyState")}
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {filtered.map((p) => {
            const configuredFields = p.fields.filter((f) => f.set).length;
            const totalFields = p.fields.length;
            return (
              <Card
                key={p.name}
                data-testid={`credentials-provider-${p.name}`}
              >
                <CardHeader className="border-b border-tp-glass-edge">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <CardTitle className="text-base capitalize">
                        {p.name}
                      </CardTitle>
                      <Badge variant="secondary" className="font-mono text-[10px]">
                        {p.kind}
                      </Badge>
                      {p.enabled ? (
                        <Badge className="border-transparent bg-ok/15 text-ok">
                          {t("common.enabled")}
                        </Badge>
                      ) : (
                        <Badge variant="secondary">{t("common.disabled")}</Badge>
                      )}
                    </div>
                    <div className="flex w-full items-center justify-between gap-3 sm:w-auto sm:justify-end">
                      <CardDescription
                        data-testid={`credentials-provider-${p.name}-count`}
                      >
                        {t("credentials.countConfigured", {
                          configured: configuredFields,
                          total: totalFields,
                        })}
                      </CardDescription>
                      <Switch
                        checked={p.enabled}
                        onCheckedChange={(next) =>
                          toggleProvider.mutate({
                            provider: p.name,
                            enabled: next,
                          })
                        }
                        aria-label={
                          p.enabled
                            ? t("credentials.providerDisabled", {
                                provider: p.name,
                              })
                            : t("credentials.providerEnabled", {
                                provider: p.name,
                              })
                        }
                        data-testid={`credentials-provider-${p.name}-toggle`}
                      />
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="grid gap-2 pt-3">
                  {p.fields.length === 0 ? (
                    <p className="text-[11px] text-tp-ink-3">
                      {t("credentials.fieldUnset")}
                    </p>
                  ) : (
                    p.fields.map((f) => {
                      const labelKey = FIELD_LABEL_KEYS[f.key];
                      return (
                        <EnvVarRow
                          key={f.key}
                          provider={p.name}
                          field={f}
                          label={labelKey ? t(labelKey) : f.key}
                          saving={
                            (saveField.isPending &&
                              saveField.variables?.provider === p.name &&
                              saveField.variables?.key === f.key) ||
                            (removeField.isPending &&
                              removeField.variables?.provider === p.name &&
                              removeField.variables?.key === f.key)
                          }
                          onSave={async (value) => {
                            await saveField.mutateAsync({
                              provider: p.name,
                              key: f.key,
                              value,
                            });
                          }}
                          onDelete={() =>
                            setPendingDelete({
                              provider: p.name,
                              key: f.key,
                            })
                          }
                        />
                      );
                    })
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <OAuthLoginModal
        open={oauthModalProvider !== null}
        provider={oauthModalProvider ?? "anthropic"}
        onOpenChange={(open) => {
          if (!open) setOauthModalProvider(null);
        }}
        onSuccess={() =>
          qc.invalidateQueries({ queryKey: ["admin", "oauth", "status"] })
        }
      />

      {/* Claude Code subprocess-login modal. Open after the launch
          mutation returns; user opens the URL on their own device,
          completes OAuth, pastes the code back here. */}
      <Dialog
        open={claudeLogin !== null}
        onOpenChange={(open) => {
          if (!open) dismissClaudeLogin();
        }}
      >
        <DialogContent data-testid="claude-code-login-dialog">
          <DialogHeader>
            <DialogTitle>{t("oauth.claudeLoginTitle")}</DialogTitle>
            <DialogDescription>
              {t("oauth.claudeLoginBody")}
            </DialogDescription>
          </DialogHeader>
          {claudeLogin && (
            <div className="flex flex-col gap-3">
              <div className="flex flex-col gap-1">
                <span className="text-xs text-tp-ink-3">
                  {t("oauth.claudeLoginUrlLabel")}
                </span>
                <div className="flex items-center gap-2">
                  <Input
                    readOnly
                    value={claudeLogin.auth_url}
                    onFocus={(e) => e.target.select()}
                    className="font-mono text-xs"
                    data-testid="claude-code-login-url"
                  />
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      void navigator.clipboard.writeText(claudeLogin.auth_url);
                      toast.success(t("oauth.claudeLoginCopied"));
                    }}
                  >
                    {t("oauth.actionCopy")}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      window.open(
                        claudeLogin.auth_url,
                        "_blank",
                        "noopener,noreferrer",
                      )
                    }
                  >
                    {t("oauth.actionOpen")}
                  </Button>
                </div>
              </div>
              <div className="flex flex-col gap-1">
                <span className="text-xs text-tp-ink-3">
                  {t("oauth.claudeLoginCodeLabel")}
                </span>
                <Input
                  placeholder={t("oauth.claudeLoginCodePlaceholder")}
                  value={claudeLoginCode}
                  onChange={(e) => setClaudeLoginCode(e.target.value)}
                  data-testid="claude-code-login-code"
                />
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={dismissClaudeLogin}
              disabled={submitClaudeCode.isPending}
            >
              {t("common.cancel")}
            </Button>
            <Button
              disabled={
                submitClaudeCode.isPending || claudeLoginCode.trim() === ""
              }
              onClick={() => submitClaudeCode.mutate(claudeLoginCode)}
              data-testid="claude-code-login-submit"
            >
              {submitClaudeCode.isPending
                ? t("oauth.claudeLoginSubmitting")
                : t("oauth.claudeLoginSubmit")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={pendingDisconnect !== null}
        onOpenChange={(o) => {
          if (!o && !disconnectActive?.isPending) setPendingDisconnect(null);
        }}
      >
        <DialogContent data-testid="oauth-disconnect-dialog">
          <DialogHeader>
            <DialogTitle>
              {t("oauth.disconnectTitle", { provider: disconnectProviderLabel })}
            </DialogTitle>
            <DialogDescription>{t("oauth.disconnectBody")}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPendingDisconnect(null)}
              data-testid="oauth-disconnect-cancel"
            >
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={disconnectActive?.isPending}
              onClick={() => disconnectActive?.mutate()}
              data-testid="oauth-disconnect-confirm"
            >
              {t("oauth.disconnectConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!pendingDelete}
        onOpenChange={(o) => {
          if (!o) setPendingDelete(null);
        }}
      >
        <DialogContent data-testid="credentials-delete-dialog">
          <DialogHeader>
            <DialogTitle>
              {pendingDelete
                ? t("credentials.deleteConfirmTitle", {
                    provider: pendingDelete.provider,
                    key: pendingDelete.key,
                  })
                : ""}
            </DialogTitle>
            <DialogDescription>
              {pendingDelete
                ? t("credentials.deleteConfirm", {
                    provider: pendingDelete.provider,
                    key: pendingDelete.key,
                  })
                : ""}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPendingDelete(null)}
              data-testid="credentials-delete-cancel"
            >
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              data-testid="credentials-delete-confirm"
              disabled={removeField.isPending}
              onClick={() => {
                if (pendingDelete) removeField.mutate(pendingDelete);
              }}
            >
              {t("common.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tile sub-components.
//
// Lifted inline so the main render stays scannable; both tile shapes are
// stateless presentation — every mutation is driven by the parent's
// useMutation hooks. PkceTile drives the Anthropic, Codex, Gemini, and xAI rows.
// ---------------------------------------------------------------------------

interface PkceTileProps {
  testId: string;
  providerLabel: string;
  loading: boolean;
  errored: boolean;
  status: OAuthProviderStatus | null;
  loggedIn: boolean;
  refreshing: boolean;
  t: (key: string, vars?: Record<string, unknown>) => string;
  onLogin: () => void;
  onRefresh: () => void;
  onDisconnect: () => void;
}

function OAuthPkceTile({
  testId,
  providerLabel,
  loading,
  errored,
  status,
  loggedIn,
  refreshing,
  t,
  onLogin,
  onRefresh,
  onDisconnect,
}: PkceTileProps) {
  return (
    <div
      className="flex flex-col gap-3 rounded-md border border-tp-glass-edge bg-tp-glass p-3 shadow-tp-panel backdrop-blur-glass backdrop-saturate-glass"
      data-testid={testId}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-col gap-1">
          <span className="font-medium">{providerLabel}</span>
          {loading ? (
            <Skeleton className="h-4 w-32" />
          ) : errored ? (
            <Badge variant="secondary" className="self-start">
              {t("oauth.statusUnavailable")}
            </Badge>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              {(() => {
                const desc = describeSource(t, status?.source ?? "none");
                return (
                  <Badge
                    className={
                      desc.tone === "ok"
                        ? "border-transparent bg-ok/15 text-ok"
                        : desc.tone === "warn"
                          ? "border-transparent bg-destructive/15 text-destructive"
                          : "bg-secondary text-secondary-foreground"
                    }
                    data-testid={`${testId}-status`}
                  >
                    {desc.label}
                  </Badge>
                );
              })()}
              {status?.expires_in_seconds != null && (
                <span className="text-[11px] text-tp-ink-3">
                  {formatExpiresIn(t, status.expires_in_seconds)}
                </span>
              )}
              {status?.username && (
                <span className="text-[11px] text-tp-ink-3">
                  {status.username}
                </span>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {!loggedIn && (
          <Button
            size="sm"
            onClick={onLogin}
            data-testid={`${testId}-login`}
          >
            <LogIn className="h-4 w-4" aria-hidden />
            {t("oauth.actionLogin")}
          </Button>
        )}
        {loggedIn && (
          <>
            <Button
              size="sm"
              variant="outline"
              disabled={refreshing}
              onClick={onRefresh}
              data-testid={`${testId}-refresh`}
            >
              <RefreshCw
                className={refreshing ? "h-4 w-4 animate-spin" : "h-4 w-4"}
                aria-hidden
              />
              {t("oauth.actionRefresh")}
            </Button>
            <Button
              size="sm"
              variant="destructive"
              onClick={onDisconnect}
              data-testid={`${testId}-disconnect`}
            >
              <Unplug className="h-4 w-4" aria-hidden />
              {t("oauth.actionDisconnect")}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
