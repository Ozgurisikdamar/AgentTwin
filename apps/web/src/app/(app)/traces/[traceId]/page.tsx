import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { TraceDetail } from "@/components/traces/trace-detail";

export const metadata: Metadata = { title: "Trace" };

const TRACE_ID = /^[0-9a-f]{32}$/i;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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
