import type { Metadata } from "next";
import { Suspense } from "react";
import { SimulationList } from "@/components/simulations/simulation-list";

export const metadata: Metadata = { title: "Simulations" };

export default function SimulationsPage() {
  return (
    <Suspense>
      <SimulationList />
    </Suspense>
  );
}
