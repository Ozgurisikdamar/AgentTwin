import type { Metadata } from "next";
import { Suspense } from "react";
import { ApprovalList } from "@/components/runtime/approval-list";

export const metadata: Metadata = { title: "Approvals" };

export default function ApprovalsPage() {
  return (
    <Suspense>
      <ApprovalList />
    </Suspense>
  );
}
