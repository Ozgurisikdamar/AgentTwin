import type { Metadata } from "next";
import { Suspense } from "react";
import { ScenarioEditor } from "@/components/scenarios/scenario-editor";

export const metadata: Metadata = { title: "New scenario" };

export default function NewScenarioPage() {
  return (
    <Suspense>
      <ScenarioEditor />
    </Suspense>
  );
}
