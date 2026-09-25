import type { Metadata } from "next";
import { Suspense } from "react";
import { DecisionList } from "@/components/runtime/decision-list";

export const metadata: Metadata = { title: "Decisions" };

export default function DecisionsPage() {
  return (
    <Suspense>
      <DecisionList />
    </Suspense>
  );
}
