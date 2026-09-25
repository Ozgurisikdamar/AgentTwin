import type { Metadata } from "next";
import { Suspense } from "react";
import { PolicyList } from "@/components/runtime/policy-list";

export const metadata: Metadata = { title: "Policies" };

export default function PoliciesPage() {
  return (
    <Suspense>
      <PolicyList />
    </Suspense>
  );
}
