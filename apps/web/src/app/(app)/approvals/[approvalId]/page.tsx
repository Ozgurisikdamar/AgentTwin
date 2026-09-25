import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { ApprovalDetail } from "@/components/runtime/approval-detail";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Approval request" };

export default async function ApprovalPage({ params }: { params: Promise<{ approvalId: string }> }) {
  const { approvalId } = await params;
  if (!UUID.test(approvalId)) notFound();
  return (
    <Suspense>
      <ApprovalDetail approvalId={approvalId.toLowerCase()} />
    </Suspense>
  );
}
