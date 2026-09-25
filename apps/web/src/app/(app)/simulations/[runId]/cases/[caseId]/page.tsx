import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { CaseDetail } from "@/components/simulations/case-detail";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Scenario result" };

export default async function CasePage({ params }: { params: Promise<{ runId: string; caseId: string }> }) {
  const { runId, caseId } = await params;
  if (!UUID.test(runId) || !UUID.test(caseId)) notFound();
  return <CaseDetail runId={runId.toLowerCase()} caseId={caseId.toLowerCase()} />;
}
