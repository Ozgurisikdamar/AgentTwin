import type { Metadata } from "next";
import { Suspense } from "react";
import { RegressionList } from "@/components/regressions/regression-list";

export const metadata: Metadata = { title: "Regressions" };

export default function RegressionsPage() {
  return (
    <Suspense>
      <RegressionList />
    </Suspense>
  );
}
