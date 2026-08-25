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
 * 繰り返しタスク削除時の「今回だけ / 今回以降 / 繰り返しタスク全体」選択ダイアログ。
 * 「今回以降を削除」は発生回の切り詰めなので、繰り返しタスクそのものを消したい場合は
 * 「この繰り返しタスクを削除」を選ぶ。
 */
export function RecurringDeleteDialog({
  open,
  onOpenChange,
  onDeleteSingle,
  onDeleteFuture,
  onDeleteSeries,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDeleteSingle: () => void;
  onDeleteFuture: () => void;
  onDeleteSeries: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>繰り返しタスクを削除</DialogTitle>
          <DialogDescription>
            削除する範囲を選択してください。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <Button variant="outline" onClick={onDeleteSingle}>
              今回だけ削除
            </Button>
            <p className="text-xs text-muted-foreground">
              この回だけを削除します。ほかの回は残ります。
            </p>
          </div>
          <div className="flex flex-col gap-1">
            <Button
              variant="outline"
              className="text-destructive hover:text-destructive"
              onClick={onDeleteFuture}
            >
              今回以降を削除
            </Button>
            <p className="text-xs text-muted-foreground">
              この回とそれ以降の回を削除します。過去の回は残ります。
            </p>
          </div>
          <div className="flex flex-col gap-1">
            <Button variant="destructive" onClick={onDeleteSeries}>
              この繰り返しタスクを削除
            </Button>
            <p className="text-xs text-muted-foreground">
              すべての回が削除されます。タスク本体ごと消えるため元に戻せません。
            </p>
          </div>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
