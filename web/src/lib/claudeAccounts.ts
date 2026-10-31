import type { Host } from "@/hooks/useHosts";
import { harnessReadinessOnHost } from "@/lib/harnessSetup";

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
