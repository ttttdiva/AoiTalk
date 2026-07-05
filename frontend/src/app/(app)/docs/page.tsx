import { Suspense } from "react";
import { DocsWorkspace } from "@/components/docs/docs-workspace";

export default function DocsPage() {
  return (
    <Suspense>
      <DocsWorkspace />
    </Suspense>
  );
}
