import type { OperationsAction } from "@/lib/operations-api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";

function receiptText(value: unknown): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "—";
}

function receiptHref(value: unknown): string | undefined {
  if (typeof value !== "string" || !/^https?:\/\//i.test(value) || /[\s\\\u0000-\u001f\u007f]/.test(value)) return undefined;
  try {
    const url = new URL(value);
    return url.hostname && !url.username && !url.password ? url.href : undefined;
  } catch {
    return undefined;
  }
}

/** Read the persisted receipt, never infer one from an action/attempt status. */
export function ActionReceiptPanel({ action }: { action: OperationsAction }) {
  const candidate = action.receipt;
  const receipt = candidate && typeof candidate.id === "string" && candidate.id.trim()
    && candidate.action_id === action.id ? candidate : null;
  const href = receiptHref(receipt?.remote_url);
  const evidenceIds = Array.isArray(receipt?.evidence_artifact_ids)
    ? receipt.evidence_artifact_ids.filter((id): id is string => typeof id === "string" && Boolean(id.trim()))
    : [];

  return (
    <Card size="sm" data-testid="operations-action-receipt">
      <CardHeader className="border-b border-border/70">
        <CardTitle className="inline-flex items-center gap-1 text-sm">
          Receipt / 外部結果
          <HelpPopover label="Receipt / 外部結果">
            保存された実行結果を表示します。審査・発売・販売の状態は外部ステータスと証拠を確認してください。
          </HelpPopover>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 pt-1">
        {receipt ? <>
          <dl className="grid gap-3 text-xs sm:grid-cols-2">
            {[
              ["Receipt ID", receipt.id],
              ["Action ID", receipt.action_id],
              ["対象Action revision", receipt.action_version],
              ["Attempt ID", receipt.attempt_id],
              ["Remote resource ID", receipt.remote_resource_id],
              ["Remote status", receipt.remote_status],
              ["Confirmation level", receipt.confirmation_level],
              ["Provider receipt ref", receipt.provider_receipt_ref],
              ["Provider observed at", receipt.provider_observed_at],
              ["Receipt created at", receipt.created_at],
            ].map(([label, value]) => <div key={String(label)} className="min-w-0">
              <dt className="font-medium text-muted-foreground">{String(label)}</dt>
              <dd className="mt-1 whitespace-pre-wrap break-all">{receiptText(value)}</dd>
            </div>)}
            <div className="min-w-0 sm:col-span-2">
              <dt className="font-medium text-muted-foreground">Remote URL</dt>
              <dd className="mt-1 break-all">{href
                ? <a href={href} target="_blank" rel="noopener noreferrer" className="text-primary underline underline-offset-2">{receiptText(receipt.remote_url)}</a>
                : receiptText(receipt.remote_url)}</dd>
            </div>
            <div className="min-w-0 sm:col-span-2">
              <dt className="font-medium text-muted-foreground">Evidence note</dt>
              <dd className="mt-1 whitespace-pre-wrap break-words">{receiptText(receipt.evidence_note)}</dd>
            </div>
            <div className="min-w-0 sm:col-span-2">
              <dt className="font-medium text-muted-foreground">Evidence artifact IDs</dt>
              <dd className="mt-1 whitespace-pre-wrap break-all">{evidenceIds.join("\n") || "—"}</dd>
            </div>
          </dl>
        </> : <p className="text-sm text-muted-foreground">保存されたReceiptはありません。</p>}
      </CardContent>
    </Card>
  );
}
