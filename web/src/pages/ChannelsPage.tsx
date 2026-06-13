import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ExternalLink,
  PlugZap,
  QrCode,
  Radio,
  RotateCw,
  Save,
  Settings2,
  WifiOff,
  X,
} from "lucide-react";
import * as QRCode from "qrcode";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { api } from "@/lib/api";
import type {
  MessagingPlatform,
  MessagingPlatformEnvVar,
  MessagingPlatformUpdate,
  TelegramOnboardingStartResponse,
  WhatsappOnboardingStartResponse,
  WhatsappOnboardingStatusResponse,
  WhatsappOnboardingMode,
} from "@/lib/api";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn, themedBody } from "@/lib/utils";

// State → badge mapping. The backend emits a small, fixed vocabulary plus
// whatever the live gateway runtime reports (connected/disconnected/fatal).
const STATE_BADGE: Record<
  string,
  { tone: "success" | "warning" | "destructive" | "secondary" | "outline"; label: string }
> = {
  connected: { tone: "success", label: "Connected" },
  pending_restart: { tone: "warning", label: "Restart to apply" },
  gateway_stopped: { tone: "warning", label: "Gateway stopped" },
  disconnected: { tone: "warning", label: "Disconnected" },
  not_configured: { tone: "outline", label: "Not configured" },
  disabled: { tone: "secondary", label: "Disabled" },
  fatal: { tone: "destructive", label: "Error" },
};

function stateBadge(state: string) {
  return STATE_BADGE[state] ?? { tone: "outline" as const, label: state };
}

const TELEGRAM_USER_ID_RE = /^\d+$/;

function formatExpiry(expiresAt: string): string {
  const ms = Date.parse(expiresAt) - Date.now();
  if (!Number.isFinite(ms) || ms <= 0) return "expired";
  const seconds = Math.ceil(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${rest.toString().padStart(2, "0")}`;
}

function isTerminalTelegramOnboardingError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /\b410\b/.test(message) && /\b(expired|claimed|gone)\b/i.test(message);
}

export default function ChannelsPage() {
  const [platforms, setPlatforms] = useState<MessagingPlatform[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();
  const { setEnd } = usePageHeader();

  // Config modal state
  const [editing, setEditing] = useState<MessagingPlatform | null>(null);
  const [draftEnv, setDraftEnv] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const closeEdit = useCallback(() => setEditing(null), []);
  const editModalRef = useModalBehavior({ open: editing !== null, onClose: closeEdit });

  // Per-card busy + restart-needed tracking
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [restartNeeded, setRestartNeeded] = useState(false);
  const [restarting, setRestarting] = useState(false);

  const gatewayRunning = platforms.length > 0 && platforms[0].gateway_running;

  const load = useCallback(() => {
    return api
      .getMessagingPlatforms()
      .then((res) => setPlatforms(res.platforms))
      .catch((e) => showToast(`Error: ${e}`, "error"));
  }, [showToast]);

  useEffect(() => {
    load().finally(() => setLoading(false));
  }, [load]);

  const openConfig = (platform: MessagingPlatform) => {
    const initial: Record<string, string> = {};
    platform.env_vars.forEach((v) => {
      initial[v.key] = "";
    });
    setDraftEnv(initial);
    setEditing(platform);
  };

  const handleSave = async () => {
    if (!editing) return;
    // Only send fields the user actually filled in — leaving a field blank
    // preserves the existing value rather than clobbering it.
    const env: Record<string, string> = {};
    Object.entries(draftEnv).forEach(([k, v]) => {
      if (v.trim()) env[k] = v.trim();
    });
    if (Object.keys(env).length === 0) {
      showToast("Nothing to save — fill in at least one field.", "error");
      return;
    }
    const missing = editing.env_vars.filter(
      (v) => v.required && !v.is_set && !env[v.key],
    );
    if (missing.length > 0) {
      showToast(`${missing[0].prompt || missing[0].key} is required`, "error");
      return;
    }
    setSaving(true);
    try {
      const body: MessagingPlatformUpdate = { env, enabled: true };
      await api.updateMessagingPlatform(editing.id, body);
      showToast(`${editing.name} saved`, "success");
      setEditing(null);
      setRestartNeeded(true);
      await load();
    } catch (e) {
      showToast(`Failed to save: ${e}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = async (platform: MessagingPlatform) => {
    const next = !platform.enabled;
    setTogglingId(platform.id);
    try {
      await api.updateMessagingPlatform(platform.id, { enabled: next });
      setPlatforms((prev) =>
        prev.map((p) =>
          p.id === platform.id
            ? { ...p, enabled: next, state: next ? "pending_restart" : "disabled" }
            : p,
        ),
      );
      setRestartNeeded(true);
    } catch (e) {
      showToast(`Error: ${e}`, "error");
    } finally {
      setTogglingId(null);
    }
  };

  const handleTest = async (platform: MessagingPlatform) => {
    setTestingId(platform.id);
    try {
      const res = await api.testMessagingPlatform(platform.id);
      showToast(`${platform.name}: ${res.message}`, res.ok ? "success" : "error");
    } catch (e) {
      showToast(`Error: ${e}`, "error");
    } finally {
      setTestingId(null);
    }
  };

  const handleRestart = async () => {
    setRestarting(true);
    try {
      await api.restartGateway();
      showToast("Gateway restarting…", "success");
      setRestartNeeded(false);
      // Give the gateway a moment to come up, then refresh status.
      setTimeout(() => void load(), 4000);
    } catch (e) {
      showToast(`Failed to restart: ${e}`, "error");
    } finally {
      setRestarting(false);
    }
  };

  useLayoutEffect(() => {
    setEnd(
      <Button
        className="uppercase"
        size="sm"
        onClick={handleRestart}
        disabled={restarting}
        prefix={restarting ? <Spinner /> : <RotateCw className="h-4 w-4" />}
      >
        {restarting ? "Restarting…" : "Restart gateway"}
      </Button>,
    );
    return () => setEnd(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setEnd, restarting]);

  const configured = useMemo(
    () => platforms.filter((p) => p.configured).length,
    [platforms],
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      {/* Restart banner */}
      {restartNeeded && (
        <Card className="border-warning/50">
          <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2 text-sm">
              <AlertTriangle className="h-4 w-4 shrink-0 text-warning" />
              <span>
                Changes are saved. Restart the gateway for them to take effect.
              </span>
            </div>
            <Button
              size="sm"
              className="uppercase shrink-0"
              onClick={handleRestart}
              disabled={restarting}
              prefix={restarting ? <Spinner /> : <RotateCw className="h-4 w-4" />}
            >
              {restarting ? "Restarting…" : "Restart now"}
            </Button>
          </CardContent>
        </Card>
      )}

      {!gatewayRunning && !restartNeeded && (
        <Card className="border-border">
          <CardContent className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
            <WifiOff className="h-4 w-4 shrink-0" />
            <span>
              The gateway is not running. Configure channels here, then start the
              gateway with <code className="font-courier">hermes gateway start</code>{" "}
              (or the Restart button above).
            </span>
          </CardContent>
        </Card>
      )}

      <p className="text-xs text-muted-foreground">
        {configured} of {platforms.length} channels configured. Credentials are
        written to <code className="font-courier">~/.hermes/.env</code>; the
        gateway connects each enabled channel on its next restart.
      </p>

      {/* Config modal */}
      {editing && (
        <div
          ref={editModalRef}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 backdrop-blur-sm p-4"
          onClick={(e) => e.target === e.currentTarget && setEditing(null)}
          role="dialog"
          aria-modal="true"
          aria-labelledby="channel-config-title"
        >
          <div
            className={cn(
              themedBody,
              "relative w-full max-w-lg border border-border bg-card shadow-2xl flex flex-col max-h-[90vh]",
            )}
          >
            <Button
              ghost
              size="icon"
              onClick={() => setEditing(null)}
              className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
              aria-label="Close"
            >
              <X />
            </Button>

            <header className="p-5 pb-3 border-b border-border">
              <h2
                id="channel-config-title"
                className="font-mondwest text-display text-base tracking-wider"
              >
                Configure {editing.name}
              </h2>
              {editing.docs_url && (
                <a
                  href={editing.docs_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-flex items-center gap-1 text-xs text-primary hover:underline"
                >
                  Setup guide <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </header>

            <div className="p-5 grid gap-4 overflow-y-auto">
              <p className="text-xs text-muted-foreground">
                {editing.description}
              </p>
              {editing.env_vars.map((field: MessagingPlatformEnvVar) => (
                <div className="grid gap-1.5" key={field.key}>
                  <Label htmlFor={`field-${field.key}`}>
                    {field.prompt || field.key}
                    {field.required ? " *" : ""}
                  </Label>
                  {field.description && (
                    <span className="text-xs text-muted-foreground">
                      {field.description}
                    </span>
                  )}
                  <Input
                    id={`field-${field.key}`}
                    type={field.is_password ? "password" : "text"}
                    placeholder={
                      field.is_set
                        ? field.redacted_value || "•••••• (set — leave blank to keep)"
                        : field.key
                    }
                    value={draftEnv[field.key] ?? ""}
                    onChange={(e) =>
                      setDraftEnv((prev) => ({ ...prev, [field.key]: e.target.value }))
                    }
                  />
                </div>
              ))}

              <div className="flex justify-end gap-2 pt-1">
                <Button ghost size="sm" onClick={() => setEditing(null)}>
                  Cancel
                </Button>
                <Button
                  className="uppercase"
                  size="sm"
                  onClick={handleSave}
                  disabled={saving}
                  prefix={saving ? <Spinner /> : undefined}
                >
                  {saving ? "Saving…" : "Save & enable"}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Platform list */}
      <div className="grid gap-3">
        {platforms.map((platform) => {
          const badge = stateBadge(platform.state);
          const busy = togglingId === platform.id;
          const StateIcon =
            platform.state === "connected"
              ? CheckCircle2
              : platform.state === "fatal"
                ? AlertTriangle
                : Radio;
          return (
            <Card key={platform.id} className="border-border">
              <CardContent className="flex flex-col gap-4 p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex items-start gap-3 min-w-0">
                    <StateIcon
                      className={cn(
                        "h-5 w-5 shrink-0 mt-0.5",
                        platform.state === "connected"
                          ? "text-success"
                          : platform.state === "fatal"
                            ? "text-destructive"
                            : "text-muted-foreground",
                      )}
                    />
                    <div className="flex flex-col gap-0.5 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-mondwest normal-case text-sm font-medium">
                          {platform.name}
                        </span>
                        <Badge tone={badge.tone}>{badge.label}</Badge>
                      </div>
                      <span className="text-xs text-muted-foreground">
                        {platform.description}
                      </span>
                      {platform.error_message && (
                        <span className="text-xs text-destructive">
                          {platform.error_message}
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-2 shrink-0 self-start sm:self-center">
                    <div className="flex items-center gap-1.5">
                      {busy ? (
                        <Spinner className="text-sm" />
                      ) : (
                        <Switch
                          checked={platform.enabled}
                          onCheckedChange={() => void handleToggle(platform)}
                          aria-label={`Enable ${platform.name}`}
                        />
                      )}
                    </div>
                    <Button
                      ghost
                      size="sm"
                      onClick={() => handleTest(platform)}
                      disabled={testingId === platform.id}
                      prefix={
                        testingId === platform.id ? (
                          <Spinner />
                        ) : (
                          <PlugZap className="h-4 w-4" />
                        )
                      }
                    >
                      Test
                    </Button>
                    <Button
                      size="sm"
                      className="uppercase"
                      onClick={() => openConfig(platform)}
                      prefix={<Settings2 className="h-4 w-4" />}
                    >
                      Configure
                    </Button>
                  </div>
                </div>
                {platform.id === "telegram" && (
                  <TelegramOnboardingPanel
                    onChanged={load}
                    onRestartNeeded={() => setRestartNeeded(true)}
                    platform={platform}
                    setRestartNeeded={setRestartNeeded}
                    showToast={showToast}
                  />
                )}
                {platform.id === "whatsapp" && (
                  <WhatsappOnboardingPanel
                    onChanged={load}
                    onRestartNeeded={() => setRestartNeeded(true)}
                    setRestartNeeded={setRestartNeeded}
                    showToast={showToast}
                  />
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

function TelegramOnboardingPanel({
  onChanged,
  onRestartNeeded,
  platform,
  setRestartNeeded,
  showToast,
}: {
  onChanged: () => Promise<void>;
  onRestartNeeded: () => void;
  platform: MessagingPlatform;
  setRestartNeeded: (needed: boolean) => void;
  showToast: (message: string, type: "success" | "error") => void;
}) {
  const [setup, setSetup] = useState<TelegramOnboardingStartResponse | null>(
    null,
  );
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [phase, setPhase] = useState<
    "idle" | "starting" | "waiting" | "ready" | "applying"
  >("idle");
  const [botUsername, setBotUsername] = useState<string | null>(null);
  const [allowedIds, setAllowedIds] = useState<string[]>([]);
  const [detectedOwnerId, setDetectedOwnerId] = useState<string | null>(null);
  const [newAllowedId, setNewAllowedId] = useState("");
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!setup || phase !== "waiting") return;
    let cancelled = false;
    let timeout: ReturnType<typeof setTimeout> | null = null;

    const poll = async () => {
      try {
        const status = await api.getTelegramOnboardingStatus(setup.pairing_id);
        if (cancelled) return;
        if (status.status === "ready") {
          setPhase("ready");
          setBotUsername(status.bot_username ?? null);
          setError("");
          if (
            status.owner_user_id &&
            TELEGRAM_USER_ID_RE.test(status.owner_user_id)
          ) {
            setDetectedOwnerId(status.owner_user_id);
            setAllowedIds([status.owner_user_id]);
          }
          return;
        }
        setError("");
        timeout = setTimeout(poll, 2000);
      } catch (pollError) {
        if (cancelled) return;

        const expiresAt = Date.parse(setup.expires_at);
        const expired =
          Number.isFinite(expiresAt) && Date.now() >= expiresAt;
        if (isTerminalTelegramOnboardingError(pollError) || expired) {
          setSetup(null);
          setQrDataUrl("");
          setPhase("idle");
          setError("Telegram pairing expired. Start a new QR setup to try again.");
          return;
        }

        setError(`Still waiting for Telegram. Retrying after: ${pollError}`);
        timeout = setTimeout(poll, 2000);
      }
    };

    timeout = setTimeout(poll, 1200);
    return () => {
      cancelled = true;
      if (timeout) clearTimeout(timeout);
    };
  }, [phase, setup]);

  useEffect(() => {
    if (!setup) return;
    const timer = setInterval(() => setTick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [setup]);

  const resetSetup = () => {
    setSetup(null);
    setQrDataUrl("");
    setPhase("idle");
    setBotUsername(null);
    setAllowedIds([]);
    setDetectedOwnerId(null);
    setNewAllowedId("");
    setError("");
  };

  const start = async () => {
    setPhase("starting");
    setError("");
    setBotUsername(null);
    setAllowedIds([]);
    setDetectedOwnerId(null);
    setNewAllowedId("");
    try {
      const res = await api.startTelegramOnboarding({ bot_name: "Hermes Agent" });
      const dataUrl = await QRCode.toDataURL(res.qr_payload, {
        errorCorrectionLevel: "M",
        margin: 1,
        width: 224,
      });
      setSetup(res);
      setQrDataUrl(dataUrl);
      setPhase("waiting");
    } catch (startError) {
      setPhase("idle");
      setError(String(startError));
    }
  };

  const cancel = async () => {
    if (setup) {
      try {
        await api.cancelTelegramOnboarding(setup.pairing_id);
      } catch {
        /* local cleanup still wins */
      }
    }
    resetSetup();
  };

  const addAllowedId = () => {
    const trimmed = newAllowedId.trim();
    if (!TELEGRAM_USER_ID_RE.test(trimmed)) {
      setError("Allowed Telegram user IDs must be numeric.");
      return;
    }
    setError("");
    setAllowedIds((ids) => (ids.includes(trimmed) ? ids : [...ids, trimmed]));
    setNewAllowedId("");
  };

  // restart_started only means the `hermes gateway restart` child spawned —
  // not that the restart will succeed (e.g. systemd linger missing, service
  // manager failure). Poll the action status briefly and surface a non-zero
  // exit via the manual-restart banner. Note: in no-service installs the
  // child becomes the foreground gateway and never exits, so "still running
  // when the window closes" counts as success.
  const watchRestartOutcome = async () => {
    for (let i = 0; i < 20; i++) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        const st = await api.getActionStatus("gateway-restart", 5);
        if (st.running) continue;
        if (st.exit_code !== 0 && st.exit_code !== null) {
          onRestartNeeded();
          showToast(
            `Gateway restart failed (exit ${st.exit_code}) — restart manually`,
            "error",
          );
        }
        return;
      } catch {
        // transient fetch error; keep polling
      }
    }
  };

  const apply = async () => {
    if (!setup) return;
    if (allowedIds.length === 0) {
      setError("Add at least one allowed Telegram user ID.");
      return;
    }
    setPhase("applying");
    setError("");
    try {
      const result = await api.applyTelegramOnboarding(setup.pairing_id, {
        allowed_user_ids: allowedIds,
      });
      resetSetup();
      if (result.restart_started) {
        showToast("Telegram saved; gateway restarting…", "success");
        setRestartNeeded(false);
        setTimeout(() => void onChanged(), 4000);
        void watchRestartOutcome();
      } else if (result.restart_started === undefined && result.needs_restart) {
        try {
          await api.restartGateway();
          showToast("Telegram saved; gateway restarting…", "success");
          setRestartNeeded(false);
          setTimeout(() => void onChanged(), 4000);
        } catch (restartError) {
          onRestartNeeded();
          showToast(`Telegram saved; gateway restart failed: ${restartError}`, "error");
        }
      } else {
        onRestartNeeded();
        const detail = result.restart_error ? `: ${result.restart_error}` : "";
        showToast(`Telegram saved; gateway restart failed${detail}`, "error");
      }
      await onChanged();
    } catch (applyError) {
      setPhase("ready");
      setError(String(applyError));
    }
  };

  const expiresIn = useMemo(
    () => (setup ? formatExpiry(setup.expires_at) : ""),
    // tick keeps the memo fresh without recalculating on every render branch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [setup, tick],
  );

  return (
    <div className="rounded-sm border border-border bg-background/35 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          className="uppercase"
          onClick={() => void start()}
          disabled={phase === "starting" || phase === "waiting" || phase === "applying"}
          prefix={phase === "starting" ? <Spinner /> : <QrCode className="h-4 w-4" />}
        >
          {phase === "starting" ? "Starting…" : "Set up with QR"}
        </Button>
        {platform.configured && (
          <span className="text-xs text-muted-foreground">
            Existing Telegram credentials are configured.
          </span>
        )}
      </div>

      {error && (
        <div className="mt-3 border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {setup && qrDataUrl && (
        <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_260px]">
          <div className="grid gap-3">
            {(phase === "ready" || phase === "applying") && (
              <div className="grid gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="success">Ready</Badge>
                  {botUsername && (
                    <span className="font-courier text-sm text-muted-foreground">
                      @{botUsername}
                    </span>
                  )}
                </div>

                <div className="grid gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                      Allowed users
                    </span>
                    {detectedOwnerId && allowedIds.includes(detectedOwnerId) && (
                      <Badge tone="success">owner detected</Badge>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {allowedIds.map((id) => (
                      <button
                        key={id}
                        type="button"
                        className="inline-flex items-center gap-1 border border-border px-2 py-1 font-courier text-xs text-foreground hover:border-destructive/50"
                        onClick={() =>
                          setAllowedIds((ids) =>
                            ids.filter((existing) => existing !== id),
                          )
                        }
                      >
                        {id}
                        <X className="h-3 w-3" />
                      </button>
                    ))}
                    {allowedIds.length === 0 && (
                      <span className="text-sm text-muted-foreground">
                        Add at least one Telegram user ID.
                      </span>
                    )}
                  </div>
                </div>

                <div className="flex flex-col gap-2 sm:flex-row">
                  <Input
                    value={newAllowedId}
                    onChange={(event) => setNewAllowedId(event.target.value)}
                    placeholder="Telegram user ID"
                    className="font-courier"
                  />
                  <Button size="sm" outlined onClick={addAllowedId} prefix={<Check />}>
                    Add
                  </Button>
                </div>

                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    className="uppercase"
                    onClick={() => void apply()}
                    disabled={phase === "applying"}
                    prefix={phase === "applying" ? <Spinner /> : <Save className="h-4 w-4" />}
                  >
                    {phase === "applying" ? "Saving…" : "Save and restart"}
                  </Button>
                  <Button size="sm" ghost onClick={() => void cancel()}>
                    Cancel
                  </Button>
                </div>
              </div>
            )}
          </div>

          <div className="flex flex-col items-center justify-center gap-3">
            <img
              src={qrDataUrl}
              alt="Telegram setup QR code"
              className="h-56 w-56 bg-white p-2"
            />
            <div className="flex flex-wrap items-center justify-center gap-2 text-sm">
              <Badge tone={expiresIn === "expired" ? "destructive" : "outline"}>
                {expiresIn}
              </Badge>
              {phase === "waiting" && <Badge tone="warning">waiting</Badge>}
            </div>
            <div className="flex flex-wrap justify-center gap-2">
              <a
                href={setup.deep_link}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-8 items-center gap-1 border border-border px-3 text-xs uppercase text-foreground hover:border-foreground/40"
              >
                <ExternalLink className="h-4 w-4" />
                Open Telegram
              </a>
              <Button size="sm" ghost onClick={() => void cancel()}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---- WhatsApp dashboard-driven pairing (phone number + pairing code) ----
//
// State machine mirrors the backend:
//   idle       -> user can input a phone number and click "Set up with phone"
//   starting   -> POST /start fired; waiting for bridge.js to come up
//   waiting    -> bridge alive, Baileys still bootstrapping (no QR yet)
//   code       -> pairing code ready; show it big so the user can type
//                 it into WhatsApp > Linked Devices > Link with phone
//   paired     -> bridge reports connection === 'open' and creds.json
//                 written; user clicks "I have paired" to apply
//   error      -> bridge returned a pairing error; user can cancel/retry
//   exited     -> bridge process died (e.g. --pair-only exit). Show exit
//                 code and offer a fresh start.
function WhatsappOnboardingPanel({
  onChanged,
  onRestartNeeded,
  setRestartNeeded,
  showToast,
}: {
  onChanged: () => Promise<void>;
  onRestartNeeded: () => void;
  setRestartNeeded: (needed: boolean) => void;
  showToast: (message: string, type: "success" | "error") => void;
}) {
  const [phone, setPhone] = useState("");
  // Two pairing modes: "phone" (8-char code, faster, no extra device
  // needed) and "qr" (scan a QR with the phone's WA app, the more
  // familiar flow). The QR path is the recovery option when phone
  // pairing fails (e.g. WA's anti-spam blocks a rapid retry, the
  // number is already linked elsewhere, etc.).
  const [mode, setMode] = useState<WhatsappOnboardingMode>("phone");
  const [setup, setSetup] = useState<WhatsappOnboardingStartResponse | null>(
    null,
  );
  const [code, setCode] = useState("");
  // ``qrDataUrl`` holds the latest PNG data URL fetched from the
  // bridge's /pairing-qr endpoint. Refreshed on every poll so a
  // missed ``connection.update`` event doesn't leave the UI showing
  // a stale image.
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [phase, setPhase] = useState<
    | "idle"
    | "starting"
    | "waiting"
    | "code"
    | "paired"
    | "applying"
    | "error"
    | "exited"
  >("idle");
  const [error, setError] = useState("");
  const [exitCode, setExitCode] = useState<number | null>(null);
  const [tick, setTick] = useState(0);

  // Poll /status while a pairing is in flight. The backend transitions the
  // bridge through starting -> waiting -> code -> paired; the UI just
  // reflects whatever the latest /status payload reports.
  useEffect(() => {
    if (!setup) return;
    if (
      phase === "code" ||
      phase === "paired" ||
      phase === "error" ||
      phase === "exited"
    ) {
      // Terminal-ish states: stop polling. Re-arm only when the user
      // explicitly retries (which goes back to "starting").
      return;
    }
    let cancelled = false;
    let timeout: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      try {
        const status: WhatsappOnboardingStatusResponse =
          await api.getWhatsappOnboardingStatus(setup.pairing_id);
        if (cancelled) return;
        // In QR mode the latest PNG data URL is returned inline with
        // ``waiting``; capture it before falling through to the phase
        // switch so the <img> tag updates every poll.
        if ("qr" in status && status.qr) {
          setQrDataUrl(status.qr);
        }
        switch (status.status) {
          case "starting":
          case "waiting":
            setError("");
            setPhase(status.status);
            break;
          case "code":
            setCode(status.code);
            setError("");
            setPhase("code");
            break;
          case "paired":
            setCode(status.code ?? code);
            setError("");
            setPhase("paired");
            return; // stop polling
          case "error":
            setError(status.error);
            setPhase("error");
            return;
          case "exited":
            setExitCode(status.exit_code);
            setPhase("exited");
            return;
        }
        timeout = setTimeout(poll, 2000);
      } catch (pollError) {
        if (cancelled) return;
        setError(`Pairing service unreachable: ${pollError}`);
        timeout = setTimeout(poll, 3000);
      }
    };
    timeout = setTimeout(poll, 1200);
    return () => {
      cancelled = true;
      if (timeout) clearTimeout(timeout);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, setup]);

  // Drive the "expires in" countdown badge. Matches the Telegram UX.
  useEffect(() => {
    if (!setup) return;
    const timer = setInterval(() => setTick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [setup]);

  // Make sure we never leave a bridge process running when the panel
  // unmounts (e.g. user navigates away mid-pairing). The backend's
  // TTL is a safety net, not a substitute for explicit cleanup.
  useEffect(() => {
    return () => {
      if (setup && phase !== "paired") {
        void api.cancelWhatsappOnboarding(setup.pairing_id).catch(() => {});
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const reset = () => {
    setSetup(null);
    setCode("");
    setPhone("");
    setQrDataUrl("");
    setPhase("idle");
    setError("");
    setExitCode(null);
  };

  const start = async () => {
    if (mode === "phone") {
      // Strip everything but digits — backend re-validates, this is just UX.
      const normalized = phone.replace(/\D+/g, "");
      if (normalized.length < 6) {
        setError("Enter a valid phone number in international format.");
        return;
      }
      setPhase("starting");
      setError("");
      setCode("");
      setQrDataUrl("");
      try {
        const res = await api.startWhatsappOnboarding({
          phone: normalized,
          mode: "phone",
        });
        setSetup(res);
        setPhase("waiting");
      } catch (startError) {
        setPhase("idle");
        setError(String(startError));
      }
    } else {
      // QR mode: no phone required; backend spawns the bridge without
      // --phone so the first QR event renders the data URL.
      setPhase("starting");
      setError("");
      setCode("");
      setQrDataUrl("");
      try {
        const res = await api.startWhatsappOnboarding({ mode: "qr" });
        setSetup(res);
        setPhase("waiting");
      } catch (startError) {
        setPhase("idle");
        setError(String(startError));
      }
    }
  };

  const cancel = async () => {
    if (setup) {
      try {
        await api.cancelWhatsappOnboarding(setup.pairing_id);
      } catch {
        /* local cleanup still wins */
      }
    }
    reset();
  };

  // Watch the in-flight gateway restart the same way Telegram does.
  // Surface a non-zero exit via the manual restart banner so the user
  // can recover if the restart died (e.g. systemd linger missing).
  const watchRestartOutcome = async () => {
    for (let i = 0; i < 20; i++) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        const st = await api.getActionStatus("gateway-restart", 5);
        if (st.running) continue;
        if (st.exit_code !== 0 && st.exit_code !== null) {
          onRestartNeeded();
          showToast(
            `Gateway restart failed (exit ${st.exit_code}) — restart manually`,
            "error",
          );
        }
        return;
      } catch {
        // transient fetch error; keep polling
      }
    }
  };

  const apply = async () => {
    if (!setup) return;
    setPhase("applying");
    setError("");
    try {
      const result = await api.applyWhatsappOnboarding(setup.pairing_id);
      reset();
      if (result.restart_started) {
        showToast("WhatsApp saved; gateway restarting…", "success");
        setRestartNeeded(false);
        setTimeout(() => void onChanged(), 4000);
        void watchRestartOutcome();
      } else {
        onRestartNeeded();
        const detail = result.restart_error ? `: ${result.restart_error}` : "";
        showToast(
          `WhatsApp saved; gateway restart failed${detail}`,
          "error",
        );
      }
      await onChanged();
    } catch (applyError) {
      setPhase("paired");
      setError(String(applyError));
    }
  };

  // Memoized countdown for the "code" phase. Mirrors the Telegram UX.
  const expiresIn = useMemo(() => {
    if (!setup) return "";
    void tick; // keep memo tied to tick
    const ms = setup.expires_at * 1000 - Date.now();
    if (!Number.isFinite(ms) || ms <= 0) return "expired";
    const seconds = Math.ceil(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return `${minutes}:${rest.toString().padStart(2, "0")}`;
  }, [setup, tick]);

  return (
    <div className="rounded-sm border border-border bg-background/35 p-4">
      {/* Mode toggle. The two flows are mutually exclusive for a
          given pairing session (a bridge is either in --phone or QR
          mode), so we render a segmented switch instead of two
          separate buttons. The "idle / error / exited" phases are
          the only ones where the user can switch modes mid-flight
          without re-arming everything. */}
      <div className="mb-3 flex items-center gap-2">
        <span className="text-xs uppercase tracking-[0.12em] text-muted-foreground shrink-0">
          Mode
        </span>
        <div className="inline-flex rounded-sm border border-border overflow-hidden">
          <button
            type="button"
            onClick={() => {
              if (phase === "idle" || phase === "error" || phase === "exited") {
                setMode("phone");
              }
            }}
            disabled={
              phase !== "idle" && phase !== "error" && phase !== "exited"
            }
            className={
              "px-3 py-1 text-xs uppercase tracking-[0.12em] transition-colors " +
              (mode === "phone"
                ? "bg-primary text-primary-foreground"
                : "bg-background/40 text-muted-foreground hover:bg-foreground/5")
            }
            data-testid="wa-mode-phone"
          >
            Phone
          </button>
          <button
            type="button"
            onClick={() => {
              if (phase === "idle" || phase === "error" || phase === "exited") {
                setMode("qr");
              }
            }}
            disabled={
              phase !== "idle" && phase !== "error" && phase !== "exited"
            }
            className={
              "px-3 py-1 text-xs uppercase tracking-[0.12em] transition-colors " +
              (mode === "qr"
                ? "bg-primary text-primary-foreground"
                : "bg-background/40 text-muted-foreground hover:bg-foreground/5")
            }
            data-testid="wa-mode-qr"
          >
            QR
          </button>
        </div>
      </div>

      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:flex-wrap">
        {mode === "phone" ? (
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <Label
              htmlFor="wa-phone"
              className="shrink-0 text-xs uppercase tracking-[0.12em] text-muted-foreground"
            >
              Phone
            </Label>
            <Input
              id="wa-phone"
              value={phone}
              onChange={(event) => setPhone(event.target.value)}
              placeholder="6281234567890"
              inputMode="tel"
              className="font-courier flex-1 min-w-0"
              disabled={
                phase !== "idle" && phase !== "error" && phase !== "exited"
              }
            />
          </div>
        ) : (
          <div className="flex-1 min-w-0 text-sm text-muted-foreground">
            Scan the QR with WhatsApp on your phone — no number required.
          </div>
        )}
        <Button
          size="sm"
          className="uppercase"
          onClick={() => void start()}
          disabled={
            phase === "starting" ||
            phase === "waiting" ||
            phase === "code" ||
            phase === "paired" ||
            phase === "applying"
          }
          prefix={
            phase === "starting" ? <Spinner /> : <PlugZap className="h-4 w-4" />
          }
        >
          {phase === "starting"
            ? "Starting…"
            : setup
              ? "Restart pairing"
              : mode === "qr"
                ? "Generate QR"
                : "Set up with phone"}
        </Button>
      </div>

      <p className="mt-2 text-xs text-muted-foreground">
        {mode === "phone"
          ? "Pair without scanning a QR code. Enter the 8-character pairing code in WhatsApp → Settings → Linked Devices → Link with phone number."
          : "Scan the QR with WhatsApp → Settings → Linked Devices → Link a device. Use this if the phone code path fails or your number is already linked elsewhere."}
      </p>

      {error && (
        <div className="mt-3 border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {phase === "exited" && (
        <div className="mt-3 border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
          Pairing bridge exited
          {exitCode !== null ? ` (code ${exitCode})` : ""}.{" "}
          <button
            type="button"
            className="underline hover:no-underline"
            onClick={() => {
              setPhase("idle");
              setError("");
            }}
          >
            Try again
          </button>
        </div>
      )}

      {(phase === "waiting" || phase === "code" || phase === "paired") && (
        <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_260px]">
          <div className="grid gap-3">
            {phase === "paired" && (
              <div className="grid gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="success">Paired</Badge>
                  <span className="font-courier text-sm text-muted-foreground">
                    {setup?.phone}
                  </span>
                </div>
                <p className="text-sm text-muted-foreground">
                  Your phone is linked. Save and restart the gateway to start
                  receiving messages.
                </p>
                {code && (
                  <div className="grid gap-1">
                    <span className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                      Pairing code used
                    </span>
                    <span className="font-courier text-lg">{code}</span>
                  </div>
                )}
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    className="uppercase"
                    onClick={() => void apply()}
                    disabled={phase !== "paired"}
                    prefix={<Save className="h-4 w-4" />}
                  >
                    Save and restart
                  </Button>
                  <Button size="sm" ghost onClick={() => void cancel()}>
                    Cancel
                  </Button>
                </div>
              </div>
            )}

            {(phase === "waiting" || phase === "code") && (
              <div className="grid gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={phase === "code" ? "outline" : "warning"}>
                    {phase === "code"
                      ? "Enter code on phone"
                      : "Waiting for WhatsApp…"}
                  </Badge>
                  {setup?.phone && (
                    <span className="font-courier text-sm text-muted-foreground">
                      {setup.phone}
                    </span>
                  )}
                </div>
                {phase === "waiting" && (
                  <p className="text-sm text-muted-foreground">
                    Connecting to WhatsApp servers and generating your pairing
                    code…
                  </p>
                )}
              </div>
            )}
          </div>

          <div className="flex flex-col items-center justify-center gap-3">
            {mode === "qr" ? (
              <div className="grid gap-1 text-center" data-testid="wa-qr-wrap">
                <span className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                  Scan with WhatsApp
                </span>
                {qrDataUrl ? (
                  // ``img`` not ``Image`` — the data URL is already
                  // in-memory and we want zero extra round-trips or
                  // SSR. ``max-w-full`` keeps it from overflowing the
                  // 260px column.
                  <img
                    src={qrDataUrl}
                    alt="WhatsApp pairing QR"
                    width={240}
                    height={240}
                    className="bg-white p-2 border border-border max-w-full h-auto"
                    data-testid="whatsapp-pairing-qr"
                  />
                ) : (
                  <div className="w-[240px] h-[240px] flex items-center justify-center border border-border bg-foreground/5">
                    <Spinner className="text-3xl text-primary" />
                  </div>
                )}
                <span className="text-[10px] text-muted-foreground">
                  QR refreshes automatically while you scan
                </span>
              </div>
            ) : phase === "code" && code ? (
              <div className="grid gap-1 text-center">
                <span className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                  Pairing code
                </span>
                <span
                  className="font-courier text-3xl tracking-widest bg-foreground/5 border border-border px-4 py-3"
                  data-testid="whatsapp-pairing-code"
                >
                  {code}
                </span>
                <Badge tone={expiresIn === "expired" ? "destructive" : "outline"}>
                  {expiresIn}
                </Badge>
              </div>
            ) : (
              <Spinner className="text-3xl text-primary" />
            )}
            <div className="flex flex-wrap justify-center gap-2">
              <Button size="sm" ghost onClick={() => void cancel()}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
