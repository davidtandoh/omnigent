import { describe, expect, it } from "vitest";
import {
  accountQuotaView,
  accountUsageView,
  CLAUDE_QUOTA_STALE_AFTER_MS,
  type AccountUsage,
} from "./claudeAccounts";

const NOW = Date.parse("2026-10-31T12:00:00Z");
const window = (resetsAt: string) => ({ percent_used: 50, resets_at: resetsAt });

describe("accountQuotaView", () => {
  it("is unavailable without a snapshot time or a reading", () => {
    expect(accountQuotaView(undefined, NOW).state).toBe("unavailable");
    expect(accountQuotaView({ account: "default", status: "unavailable" }, NOW).state).toBe(
      "unavailable",
    );
    expect(
      accountQuotaView(
        { account: "default", status: "available", five_hour: window("2026-10-31T13:00:00Z") },
        NOW,
      ).state,
    ).toBe("unavailable");
  });

  it("marks a reading older than the threshold stale even when the host called it fresh", () => {
    const observed = new Date(NOW - CLAUDE_QUOTA_STALE_AFTER_MS - 1).toISOString();
    const view = accountQuotaView(
      {
        account: "default",
        status: "available",
        seven_day: window("2026-11-02T12:00:00Z"),
        observed_at: observed,
      },
      NOW,
    );
    expect(view.state).toBe("stale");
    expect(view.ageText).toBe("Read 15m ago");
  });

  it("formats near and far resets", () => {
    const view = accountQuotaView(
      {
        account: "default",
        status: "available",
        five_hour: window("2026-10-31T12:20:00Z"),
        seven_day: window("2026-10-31T15:00:00Z"),
        observed_at: "2026-10-31T12:00:00Z",
      },
      NOW,
    );
    expect(view.ageText).toBe("Read just now");
    expect(view.windows.map((w) => w.resetText)).toEqual(["Resets in 20m", "Resets in 3h"]);
  });
});

describe("accountUsageView", () => {
  const codex = (overrides: Partial<AccountUsage>): AccountUsage => ({
    provider: "codex",
    account: "default",
    status: "available",
    observed_at: "2026-10-31T12:00:00Z",
    windows: [
      { kind: "session", label: "5-hour", percent_used: 12, resets_at: "2026-10-31T15:00:00Z" },
      { kind: "weekly", label: "Weekly", percent_used: 36, resets_at: "2026-11-04T09:30:00Z" },
    ],
    ...overrides,
  });

  it("shows each reported window with its own label and reset", () => {
    const view = accountUsageView(codex({}), NOW);
    expect(view.state).toBe("current");
    expect(view.ageText).toBe("Read just now");
    expect(view.windows.map((w) => [w.label, w.percentUsed, w.resetText])).toEqual([
      ["5-hour", 12, "Resets in 3h"],
      ["Weekly", 36, expect.stringMatching(/^Resets /)],
    ]);
  });

  it("keeps a window without a reset time and says so", () => {
    const view = accountUsageView(
      codex({
        windows: [
          { kind: "monthly", label: "Monthly credit limit", percent_used: 80, resets_at: null },
        ],
      }),
      NOW,
    );
    expect(view.windows).toEqual([
      { label: "Monthly credit limit", percentUsed: 80, resetText: "No reset time reported" },
    ]);
  });

  it("is unavailable without a snapshot, a reading, or any windows", () => {
    expect(accountUsageView(undefined, NOW).state).toBe("unavailable");
    expect(
      accountUsageView(codex({ status: "unavailable", windows: [], observed_at: null }), NOW).state,
    ).toBe("unavailable");
    expect(accountUsageView(codex({ windows: [] }), NOW).state).toBe("unavailable");
    expect(accountUsageView(codex({ observed_at: null }), NOW).state).toBe("unavailable");
  });

  it("marks a host-stale reading stale and drops a window that has reset", () => {
    const view = accountUsageView(
      codex({
        status: "stale",
        observed_at: "2026-10-31T09:00:00Z",
        windows: [
          { kind: "session", label: "5-hour", percent_used: 75, resets_at: "2026-10-31T11:00:00Z" },
        ],
      }),
      NOW,
    );
    expect(view.state).toBe("stale");
    expect(view.ageText).toBe("Read 3h ago");
    expect(view.windows).toEqual([
      { label: "5-hour", percentUsed: null, resetText: "Reset since this reading" },
    ]);
  });
});
