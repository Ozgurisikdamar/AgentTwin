import type { Metadata } from "next";
import { Suspense } from "react";
import { JudgesView } from "@/components/judges/judges-view";

export const metadata: Metadata = { title: "Judge calibration" };

export default function JudgesPage() {
  return (
    <Suspense>
      <JudgesView />
    </Suspense>
  );
}
