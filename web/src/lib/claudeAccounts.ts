import type { Host } from "@/hooks/useHosts";

/** How the picker and Settings name a host's default Claude login. */
export const CLAUDE_DEFAULT_ACCOUNT_LABEL = "Default login";

/** The host's extra Claude account names, sorted; empty when none are reported. */
export function hostClaudeAccountNames(host: Host | null | undefined): string[] {
  return Object.keys(host?.claude_accounts ?? {}).sort();
}
