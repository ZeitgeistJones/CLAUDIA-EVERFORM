// /sources — the contribution report: every matching image, whether it shaped the
// portrait, and why not if it didn't. Data is the worker's public report.json.

import type { Metadata } from "next";
import { Suspense } from "react";

import { SourcesView } from "@/components/everform/sources-view";

export const metadata: Metadata = {
  title: "Sources · CLAUDIA EVERFORM",
  description: "Every image of Claudia considered for the fused portrait, and why it was used or left out.",
};

export default function SourcesPage() {
  return (
    <Suspense fallback={<div className="min-h-dvh bg-background" />}>
      <SourcesView />
    </Suspense>
  );
}
