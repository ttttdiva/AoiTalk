"use client";

import { Brain } from "lucide-react";
import { ScopedMemoryManager } from "@/components/memory/scoped-memory-manager";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export function ProjectMemoryPanel({
  projectId,
  projectName,
  // Permission projections are fail-closed. The Projects API normally
  // supplies this value for owners/members; an omitted value must not expose
  // mutation controls while that projection is loading or stale.
  canWrite = false,
}: {
  projectId: string;
  projectName: string;
  canWrite?: boolean;
}) {
  return (
    <Card className="mx-auto w-full max-w-5xl border-border bg-card shadow-none">
      <CardHeader className="border-b border-border">
        <CardTitle className="flex items-center gap-2 text-base font-semibold"><Brain className="size-4" />プロジェクトメモリ</CardTitle>
        <CardDescription>このプロジェクトで再利用する確定メモリと、確認待ちの候補を管理します。</CardDescription>
      </CardHeader>
      <CardContent className="pt-5">
        {!canWrite && (
          <p className="mb-3 rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground">
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
