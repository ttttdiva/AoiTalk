"use client";

import { Brain } from "lucide-react";
import { ScopedMemoryManager } from "@/components/memory/scoped-memory-manager";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function ProjectMemoryPanel({
  projectId,
  projectName,
  canWrite = true,
}: {
  projectId: string;
  projectName: string;
  canWrite?: boolean;
}) {
  return (
    <Card className="border-border bg-card shadow-none">
      <CardHeader className="border-b border-border">
        <CardTitle className="flex items-center gap-2 text-base font-semibold"><Brain className="size-4" />プロジェクトメモリ</CardTitle>
      </CardHeader>
      <CardContent>
        {!canWrite && (
          <p className="mb-3 text-sm text-muted-foreground">
            読み取り専用です。変更にはプロジェクトの書き込み権限が必要です。
          </p>
        )}
        <ScopedMemoryManager
          projectId={projectId}
          projectName={projectName}
          projectOnly
          readOnly={!canWrite}
        />
      </CardContent>
    </Card>
  );
}
