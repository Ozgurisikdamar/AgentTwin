import type { Metadata } from "next";
import { Suspense } from "react";
import { NewSimulation } from "@/components/simulations/new-simulation";

export const metadata: Metadata = { title: "New simulation" };

export default function NewSimulationPage() {
  return (
    <Suspense>
      <NewSimulation />
    </Suspense>
  );
}
