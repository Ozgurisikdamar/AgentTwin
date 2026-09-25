import type { Metadata } from "next";
import { Suspense } from "react";
import { GraphExplorer } from "@/components/graph/graph-explorer";

export const metadata: Metadata = { title: "Dependency graph" };

export default function GraphPage() {
  return (
    <Suspense>
      <GraphExplorer />
    </Suspense>
  );
}
