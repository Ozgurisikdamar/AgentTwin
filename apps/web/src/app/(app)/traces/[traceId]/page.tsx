import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { TraceDetail } from "@/components/traces/trace-detail";
import { TRACE_ID, UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Trace" };

export default async function TracePage({
  params,
  searchParams,
}: {
  params: Promise<{ traceId: string }>;
  searchParams: Promise<{ project_id?: string }>;
}) {
  const { traceId } = await params;
  const { project_id } = await searchParams;
  if (!TRACE_ID.test(traceId)) notFound();
  return (
    <TraceDetail
      traceId={traceId.toLowerCase()}
      projectId={project_id && UUID.test(project_id) ? project_id : undefined}
    />
  );
}
