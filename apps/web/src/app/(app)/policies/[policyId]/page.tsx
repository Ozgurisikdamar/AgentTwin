import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { PolicyDetail } from "@/components/runtime/policy-detail";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Policy" };

export default async function PolicyPage({ params }: { params: Promise<{ policyId: string }> }) {
  const { policyId } = await params;
  if (!UUID.test(policyId)) notFound();
  return (
    <Suspense>
      <PolicyDetail policyId={policyId.toLowerCase()} />
    </Suspense>
  );
}
