"use client";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/** 繰り返しタスク削除時の「今回だけ / 今回以降」選択ダイアログ。 */
export function RecurringDeleteDialog({
  open,
  onOpenChange,
  onDeleteSingle,
  onDeleteFuture,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDeleteSingle: () => void;
  onDeleteFuture: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>繰り返しタスクを削除</DialogTitle>
          <DialogDescription>
            今回だけ削除するか、この回以降の繰り返しをまとめて削除するか選択してください。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-2">
          <Button variant="outline" onClick={onDeleteSingle}>
            今回だけ削除
          </Button>
          <Button variant="destructive" onClick={onDeleteFuture}>
            今回以降を削除
          </Button>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
