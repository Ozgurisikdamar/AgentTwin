import type { Metadata } from "next";
import { Suspense } from "react";
import { NewEvaluation } from "@/components/evaluations/new-evaluation";

export const metadata: Metadata = { title: "New evaluation" };

export default function NewEvaluationPage() {
  return (
    <Suspense>
      <NewEvaluation />
    </Suspense>
  );
}
