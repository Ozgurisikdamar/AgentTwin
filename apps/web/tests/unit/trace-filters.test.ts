import { describe, expect, it } from "vitest";
import {
  type TraceFilters,
  activeFilterCount,
  filtersToParams,
  isoToLocalInput,
  localInputToISO,
  parseFilters,
  withFilter,
} from "@/lib/trace-filters";

describe("trace filters", () => {
  it("parses valid values and drops invalid ones", () => {
    const f = parseFilters(
      new URLSearchParams(
        "agent=support-refund-agent&status=ERROR&outcome=bogus&project_id=not-a-uuid&min_duration_ms=12.5&max_cost_usd=-1&from=2026-09-24T10:00:00Z&flagged=maybe&unknown=1",
      ),
    );
    expect(f).toEqual({
      agent: "support-refund-agent",
      status: "ERROR",
      min_duration_ms: "12.5",
      from: "2026-09-24T10:00:00Z",
    });
  });

  it("drops what the trace service would refuse", () => {
    // Enum values are the service's canonical ones, and times are RFC 3339:
    // `?from=2026-09-24` answered `400 INVALID_FILTER` and broke the page.
    const f = parseFilters(
      new URLSearchParams(
        "source=simulation&from=2026-09-24&to=2026-09-24T10:00:00.5%2B03:00&status=error&outcome=Failure",
      ),
    );
    expect(f).toEqual({ source: "simulation", to: "2026-09-24T10:00:00.5+03:00" });
    expect(parseFilters(new URLSearchParams("source=bogus&from=Sep 24 2026"))).toEqual({});
    expect(filtersToParams({ source: "bogus" } as unknown as TraceFilters).toString()).toBe("");
  });

  it("serializes in a stable order and round-trips", () => {
    const f = { tool: "refund_payment", agent: "a", project_id: "01a0d51e-3e2e-7eef-931b-c2fe2c2b4f0a" };
    const qs = filtersToParams(f).toString();
    expect(qs).toBe("project_id=01a0d51e-3e2e-7eef-931b-c2fe2c2b4f0a&agent=a&tool=refund_payment");
    expect(parseFilters(new URLSearchParams(qs))).toEqual(f);
  });

  it("sets and clears single filters without mutating", () => {
    const f = { agent: "a" };
    const g = withFilter(f, "signal", "retry");
    expect(g).toEqual({ agent: "a", signal: "retry" });
    expect(f).toEqual({ agent: "a" });
    expect(withFilter(g, "agent", "")).toEqual({ signal: "retry" });
    expect(activeFilterCount(g)).toBe(2);
  });

  it("converts datetime-local values through UTC", () => {
    const iso = localInputToISO("2026-09-24T10:30");
    expect(iso).toMatch(/Z$/);
    expect(isoToLocalInput(iso)).toBe("2026-09-24T10:30");
    expect(localInputToISO("")).toBeUndefined();
    expect(isoToLocalInput("garbage")).toBe("");
  });
});
