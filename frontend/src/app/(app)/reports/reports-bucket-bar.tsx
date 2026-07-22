import { type TimeReportBucket } from "@/lib/task-api";
import { formatSeconds } from "./reports-utils";

export function BucketBar({
  bucket,
  maxSeconds,
  onClick,
}: {
  bucket: TimeReportBucket;
  maxSeconds: number;
  onClick?: () => void;
}) {
  const pct = maxSeconds > 0 ? (bucket.seconds / maxSeconds) * 100 : 0;
  const content = (
    <div className="space-y-1 rounded-md border border-border bg-card p-2.5">
      <div className="flex items-center justify-between text-sm">
        <div className="min-w-0">
          <span className="block truncate">{bucket.label}</span>
          {bucket.project_name && bucket.project_name !== bucket.label && (
            <span className="block truncate text-xs text-muted-foreground">
              {bucket.project_name}
            </span>
          )}
        </div>
        <span className="shrink-0 text-muted-foreground">
          {formatSeconds(bucket.seconds)} ({bucket.entries}件)
        </span>
      </div>
      <div className="h-2 rounded-full bg-muted/55 overflow-hidden">
        <div
          className="h-full rounded-full bg-primary/75 transition-all"
          style={{ width: `${Math.max(pct, 1)}%` }}
        />
      </div>
    </div>
  );
  if (!onClick) return content;
  return (
    <button
      type="button"
      className="w-full rounded-md text-left transition-colors hover:bg-accent/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      onClick={onClick}
    >
      {content}
    </button>
  );
}
