"use client";

import { useState } from "react";
import { Brain, ChevronDown, ChevronUp } from "lucide-react";
import { ScopedMemoryManager } from "@/components/memory/scoped-memory-manager";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useProject } from "@/contexts/project-context";

export function MemorySection() {
  const [expanded, setExpanded] = useState(false);
  const { selectedProject } = useProject();

  return (
    <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0">
      <CardHeader
        className="cursor-pointer select-none"
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        aria-controls="memory-content"
        onClick={() => setExpanded((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setExpanded((value) => !value);
          }
        }}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <Brain className="size-4" />
            メモリ
          </span>
          {expanded ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
        </CardTitle>
      </CardHeader>
      {expanded && (
        <CardContent id="memory-content">
          <ScopedMemoryManager
            projectId={selectedProject?.source === "remote" ? undefined : selectedProject?.id}
            projectName={selectedProject?.name}
          />
        </CardContent>
      )}
    </Card>
  );
}
