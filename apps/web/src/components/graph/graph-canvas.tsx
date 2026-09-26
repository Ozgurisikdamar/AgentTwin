"use client";

import {
  Background,
  Controls,
  type Edge,
  Handle,
  MarkerType,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo } from "react";
import type { GraphComponent, GraphView } from "@/lib/api/graph";
import { kindLabel, relationPhrase } from "@/lib/changes";
import {
  NODE_HEIGHT,
  NODE_WIDTH,
  edgeEvidence,
  edgeSentence,
  isRiskyTool,
  layoutGraph,
  refKey,
  riskOf,
} from "@/lib/graph";
import { humanize } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useTheme } from "@/components/shell/theme";

type ComponentNodeData = {
  component: GraphComponent;
  focus: boolean;
  selected: boolean;
  /** The blast radius severity of a change set, when one is shown. */
  impact: string | null;
  dimmed: boolean;
};

type ComponentNode = Node<ComponentNodeData, "component">;

const KIND_TONE: Record<string, string> = {
  AGENT: "border-indigo-300 bg-indigo-50",
  AGENT_VERSION: "border-indigo-300 bg-indigo-50",
  PROMPT: "border-violet-300 bg-violet-50",
  MODEL: "border-violet-300 bg-violet-50",
  RETRIEVAL_SOURCE: "border-violet-300 bg-violet-50",
  TOOL: "border-amber-300 bg-amber-50",
  MCP_SERVER: "border-amber-300 bg-amber-50",
  SCENARIO: "border-emerald-300 bg-emerald-50",
  EVALUATOR: "border-emerald-300 bg-emerald-50",
  DATASET: "border-emerald-300 bg-emerald-50",
  POLICY: "border-emerald-300 bg-emerald-50",
};

const IMPACT_RING: Record<string, string> = {
  critical: "ring-2 ring-rose-500",
  high: "ring-2 ring-orange-400",
  medium: "ring-2 ring-amber-300",
  low: "ring-1 ring-slate-300",
};

const handle = "!h-1.5 !w-1.5 !min-h-0 !min-w-0 !border-0 !bg-transparent";

function ComponentNodeView({ data }: NodeProps<ComponentNode>) {
  const c = data.component;
  const risk = c.kind === "TOOL" ? riskOf(c) : "";
  return (
    <div
      className={cn(
        "flex h-full w-full flex-col justify-center rounded-md border px-2 text-left shadow-sm",
        KIND_TONE[c.kind] ?? "border-sky-300 bg-sky-50",
        data.focus && "border-2 border-indigo-600",
        data.selected && "outline outline-2 outline-offset-2 outline-indigo-600",
        data.impact ? IMPACT_RING[data.impact] : null,
        data.dimmed && "opacity-40",
      )}
      data-testid="graph-node"
      data-kind={c.kind}
      data-key={c.key}
      data-focus={data.focus || undefined}
      data-impact={data.impact ?? undefined}
    >
      <Handle id="in-left" type="target" position={Position.Left} className={handle} isConnectable={false} />
      <Handle id="out-left" type="source" position={Position.Left} className={handle} isConnectable={false} />
      <Handle
        id="in-right"
        type="target"
        position={Position.Right}
        className={handle}
        isConnectable={false}
      />
      <Handle
        id="out-right"
        type="source"
        position={Position.Right}
        className={handle}
        isConnectable={false}
      />
      <span className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-slate-600">
        {kindLabel(c.kind)}
        {risk ? (
          <span
            className={cn(
              "rounded px-1 normal-case tracking-normal",
              isRiskyTool(c) ? "bg-rose-600 text-white" : "bg-slate-200 text-slate-700",
            )}
          >
            {humanize(risk)}
          </span>
        ) : null}
      </span>
      <span className="truncate font-mono text-xs text-slate-900" title={c.label}>
        {c.label}
      </span>
    </div>
  );
}

const NODE_TYPES = { component: ComponentNodeView };

// Palette variables, so the edges follow the theme (palette.css).
const EDGE_STROKE = {
  observed: "var(--color-emerald-600)",
  declared: "var(--color-slate-500)",
  inferred: "var(--color-amber-600)",
} as const;

export interface GraphCanvasProps {
  view: GraphView;
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** Severity per `KIND:key` of a change set's blast radius. */
  impact?: ReadonlyMap<string, string> | null;
}

/**
 * The React Flow elements of a view: positions from the layout, handles on
 * the side the edge leaves and arrives, labels a screen reader reads.
 */
export function toFlow(
  view: GraphView,
  selectedId: string | null,
  impact?: ReadonlyMap<string, string> | null,
): { nodes: ComponentNode[]; edges: Edge[] } {
  const layout = layoutGraph(view);
  const byId = new Map(view.nodes.map((n) => [n.id, n]));
  const column = new Map(layout.nodes.map((p) => [p.component.id, p.column]));
  const focus = new Set(view.focus.map((f) => f.id));
  const touching = new Set<string>();
  if (selectedId) {
    touching.add(selectedId);
    for (const e of view.edges) {
      if (e.from === selectedId) touching.add(e.to);
      if (e.to === selectedId) touching.add(e.from);
    }
  }
  const nodes: ComponentNode[] = layout.nodes.map((p) => ({
    id: p.component.id,
    type: "component",
    position: { x: p.x, y: p.y },
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    draggable: false,
    connectable: false,
    ariaLabel: `${kindLabel(p.component.kind)} ${p.component.label}`,
    ariaRole: "button",
    data: {
      component: p.component,
      focus: focus.has(p.component.id),
      selected: p.component.id === selectedId,
      impact: impact?.get(refKey(p.component)) ?? null,
      dimmed: Boolean(selectedId) && !touching.has(p.component.id),
    },
  }));
  const edges: Edge[] = view.edges.map((e) => {
    const from = column.get(e.from) ?? 0;
    const to = column.get(e.to) ?? 0;
    const kind = edgeEvidence(e);
    const active = !selectedId || e.from === selectedId || e.to === selectedId;
    return {
      id: e.id,
      source: e.from,
      target: e.to,
      sourceHandle: from <= to ? "out-right" : "out-left",
      targetHandle: from < to ? "in-left" : "in-right",
      ariaLabel: `${edgeSentence(e, byId, relationPhrase(e.type))} (${kind})`,
      focusable: false,
      selectable: false,
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: EDGE_STROKE[kind] },
      style: {
        stroke: EDGE_STROKE[kind],
        strokeWidth: selectedId && active ? 2 : 1.25,
        strokeDasharray: kind === "inferred" ? "5 4" : undefined,
        opacity: active ? 1 : 0.2,
      },
    };
  });
  return { nodes, edges };
}

/**
 * The view drawn with React Flow: columns by distance from the focus,
 * edges styled by their evidence (observed solid green, declared grey,
 * inferred dashed amber) and pointing the way the relationship reads.
 * Selecting a component highlights its relationships; everything the canvas
 * shows is also in the relationship table next to it.
 */
export default function GraphCanvas({ view, selectedId, onSelect, impact }: GraphCanvasProps) {
  const { nodes, edges } = useMemo(() => toFlow(view, selectedId, impact), [view, selectedId, impact]);
  const { resolved } = useTheme();

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      colorMode={resolved}
      nodeTypes={NODE_TYPES}
      onNodeClick={(_e, node) => onSelect(node.id)}
      nodesDraggable={false}
      nodesConnectable={false}
      edgesFocusable={false}
      fitView
      fitViewOptions={{ padding: 0.1, maxZoom: 1 }}
      minZoom={0.2}
      proOptions={{ hideAttribution: true }}
      ariaLabelConfig={{
        "node.a11yDescription.default": "Press enter or space to show this component's relationships.",
      }}
      onNodesChange={(changes) => {
        // Keyboard selection (enter/space on a focused node) arrives as a change.
        for (const c of changes) if (c.type === "select" && c.selected) onSelect(c.id);
      }}
    >
      <Background gap={24} size={1} />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}
