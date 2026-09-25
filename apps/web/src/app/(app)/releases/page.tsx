import type { Metadata } from "next";
import { Suspense } from "react";
import { ReleaseList } from "@/components/releases/release-list";

export const metadata: Metadata = { title: "Releases" };

export default function ReleasesPage() {
  return (
    <Suspense>
      <ReleaseList />
    </Suspense>
  );
}
