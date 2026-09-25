import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { DatasetDetailView } from "@/components/datasets/dataset-detail";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Dataset" };

export default async function DatasetPage({ params }: { params: Promise<{ datasetId: string }> }) {
  const { datasetId } = await params;
  if (!UUID.test(datasetId)) notFound();
  return (
    <Suspense>
      <DatasetDetailView datasetId={datasetId.toLowerCase()} />
    </Suspense>
  );
}
