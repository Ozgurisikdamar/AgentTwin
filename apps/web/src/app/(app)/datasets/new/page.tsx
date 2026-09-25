import type { Metadata } from "next";
import { Suspense } from "react";
import { NewDataset } from "@/components/datasets/new-dataset";

export const metadata: Metadata = { title: "New dataset" };

export default function NewDatasetPage() {
  return (
    <Suspense>
      <NewDataset />
    </Suspense>
  );
}
