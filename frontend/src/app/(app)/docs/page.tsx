"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { DocsWorkspace } from "@/components/docs/docs-workspace";

function DocsPageContent() {
  const searchParams = useSearchParams();
  const nodeId = searchParams.get("node_id");
  return <DocsWorkspace initialNodeId={nodeId} />;
}

export default function DocsPage() {
  return (
    <Suspense>
      <DocsPageContent />
    </Suspense>
  );
}
