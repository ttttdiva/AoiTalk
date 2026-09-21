"use client";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/**
 * 繰り返しタスクの日時変更を、今回の発生回だけに適用するか、
 * 今回以降の発生回へ適用するかを選ぶダイアログ。
 *
 * このコンポーネント自身は mutation を行わない。閉じる操作（Escape、
 * outside click、キャンセルを含む）は onOpenChange(false) だけを通知し、
 * 呼び出し側が保留中の変更を破棄する。
 */
export type RecurringDateChangeDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onApplySingle: () => void;
  onApplyFuture: () => void;
};

export function RecurringDateChangeDialog({
  open,
  onOpenChange,
  onApplySingle,
  onApplyFuture,
}: RecurringDateChangeDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>繰り返しタスクの日時を変更</DialogTitle>
          <DialogDescription>
            この日時変更をどこまで反映しますか？
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <Button variant="outline" onClick={onApplySingle}>
              今回のみ
            </Button>
            <p className="text-xs text-muted-foreground">
              選択した回だけ日時を変更します
            </p>
          </div>
          <div className="flex flex-col gap-1">
            <Button variant="outline" onClick={onApplyFuture}>
              今回以降
            </Button>
            <p className="text-xs text-muted-foreground">
              この回と、それ以降の繰り返しの日時を変更します
            </p>
          </div>
          <button
            type="button"
            className="inline-flex h-8 items-center justify-center rounded-lg px-2.5 text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="recurring-date-change-cancel"
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
              onOpenChange(false);
            }}
          >
            キャンセル
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
