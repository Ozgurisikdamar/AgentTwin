import type { Metadata } from "next";
import { Suspense } from "react";
import { ReviewQueue } from "@/components/reviews/review-queue";

export const metadata: Metadata = { title: "Reviews" };

export default function ReviewsPage() {
  return (
    <Suspense>
      <ReviewQueue />
    </Suspense>
  );
}
