"use client";

import { Badge } from "@/components/ui/badge";

/** エージェントトリアージ結果カード。metadata から抽出済みの値を props で受け取る。 */
export function TaskDetailTriageCard({
  triageStatus,
  triageSummary,
  triageHasSummary,
  triageQuestions,
}: {
  triageStatus: string;
  triageSummary: string;
  triageHasSummary: boolean;
  triageQuestions: string[];
}) {
  return (
    <div className="rounded-lg border bg-muted/20 p-3">
      <div className="mb-2 flex items-center gap-2">
        <Badge
          variant={triageStatus === "needs_user" ? "destructive" : "secondary"}
        >
          {triageStatus}
        </Badge>
        <span className="text-sm font-medium">Agent triage</span>
      </div>
      {triageHasSummary ? (
        <p className="whitespace-pre-wrap text-sm text-muted-foreground">
          {triageSummary}
        </p>
      ) : null}
      {triageQuestions.length > 0 ? (
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          {triageQuestions.map((question) => (
            <li key={question}>{question}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
