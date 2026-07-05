import { Suspense } from "react";
import { DocsWorkspace } from "@/components/docs/docs-workspace";

export default async function DocsNodePage({
  params,
}: {
  params: Promise<{ nodeId: string }>;
}) {
  const { nodeId } = await params;
  return (
    <Suspense>
      <DocsWorkspace initialNodeId={nodeId} />
    </Suspense>
  );
}
