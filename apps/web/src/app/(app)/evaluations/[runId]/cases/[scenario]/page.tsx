import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { EvalCaseView } from "@/components/evaluations/case-comparison";
import { NAME, UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Compared case" };

export default async function EvalCasePage({
  params,
}: {
  params: Promise<{ runId: string; scenario: string }>;
}) {
  const { runId, scenario } = await params;
  const name = decodeURIComponent(scenario);
  if (!UUID.test(runId) || !NAME.test(name)) notFound();
  return <EvalCaseView runId={runId.toLowerCase()} scenario={name} />;
}
