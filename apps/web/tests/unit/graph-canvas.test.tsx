import { fireEvent, render, screen } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import GraphCanvas, { toFlow } from "@/components/graph/graph-canvas";
import { ThemeProvider } from "@/components/shell/theme";
import type { GraphView } from "@/lib/api/graph";
import { filterView } from "@/lib/graph";
import type { Theme } from "@/lib/theme";
import { livePromptNeighbourhood } from "./graph-fixtures";

// React Flow measures the DOM; jsdom has no layout (the setup React Flow's
// own documentation gives for jsdom).
beforeAll(() => {
  class ResizeObserver {
    constructor(private readonly callback: ResizeObserverCallback) {}
    observe(target: Element) {
      const contentRect = { width: 1200, height: 800 } as DOMRectReadOnly;
      this.callback(
        [{ target, contentRect } as ResizeObserverEntry],
        this as unknown as globalThis.ResizeObserver,
      );
    }
    unobserve() {}
    disconnect() {}
  }
  class DOMMatrixReadOnly {
    m22: number;
    constructor(transform: string) {
      const scale = transform?.match(/scale\(([1-9.])\)/)?.[1];
      this.m22 = scale !== undefined ? Number(scale) : 1;
    }
  }
  vi.stubGlobal("ResizeObserver", ResizeObserver);
  vi.stubGlobal("DOMMatrixReadOnly", DOMMatrixReadOnly);
  Object.defineProperties(HTMLElement.prototype, {
    offsetHeight: {
      configurable: true,
      get(this: HTMLElement) {
        return Number.parseFloat(this.style.height) || 1;
      },
    },
    offsetWidth: {
      configurable: true,
      get(this: HTMLElement) {
        return Number.parseFloat(this.style.width) || 1;
      },
    },
  });
  (SVGElement.prototype as unknown as { getBBox: () => DOMRect }).getBBox = () =>
    ({ x: 0, y: 0, width: 0, height: 0 }) as DOMRect;
});

const view = livePromptNeighbourhood as GraphView;
const id = (kind: string, key: string) => view.nodes.find((n) => n.kind === kind && n.key === key)!.id;

function renderCanvas(props: Partial<Parameters<typeof GraphCanvas>[0]> = {}, theme: Theme = "light") {
  const onSelect = vi.fn();
  render(
    <ThemeProvider initial={theme}>
      <div style={{ width: 1200, height: 800 }}>
        <GraphCanvas view={view} selectedId={null} onSelect={onSelect} {...props} />
      </div>
    </ThemeProvider>,
  );
  return onSelect;
}

describe("GraphCanvas", () => {
  it("draws every component of the view, the focus marked, risky tools labelled", async () => {
    renderCanvas();
    const nodes = await screen.findAllByTestId("graph-node");
    expect(nodes).toHaveLength(18);
    const focus = nodes.filter((n) => n.hasAttribute("data-focus"));
    expect(focus.map((n) => n.getAttribute("data-kind"))).toEqual(["PROMPT"]);
    const refund = nodes.find((n) => n.getAttribute("data-key") === "refund_payment")!;
    expect(refund).toHaveTextContent("Write irreversible");
    expect(screen.getByRole("button", { name: /Tool refund_payment/ })).toBeInTheDocument();
  });

  it("draws in the page's theme", async () => {
    renderCanvas({}, "dark");
    await screen.findAllByTestId("graph-node");
    expect(document.querySelector(".react-flow")).toHaveClass("dark");
  });

  it("names every edge the way it reads, with how it is known", () => {
    // jsdom cannot measure handles, so React Flow draws no edge there: the
    // elements are checked as they are handed to it.
    const { edges } = toFlow(view, null);
    const labels = edges.map((e) => e.ariaLabel);
    expect(labels).toHaveLength(46);
    expect(labels).toContain("refund_payment writes payments-db (declared)");
    expect(labels).toContain("support-refund-agent@1.3.0 uses refund_payment (observed)");
    expect(labels).toContain("export_customer_data can change orders-api (declared)");
    const writes = edges.find((e) => e.ariaLabel === "refund_payment writes payments-db (declared)")!;
    // Left to right: out of the tool's right side into the database's left.
    expect([writes.sourceHandle, writes.targetHandle]).toEqual(["out-right", "in-left"]);
    // The agent version uses the prompt it sits right of: the edge runs back.
    const usesPrompt = edges.find((e) => e.ariaLabel?.startsWith("support-refund-agent@1.3.0 uses prompt"))!;
    expect([usesPrompt.sourceHandle, usesPrompt.targetHandle]).toEqual(["out-left", "in-right"]);
    const observed = edges.find((e) => e.ariaLabel?.endsWith("(observed)"))!;
    expect(observed.style).toMatchObject({ stroke: "var(--color-emerald-600)", strokeDasharray: undefined });
  });

  it("dashes what is only inferred and fades what the selection does not touch", () => {
    const inferred = structuredClone(view);
    inferred.edges[0] = { ...inferred.edges[0]!, sources: ["INFERRED"], certain: false };
    const { edges, nodes } = toFlow(inferred, id("TOOL", "refund_payment"));
    expect(edges[0]!.style).toMatchObject({ strokeDasharray: "5 4", stroke: "var(--color-amber-600)" });
    expect(edges[0]!.ariaLabel).toMatch(/\(inferred\)$/);
    const touching = edges.filter(
      (e) => e.source === id("TOOL", "refund_payment") || e.target === id("TOOL", "refund_payment"),
    );
    expect(touching.every((e) => e.style?.opacity === 1 && e.style?.strokeWidth === 2)).toBe(true);
    expect(edges.filter((e) => !touching.includes(e)).every((e) => e.style?.opacity === 0.2)).toBe(true);
    expect(nodes.find((n) => n.id === id("DATABASE", "payments-db"))!.data.dimmed).toBe(false);
    expect(nodes.find((n) => n.id === id("TOOL", "lookup_customer"))!.data.dimmed).toBe(true);
  });

  it("selects a component on click and outlines the blast radius by severity", async () => {
    const impact = new Map([
      ["TOOL:refund_payment", "critical"],
      ["SERVICE:payments-api", "high"],
    ]);
    const onSelect = renderCanvas({ impact, selectedId: id("TOOL", "refund_payment") });
    const nodes = await screen.findAllByTestId("graph-node");
    const refund = nodes.find((n) => n.getAttribute("data-key") === "refund_payment")!;
    expect(refund).toHaveAttribute("data-impact", "critical");
    expect(nodes.find((n) => n.getAttribute("data-key") === "payments-db")).not.toHaveAttribute(
      "data-impact",
    );
    // Components the selected one is not related to are dimmed.
    expect(nodes.find((n) => n.getAttribute("data-key") === "lookup_customer")!.className).toContain(
      "opacity-40",
    );
    expect(refund.className).not.toContain("opacity-40");
    fireEvent.click(nodes.find((n) => n.getAttribute("data-key") === "payments-db")!);
    expect(onSelect).toHaveBeenCalledWith(id("DATABASE", "payments-db"));
  });

  it("draws a filtered view as given", async () => {
    renderCanvas({ view: filterView(view, { evidence: "observed" }) });
    expect(await screen.findAllByTestId("graph-node")).toHaveLength(1);
  });
});
