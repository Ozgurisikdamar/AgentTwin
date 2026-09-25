import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { SimulationDetail } from "@/components/simulations/simulation-detail";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Simulation" };

export default async function SimulationPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  if (!UUID.test(runId)) notFound();
  return <SimulationDetail runId={runId.toLowerCase()} />;
}
