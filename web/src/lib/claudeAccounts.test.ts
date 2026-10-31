import { describe, expect, it } from "vitest";
import { accountQuotaView, CLAUDE_QUOTA_STALE_AFTER_MS } from "./claudeAccounts";

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
