import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { RegressionDetail } from "@/components/regressions/regression-detail";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Regression" };

export default async function RegressionPage({ params }: { params: Promise<{ regressionId: string }> }) {
  const { regressionId } = await params;
  if (!UUID.test(regressionId)) notFound();
  return (
    <Suspense>
      <RegressionDetail regressionId={regressionId.toLowerCase()} />
    </Suspense>
  );
}
