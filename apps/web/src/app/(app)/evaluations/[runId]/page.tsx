import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { EvalRunDetailView } from "@/components/evaluations/eval-run-detail";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Evaluation" };

export default async function EvaluationPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  if (!UUID.test(runId)) notFound();
  return (
    <Suspense>
      <EvalRunDetailView runId={runId.toLowerCase()} />
    </Suspense>
  );
}
