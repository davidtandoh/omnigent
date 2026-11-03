import type { Host } from "@/hooks/useHosts";
import { harnessReadinessOnHost } from "@/lib/harnessSetup";
import { relativeTime } from "@/lib/relativeTime";

/** How the picker and Settings name a host's default Claude login. */
export const CLAUDE_DEFAULT_ACCOUNT_LABEL = "Default login";

/** The host's extra Claude account names, sorted; empty when none are reported. */
export function hostClaudeAccountNames(host: Host | null | undefined): string[] {
  return Object.keys(host?.claude_accounts ?? {})
    .filter((name) => name !== "default")
    .sort();
}

export function pickerHarnessReadinessOnHost(
  harness: string | null | undefined,
  host: Host | null | undefined,
) {
  const readiness = harnessReadinessOnHost(harness, host);
  if (
    harness === "claude-native" &&
    readiness.reason === "needs-auth" &&
    hostClaudeAccountNames(host).length > 0
  ) {
    return { ...readiness, selectable: true, fallbackRelevant: false };
  }
  return readiness;
}

/** One usage window from `GET /v1/hosts/{id}/claude-quotas`. */
export interface ClaudeQuotaWindow {
  percent_used: number;
  /** ISO-8601 time the window resets. */
  resets_at: string;
}

/** A weekly limit scoped to one model family, e.g. `"Opus"`. */
export interface ClaudeModelQuotaWindow extends ClaudeQuotaWindow {
  model: string;
}

/** A host's sanitized quota snapshot for one Claude account. */
export interface ClaudeAccountQuota {
  /** Short account name; `"default"` is the default login. */
  account: string;
  /** `stale` is the host's last successful reading after a fresh read failed. */
  status: "available" | "stale" | "unavailable";
  five_hour?: ClaudeQuotaWindow | null;
  seven_day?: ClaudeQuotaWindow | null;
  model_windows?: ClaudeModelQuotaWindow[];
  /** ISO-8601 time the vendor reported these windows. */
  observed_at?: string | null;
}

/** One provider-neutral usage window, as read from a CLI's own usage report. */
export interface UsageWindow {
  kind: "session" | "weekly" | "weekly_model" | "monthly";
  /** Display name, e.g. `"5-hour"` or `"Monthly credit limit"`. */
  label: string;
  percent_used: number;
  /** ISO-8601 time the window resets; `null` when the CLI reports none. */
  resets_at?: string | null;
  window_minutes?: number | null;
}

/** One account's usage snapshot from the host's own CLI (Codex today). */
export interface AccountUsage {
  provider: "claude" | "codex";
  /** Short account name; `"default"` is the CLI's own login. */
  account: string;
  status: "available" | "stale" | "unavailable";
  windows?: UsageWindow[];
  observed_at?: string | null;
}

/** The body of `GET /v1/hosts/{id}/claude-quotas`. Older hosts omit `usage`. */
export interface HostQuotaSnapshot {
  quotas: ClaudeAccountQuota[];
  usage: AccountUsage[];
}

/** A reading older than this is shown as stale even if the host called it fresh. */
export const CLAUDE_QUOTA_STALE_AFTER_MS = 15 * 60_000;

export interface QuotaWindowView {
  label: string;
  /** `null` once the window has reset since the reading: the old number no longer applies. */
  percentUsed: number | null;
  resetText: string;
}

export interface AccountQuotaView {
  state: "current" | "stale" | "unavailable";
  /** Snapshot age, e.g. `"Read 5m ago"`; `null` when there is no snapshot. */
  ageText: string | null;
  windows: QuotaWindowView[];
}

const HOUR_MS = 60 * 60_000;

function resetText(resetMs: number, nowMs: number): string {
  const remaining = resetMs - nowMs;
  if (remaining < HOUR_MS) return `Resets in ${Math.max(1, Math.ceil(remaining / 60_000))}m`;
  if (remaining < 24 * HOUR_MS) {
    const hours = Math.floor(remaining / HOUR_MS);
    const minutes = Math.floor((remaining % HOUR_MS) / 60_000);
    return `Resets in ${hours}h${minutes ? ` ${minutes}m` : ""}`;
  }
  const when = new Date(resetMs).toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
  return `Resets ${when}`;
}

interface LabeledWindow {
  label: string;
  percent_used: number;
  resets_at?: string | null;
}

const UNAVAILABLE: AccountQuotaView = { state: "unavailable", ageText: null, windows: [] };

/**
 * Shared display logic for Claude quotas and provider-neutral usage. Never
 * presents an old number as current: an aged or host-stale reading is
 * `stale`, and a window whose reset time has passed drops its percentage. A
 * window with a malformed reset time is dropped; one with no reset time at
 * all keeps its percentage and says so.
 */
function quotaView(
  status: "available" | "stale",
  observedMs: number,
  windows: LabeledWindow[],
  nowMs: number,
): AccountQuotaView {
  const ageMs = Math.max(0, nowMs - observedMs);
  const age = relativeTime(observedMs, nowMs);
  return {
    state: status === "stale" || ageMs > CLAUDE_QUOTA_STALE_AFTER_MS ? "stale" : "current",
    ageText: age === "now" ? "Read just now" : `Read ${age} ago`,
    windows: windows.flatMap<QuotaWindowView>(({ label, percent_used, resets_at }) => {
      if (resets_at == null) {
        return [{ label, percentUsed: percent_used, resetText: "No reset time reported" }];
      }
      const resetMs = Date.parse(resets_at);
      if (!Number.isFinite(resetMs)) return [];
      return resetMs <= nowMs
        ? [{ label, percentUsed: null, resetText: "Reset since this reading" }]
        : [{ label, percentUsed: percent_used, resetText: resetText(resetMs, nowMs) }];
    }),
  };
}

/** Turn one Claude account's quota snapshot into display rows. */
export function accountQuotaView(
  quota: ClaudeAccountQuota | undefined,
  nowMs: number,
): AccountQuotaView {
  const observedMs = quota?.observed_at ? Date.parse(quota.observed_at) : NaN;
  if (!quota || quota.status === "unavailable" || !Number.isFinite(observedMs)) {
    return UNAVAILABLE;
  }
  const windows: [string, ClaudeQuotaWindow | null | undefined][] = [
    ["5-hour", quota.five_hour],
    ["Weekly", quota.seven_day],
    ...(quota.model_windows ?? []).map((window): [string, ClaudeQuotaWindow] => [
      `${window.model} weekly`,
      window,
    ]),
  ];
  return quotaView(
    quota.status,
    observedMs,
    windows.flatMap(([label, window]) => (window ? [{ ...window, label }] : [])),
    nowMs,
  );
}

/**
 * Turn one provider-neutral usage snapshot into display rows. A snapshot
 * without windows is unavailable: there is nothing honest to draw.
 */
export function accountUsageView(usage: AccountUsage | undefined, nowMs: number): AccountQuotaView {
  const observedMs = usage?.observed_at ? Date.parse(usage.observed_at) : NaN;
  const windows = usage?.windows ?? [];
  if (
    !usage ||
    usage.status === "unavailable" ||
    !Number.isFinite(observedMs) ||
    windows.length === 0
  ) {
    return UNAVAILABLE;
  }
  return quotaView(usage.status, observedMs, windows, nowMs);
}
