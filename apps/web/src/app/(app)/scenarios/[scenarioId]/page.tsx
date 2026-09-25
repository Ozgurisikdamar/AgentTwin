import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { ScenarioEditor } from "@/components/scenarios/scenario-editor";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Scenario" };

export default async function ScenarioPage({ params }: { params: Promise<{ scenarioId: string }> }) {
  const { scenarioId } = await params;
  if (!UUID.test(scenarioId)) notFound();
  return (
    <Suspense>
      <ScenarioEditor key={scenarioId} scenarioId={scenarioId.toLowerCase()} />
    </Suspense>
  );
}
