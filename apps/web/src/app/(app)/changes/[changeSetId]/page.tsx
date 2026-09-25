import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { ChangeSetDetail } from "@/components/changes/change-set-detail";
import { UUID } from "@/lib/ids";

export const metadata: Metadata = { title: "Change impact" };

export default async function ChangeSetPage({ params }: { params: Promise<{ changeSetId: string }> }) {
  const { changeSetId } = await params;
  if (!UUID.test(changeSetId)) notFound();
  return <ChangeSetDetail changeSetId={changeSetId.toLowerCase()} />;
}
