import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { ReleaseDetail } from "@/components/releases/release-detail";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Release" };

export default async function ReleasePage({ params }: { params: Promise<{ releaseId: string }> }) {
  const { releaseId } = await params;
  if (!UUID.test(releaseId)) notFound();
  return (
    <Suspense>
      <ReleaseDetail releaseId={releaseId.toLowerCase()} />
    </Suspense>
  );
}
