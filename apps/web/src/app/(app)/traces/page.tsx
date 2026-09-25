import type { Metadata } from "next";
import { Suspense } from "react";
import { TraceExplorer } from "@/components/traces/trace-explorer";

export const metadata: Metadata = { title: "Traces" };

export default function TracesPage() {
  return (
    <Suspense>
      <TraceExplorer />
    </Suspense>
  );
}
