import { describe, expect, it } from "vitest";
import { formatDate, formatElapsed } from "./format";

describe("formatDate", () => {
  it("formats a valid ISO timestamp", () => {
    expect(formatDate("2026-11-09T10:00:00Z")).not.toBe("Date unavailable");
  });

  it("handles invalid timestamps", () => {
    expect(formatDate("not-a-date")).toBe("Date unavailable");
  });
});

describe("formatElapsed", () => {
  it("formats a completed stage duration", () => {
    expect(formatElapsed("2026-11-09T10:00:00Z", "2026-11-09T10:01:07Z")).toBe("1m 7s");
  });

  it("formats an active stage against the supplied current time", () => {
    expect(formatElapsed("2026-11-09T10:00:00Z", null, Date.parse("2026-11-09T10:00:12Z"))).toBe("12s");
  });

  it("ignores missing or invalid start times", () => {
    expect(formatElapsed(null, null)).toBeNull();
    expect(formatElapsed("invalid", null)).toBeNull();
  });
});
