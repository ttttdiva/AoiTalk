"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { hfAddReference } from "@/lib/hf-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function HfReferenceDialog({
  open,
  onOpenChange,
  onAdded,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdded: (path?: string) => void;
}) {
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) setValue("");
  }, [open]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!value.trim() || saving) return;
    setSaving(true);
    try {
      const result = await hfAddReference(value.trim());
      if (result.kind === "account") {
        toast.success(`${result.account.username} を追加しました`);
        onAdded();
      } else {
        toast.success(`${result.repositories.length}件のリポジトリ参照を追加しました`);
        onAdded(result.repositories[0]?.path);
      }
      onOpenChange(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "HF参照の追加に失敗しました");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>HF参照を追加</DialogTitle>
            <DialogDescription>
              HFトークン、owner/repository、またはHugging Face URLを入力してください。
            </DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            type={value.trimStart().startsWith("hf_") ? "password" : "text"}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="hf_... または owner/repository"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
          />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              キャンセル
            </Button>
            <Button type="submit" disabled={saving || !value.trim()}>
              {saving ? "確認中..." : "追加"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
