import type { Metadata } from "next";
import { Suspense } from "react";
import { ScenarioList } from "@/components/scenarios/scenario-list";

export const metadata: Metadata = { title: "Scenarios" };

export default function ScenariosPage() {
  return (
    <Suspense>
      <ScenarioList />
    </Suspense>
  );
}
