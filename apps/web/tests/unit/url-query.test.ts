import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useUrlQuery, withChanges } from "@/lib/use-url-query";

const nav = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/scenarios",
  useRouter: () => ({ replace: nav.replace }),
  useSearchParams: () => new URLSearchParams("severity=high&tag=refunds"),
}));

describe("withChanges", () => {
  it("sets, replaces and removes keys without touching the others", () => {
    const next = withChanges("severity=high&tag=refunds", { severity: "", include_archived: "1", tag: "x" });
    expect(next.toString()).toBe("tag=x&include_archived=1");
    expect(withChanges(new URLSearchParams(), {}).toString()).toBe("");
  });
});

describe("useUrlQuery", () => {
  it("reads the query and replaces the URL in place, without scrolling", () => {
    const { result } = renderHook(() => useUrlQuery());
    expect(result.current.params.get("severity")).toBe("high");
    act(() => result.current.update({ severity: "", include_archived: "1" }));
    expect(nav.replace).toHaveBeenLastCalledWith("/scenarios?tag=refunds&include_archived=1", {
      scroll: false,
    });
    act(() => result.current.replace(new URLSearchParams()));
    expect(nav.replace).toHaveBeenLastCalledWith("/scenarios", { scroll: false });
  });
});
