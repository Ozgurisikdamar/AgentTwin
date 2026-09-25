import type { Metadata } from "next";
import { Suspense } from "react";
import { NewPolicy } from "@/components/runtime/new-policy";

export const metadata: Metadata = { title: "New policy" };

export default function NewPolicyPage() {
  return (
    <Suspense>
      <NewPolicy />
    </Suspense>
  );
}
