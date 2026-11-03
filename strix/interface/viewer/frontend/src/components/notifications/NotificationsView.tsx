import { useCallback, useEffect, useId, useState } from "react";
import { AlertCircle, Bell, CheckCircle2, Loader2, ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  NOTIFICATION_CHANNELS,
  NOTIFICATION_EVENTS,
  SettingsError,
  fetchNotifications,
  saveNotifications,
  testNotification,
  type NotificationChannelKey,
  type NotificationEventKey,
  type NotificationsUpdate,
  type NotificationsView,
  type NotificationTestResult,
} from "@/data/serverSource";

/**
 * The Notifications page: send a message to Slack, Microsoft Teams or Telegram
 * when a scan starts, makes progress, finishes, stops or fails.
 *
 * Webhook URLs and bot tokens are write-only. They are sent when typed and never
 * shown again; a saved one is shown as "Saved" and kept unless replaced or
 * explicitly removed. Every control has a real label, errors use `role="alert"`
 * with an icon (colour is never the only signal), and results are announced
 * through an `aria-live` region.
 */

const EVENT_LABELS: Record<NotificationEventKey, { label: string; hint: string }> = {
  started: { label: "Scan started", hint: "When a scan launched from this viewer begins." },
  progress: { label: "Progress updates", hint: "A periodic update while the scan is running." },
  finished: { label: "Scan finished", hint: "When a scan completes, with finding counts." },
  stopped: { label: "Scan stopped", hint: "When a scan is stopped before it completes." },
  failed: { label: "Scan failed", hint: "When a scan exits with an error." },
};

interface ChannelCopy {
  label: string;
  help: string;
}

const CHANNEL_COPY: Record<NotificationChannelKey, ChannelCopy> = {
  slack: {
    label: "Slack",
    help: "Create an Incoming Webhook for the channel in your Slack workspace, then paste its URL.",
  },
  teams: {
    label: "Microsoft Teams",
    help: 'In Teams, add the "Post to a channel when a webhook request is received" workflow to a channel, then paste its URL.',
  },
  telegram: {
    label: "Telegram",
    help: "Create a bot with @BotFather, add it to your chat or channel, then enter the bot token and the chat id.",
  },
};

type Secrets = { slack: string; teams: string; token: string };

interface FormState {
  enabled: boolean;
  events: Record<NotificationEventKey, boolean>;
  interval: string;
  channelEnabled: Record<NotificationChannelKey, boolean>;
  secrets: Secrets;
  chatId: string;
  /** Saved secrets the user asked to remove on the next save. */
  removed: Set<"slack" | "teams" | "token">;
}

function formFromView(view: NotificationsView): FormState {
  return {
    enabled: view.enabled,
    events: { ...view.events },
    interval: String(view.progress_interval_minutes),
    channelEnabled: {
      slack: view.channels.slack.enabled,
      teams: view.channels.teams.enabled,
      telegram: view.channels.telegram.enabled,
    },
    secrets: { slack: "", teams: "", token: "" },
    chatId: view.channels.telegram.chat_id,
    removed: new Set(),
  };
}

/** Where a server error code belongs on this page. */
type ErrorTarget = NotificationChannelKey | "interval" | "form";

function errorTarget(code: string, secrets: Secrets): ErrorTarget {
  if (code === "invalid_bot_token" || code === "invalid_chat_id") return "telegram";
  if (code === "invalid_interval") return "interval";
  if (code === "invalid_webhook_url") {
    // The server does not say which webhook; if exactly one was typed it is that one.
    const typed = (["slack", "teams"] as const).filter((c) => secrets[c].trim() !== "");
    return typed.length === 1 ? typed[0] : "form";
  }
  return "form";
}

export function NotificationsPage() {
  const [view, setView] = useState<NotificationsView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<{ target: ErrorTarget; message: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await fetchNotifications();
      setView(next);
      setForm(formFromView(next));
      setLoadError(null);
    } catch (err) {
      setLoadError(
        err instanceof SettingsError && err.code === "notifications_unavailable"
          ? "Notifications are not available in this viewer."
          : "The notification settings could not be loaded."
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loadError) {
    return (
      <PageShell>
        <Banner message={loadError} testId="notifications-load-error" />
      </PageShell>
    );
  }
  if (!view || !form) {
    return (
      <PageShell>
        <div className="flex items-center gap-2 text-sm text-[#888]" role="status">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Loading…
        </div>
      </PageShell>
    );
  }

  const readOnly = !view.editable;
  const blocked = readOnly || view.file_status === "unreadable";
  const patch = (next: Partial<FormState>) => {
    setSaved(false);
    setForm((prev) => (prev ? { ...prev, ...next } : prev));
  };

  const handleSave = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || blocked) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const next = await saveNotifications(buildUpdate(form, view));
      setView(next);
      setForm(formFromView(next));
      setSaved(true);
    } catch (err) {
      if (err instanceof SettingsError) {
        setError({ target: errorTarget(err.code, form.secrets), message: err.message });
      } else {
        setError({ target: "form", message: "The settings could not be saved. Please try again." });
      }
    } finally {
      setSaving(false);
    }
  };

  const intervalError = error?.target === "interval" ? error.message : undefined;
  const intervalBad =
    form.events.progress &&
    !(Number.isInteger(Number(form.interval)) &&
      Number(form.interval) >= view.interval_min &&
      Number(form.interval) <= view.interval_max);

  return (
    <PageShell>
      <p className="max-w-2xl text-sm leading-relaxed text-[#aaa]">
        Get a message in Slack, Microsoft Teams or Telegram when a scan you start from this viewer
        begins, makes progress, finishes, stops or fails. Messages include the target, scan mode,
        run name, elapsed time and finding counts — never your instructions, keys or finding
        details.
      </p>

      {readOnly && (
        <Notice testId="notifications-readonly-note">
          This viewer is reachable over the network with sign-in off, so notification settings are
          read-only. Turn sign-in on (the default) to change them remotely, or open the viewer from
          this machine.
        </Notice>
      )}
      {view.file_status === "unreadable" && (
        <Banner
          testId="notifications-unreadable"
          message={`${view.config_path} is not valid JSON. Fix or delete the file, then reload this page. Saving is disabled so it is not overwritten.`}
        />
      )}
      {error?.target === "form" && (
        <Banner testId="notifications-error" message={error.message} />
      )}

      <form onSubmit={handleSave} noValidate className="space-y-6" data-testid="notifications-form">
        {/* Master switch */}
        <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-[#2a2a2a] px-4 py-3 text-sm text-[#ddd]">
          <input
            type="checkbox"
            data-testid="notifications-enabled"
            checked={form.enabled}
            disabled={blocked || saving}
            onChange={(e) => patch({ enabled: e.target.checked })}
            className="mt-0.5 h-4 w-4 flex-shrink-0 accent-emerald-400"
          />
          <span>
            <span className="block font-medium text-white">Send notifications</span>
            <span className="block text-xs text-[#888]">
              Master switch. Nothing is sent while this is off, whatever else is set up.
            </span>
          </span>
        </label>

        {/* Events */}
        <fieldset className="space-y-2" disabled={blocked || saving}>
          <legend className="text-sm font-medium text-white">Notify me when</legend>
          <div className="space-y-2">
            {NOTIFICATION_EVENTS.map((key) => (
              <label
                key={key}
                className="flex cursor-pointer items-start gap-3 rounded-lg border border-[#2a2a2a] px-3 py-2.5 text-sm"
              >
                <input
                  type="checkbox"
                  data-testid={`notifications-event-${key}`}
                  checked={form.events[key]}
                  onChange={(e) => patch({ events: { ...form.events, [key]: e.target.checked } })}
                  className="mt-0.5 h-4 w-4 flex-shrink-0 accent-emerald-400"
                />
                <span className="min-w-0">
                  <span className="block font-medium text-white">{EVENT_LABELS[key].label}</span>
                  <span className="block text-xs text-[#888]">{EVENT_LABELS[key].hint}</span>
                </span>
              </label>
            ))}
          </div>
          {form.events.progress && (
            <IntervalField
              value={form.interval}
              min={view.interval_min}
              max={view.interval_max}
              disabled={blocked || saving}
              invalid={intervalBad || intervalError !== undefined}
              error={intervalError}
              onChange={(interval) => patch({ interval })}
            />
          )}
        </fieldset>

        {/* Channels */}
        <div className="space-y-4">
          <h2 className="text-sm font-medium text-white">Where to send</h2>
          {NOTIFICATION_CHANNELS.map((key) => (
            <ChannelCard
              key={key}
              channel={key}
              view={view}
              form={form}
              disabled={blocked || saving}
              error={error?.target === key ? error.message : undefined}
              onPatch={patch}
            />
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            data-testid="notifications-save"
            disabled={blocked || saving || intervalBad}
            className={cn(
              "inline-flex items-center justify-center gap-2 rounded-lg px-5 py-2.5 text-sm font-semibold transition-colors",
              blocked || saving || intervalBad
                ? "cursor-not-allowed bg-white/[0.08] text-[#666]"
                : "bg-white text-black hover:bg-neutral-200"
            )}
          >
            {saving && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
            {saving ? "Saving…" : "Save"}
          </button>
          <div role="status" aria-live="polite" className="text-sm">
            {saved && (
              <span
                data-testid="notifications-saved"
                className="inline-flex items-center gap-1.5 text-emerald-400"
              >
                <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                Saved. New settings apply to the next notification.
              </span>
            )}
          </div>
        </div>

        <p className="text-xs text-[#666]">
          Webhook URLs and bot tokens are stored in {view.config_path} on this machine and are never
          shown again after saving.
        </p>
      </form>
    </PageShell>
  );
}

/** Build the PUT body: only what changed, and a secret only when the user typed one. */
function buildUpdate(form: FormState, view: NotificationsView): NotificationsUpdate {
  const channels: NonNullable<NotificationsUpdate["channels"]> = {
    slack: { enabled: form.channelEnabled.slack },
    teams: { enabled: form.channelEnabled.teams },
    telegram: { enabled: form.channelEnabled.telegram },
  };
  const clear: NonNullable<NotificationsUpdate["clear"]> = [];

  if (form.secrets.slack.trim()) channels.slack!.webhook_url = form.secrets.slack.trim();
  else if (form.removed.has("slack")) clear.push("slack.webhook_url");

  if (form.secrets.teams.trim()) channels.teams!.webhook_url = form.secrets.teams.trim();
  else if (form.removed.has("teams")) clear.push("teams.webhook_url");

  if (form.secrets.token.trim()) channels.telegram!.bot_token = form.secrets.token.trim();
  else if (form.removed.has("token")) clear.push("telegram.bot_token");

  const chat = form.chatId.trim();
  if (chat && chat !== view.channels.telegram.chat_id) channels.telegram!.chat_id = chat;
  else if (!chat && view.channels.telegram.chat_id) clear.push("telegram.chat_id");

  const update: NotificationsUpdate = {
    enabled: form.enabled,
    events: { ...form.events },
    channels,
  };
  if (form.events.progress) update.progress_interval_minutes = Number(form.interval);
  if (clear.length > 0) update.clear = clear;
  return update;
}

function PageShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto max-w-2xl space-y-6" data-testid="notifications-view">
      <div className="flex items-center gap-2">
        <Bell className="h-5 w-5 text-[#888]" aria-hidden="true" />
        <h1 className="text-2xl font-semibold text-white">Notifications</h1>
      </div>
      {children}
    </div>
  );
}

function Banner({ message, testId }: { message: string; testId: string }) {
  return (
    <div
      role="alert"
      data-testid={testId}
      className="flex items-start gap-3 rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3"
    >
      <AlertCircle className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-400" aria-hidden="true" />
      <p className="text-sm text-red-300">{message}</p>
    </div>
  );
}

function Notice({ children, testId }: { children: React.ReactNode; testId: string }) {
  return (
    <div
      role="note"
      data-testid={testId}
      className="flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3"
    >
      <ShieldAlert className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-400" aria-hidden="true" />
      <p className="text-sm text-amber-200">{children}</p>
    </div>
  );
}

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p id={id} role="alert" className="flex items-center gap-1.5 text-xs text-red-400">
      <ShieldAlert className="h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </p>
  );
}

const INPUT_CLASS =
  "block w-full rounded-lg border bg-[rgba(255,255,255,0.03)] px-3 py-2 text-sm text-white placeholder:text-[#555] transition-colors focus:outline-none focus:ring-2 focus:ring-white/20 disabled:opacity-60";

function IntervalField({
  value,
  min,
  max,
  disabled,
  invalid,
  error,
  onChange,
}: {
  value: string;
  min: number;
  max: number;
  disabled: boolean;
  invalid: boolean;
  error?: string;
  onChange: (value: string) => void;
}) {
  const id = useId();
  return (
    <div className="space-y-1.5 pl-1 pt-1">
      <label htmlFor={id} className="block text-xs font-medium text-[#bbb]">
        Send a progress update every (minutes)
      </label>
      <input
        id={id}
        data-testid="notifications-interval"
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        step={1}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={invalid ? true : undefined}
        aria-describedby={`${id}-hint`}
        className={cn(INPUT_CLASS, "w-32", invalid ? "border-red-500/60" : "border-[#2a2a2a]")}
      />
      <p id={`${id}-hint`} className="text-xs text-[#666]">
        Between {min} and {max} minutes.
      </p>
      <FieldError id={`${id}-err`} message={error} />
    </div>
  );
}

interface ChannelCardProps {
  channel: NotificationChannelKey;
  view: NotificationsView;
  form: FormState;
  disabled: boolean;
  error?: string;
  onPatch: (next: Partial<FormState>) => void;
}

function ChannelCard({ channel, view, form, disabled, error, onPatch }: ChannelCardProps) {
  const uid = useId();
  const copy = CHANNEL_COPY[channel];
  const saved = view.channels[channel];
  const isTelegram = channel === "telegram";
  const secretKey = isTelegram ? "token" : channel;
  const secretValue = isTelegram ? form.secrets.token : form.secrets[channel as "slack" | "teams"];
  const removed = form.removed.has(secretKey as "slack" | "teams" | "token");
  const hasSavedSecret = isTelegram ? saved.token_set : saved.configured;

  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<NotificationTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const setSecret = (value: string) => {
    onPatch({ secrets: { ...form.secrets, [isTelegram ? "token" : channel]: value } });
    if (removed && value) toggleRemoved(false);
  };
  const toggleRemoved = (on: boolean) => {
    const next = new Set(form.removed);
    if (on) next.add(secretKey as "slack" | "teams" | "token");
    else next.delete(secretKey as "slack" | "teams" | "token");
    onPatch({ removed: next });
  };

  const runTest = async () => {
    setTesting(true);
    setResult(null);
    setTestError(null);
    try {
      const body = isTelegram
        ? {
            channel,
            ...(form.secrets.token.trim() ? { bot_token: form.secrets.token.trim() } : {}),
            ...(form.chatId.trim() ? { chat_id: form.chatId.trim() } : {}),
          }
        : {
            channel,
            ...(secretValue.trim() ? { webhook_url: secretValue.trim() } : {}),
          };
      setResult(await testNotification(body));
    } catch (err) {
      setTestError(
        err instanceof SettingsError ? err.message : "The test could not be sent. Please try again."
      );
    } finally {
      setTesting(false);
    }
  };

  const secretId = `${uid}-secret`;
  const chatId = `${uid}-chat`;
  const errId = `${uid}-err`;
  const secretLabel = isTelegram ? "Bot token" : "Webhook URL";
  const placeholder = hasSavedSecret && !removed
    ? "Saved — leave blank to keep it"
    : isTelegram
      ? "123456789:AA…"
      : "https://…";
  const canTest =
    !disabled &&
    !testing &&
    (isTelegram
      ? (form.secrets.token.trim() !== "" || (saved.token_set && !removed)) && form.chatId.trim() !== ""
      : secretValue.trim() !== "" || (saved.configured && !removed));

  return (
    <section
      data-testid={`notifications-channel-${channel}`}
      className="space-y-3 rounded-lg border border-[#2a2a2a] p-4"
    >
      <div className="flex items-center justify-between gap-3">
        <label className="flex cursor-pointer items-center gap-3 text-sm font-medium text-white">
          <input
            type="checkbox"
            data-testid={`notifications-channel-${channel}-enabled`}
            checked={form.channelEnabled[channel]}
            disabled={disabled}
            onChange={(e) =>
              onPatch({ channelEnabled: { ...form.channelEnabled, [channel]: e.target.checked } })
            }
            className="h-4 w-4 flex-shrink-0 accent-emerald-400"
          />
          {copy.label}
        </label>
        <span
          className={cn(
            "rounded-full border px-2 py-0.5 text-[11px]",
            saved.configured
              ? "border-emerald-500/30 text-emerald-400"
              : "border-[#2a2a2a] text-[#777]"
          )}
        >
          {saved.configured ? "Configured" : "Not configured"}
        </span>
      </div>

      <p className="text-xs leading-relaxed text-[#777]">{copy.help}</p>

      <div className="space-y-1.5">
        <label htmlFor={secretId} className="block text-xs font-medium text-[#bbb]">
          {secretLabel}
        </label>
        <input
          id={secretId}
          data-testid={`notifications-${channel}-secret`}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={secretValue}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => setSecret(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errId : undefined}
          className={cn(INPUT_CLASS, error ? "border-red-500/60" : "border-[#2a2a2a]")}
        />
        {!isTelegram && saved.host && !removed && (
          <p className="text-xs text-[#666]">Saved webhook host: {saved.host}</p>
        )}
        {hasSavedSecret && (
          <label className="flex cursor-pointer items-center gap-2 text-xs text-[#999]">
            <input
              type="checkbox"
              data-testid={`notifications-${channel}-remove`}
              checked={removed}
              disabled={disabled || secretValue.trim() !== ""}
              onChange={(e) => toggleRemoved(e.target.checked)}
              className="h-3.5 w-3.5 accent-emerald-400"
            />
            Remove the saved {isTelegram ? "bot token" : "webhook URL"} when I save
          </label>
        )}
      </div>

      {isTelegram && (
        <div className="space-y-1.5">
          <label htmlFor={chatId} className="block text-xs font-medium text-[#bbb]">
            Chat id
          </label>
          <input
            id={chatId}
            data-testid="notifications-telegram-chat"
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={form.chatId}
            disabled={disabled}
            placeholder="-1001234567890 or @channelname"
            onChange={(e) => onPatch({ chatId: e.target.value })}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errId : undefined}
            className={cn(INPUT_CLASS, error ? "border-red-500/60" : "border-[#2a2a2a]")}
          />
        </div>
      )}

      <FieldError id={errId} message={error} />

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          data-testid={`notifications-${channel}-test`}
          onClick={() => void runTest()}
          disabled={!canTest}
          className={cn(
            "inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors",
            canTest
              ? "border-[#3a3a3a] text-white hover:border-[#666]"
              : "cursor-not-allowed border-[#2a2a2a] text-[#555]"
          )}
        >
          {testing && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          {testing ? "Sending…" : "Send test message"}
        </button>
        <div role="status" aria-live="polite" className="min-w-0 text-xs">
          {result && (
            <span
              data-testid={`notifications-${channel}-test-result`}
              className={cn(
                "inline-flex items-start gap-1.5",
                result.ok ? "text-emerald-400" : "text-red-400"
              )}
            >
              {result.ok ? (
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
              ) : (
                <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
              )}
              <span>{result.message}</span>
            </span>
          )}
          {testError && (
            <span className="inline-flex items-start gap-1.5 text-red-400" role="alert">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
              <span>{testError}</span>
            </span>
          )}
        </div>
      </div>
    </section>
  );
}

export default NotificationsPage;
