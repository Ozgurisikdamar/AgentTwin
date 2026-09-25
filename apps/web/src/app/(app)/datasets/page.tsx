import type { Metadata } from "next";
import { Suspense } from "react";
import { DatasetList } from "@/components/datasets/dataset-list";

export const metadata: Metadata = { title: "Datasets" };

export default function DatasetsPage() {
  return (
    <Suspense>
      <DatasetList />
    </Suspense>
  );
}
