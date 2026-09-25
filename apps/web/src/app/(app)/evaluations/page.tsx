import type { Metadata } from "next";
import { Suspense } from "react";
import { EvalRunList } from "@/components/evaluations/eval-run-list";

export const metadata: Metadata = { title: "Evaluations" };

export default function EvaluationsPage() {
  return (
    <Suspense>
      <EvalRunList />
    </Suspense>
  );
}
