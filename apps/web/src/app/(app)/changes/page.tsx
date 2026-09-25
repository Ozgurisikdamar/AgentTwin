import type { Metadata } from "next";
import { Suspense } from "react";
import { ChangeSetList } from "@/components/changes/change-set-list";

export const metadata: Metadata = { title: "Changes" };

export default function ChangesPage() {
  return (
    <Suspense>
      <ChangeSetList />
    </Suspense>
  );
}
