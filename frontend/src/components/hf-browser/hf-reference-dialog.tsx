"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { hfAddReference, hfDeleteAccount, hfListAccounts, type HfAccount } from "@/lib/hf-api";
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
  const [accounts, setAccounts] = useState<HfAccount[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [deletingAccountId, setDeletingAccountId] = useState<string | null>(null);

  const loadAccounts = useCallback(async () => {
    setAccountsLoading(true);
    try {
      const result = await hfListAccounts();
      setAccounts(result.accounts);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "HFアカウント一覧を取得できませんでした");
    } finally {
      setAccountsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void loadAccounts();
    } else {
      setValue("");
    }
  }, [loadAccounts, open]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!value.trim() || saving) return;
    setSaving(true);
    try {
      const result = await hfAddReference(value.trim());
      if (result.kind === "account") {
        toast.success(`${result.account.username} を追加しました`);
        window.dispatchEvent(new Event("aoitalk:hf-accounts-changed"));
        await loadAccounts();
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

  const removeAccount = async (account: HfAccount) => {
    if (deletingAccountId || !window.confirm(`${account.username} のHFアカウントを削除しますか？`)) return;
    setDeletingAccountId(account.id);
    try {
      await hfDeleteAccount(account.id);
      setAccounts((current) => current.filter((item) => item.id !== account.id));
      window.dispatchEvent(new Event("aoitalk:hf-accounts-changed"));
      toast.success(`${account.username} を削除しました`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "HFアカウントの削除に失敗しました");
    } finally {
      setDeletingAccountId(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>HF参照・アカウント設定</DialogTitle>
            <DialogDescription>
              HFトークン（登録時に接続確認）、owner/repository、またはHugging Face URLを入力してください。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 rounded-md border p-2">
            <div className="text-xs font-medium">登録済みアカウント</div>
            {accountsLoading ? (
              <p className="text-xs text-muted-foreground">読み込み中...</p>
            ) : accounts.length === 0 ? (
              <p className="text-xs text-muted-foreground">未設定（トークンは再表示されません）</p>
            ) : (
              <div className="space-y-1">
                {accounts.map((account) => (
                  <div key={account.id} className="flex items-center justify-between gap-2 rounded border px-2 py-1 text-xs">
                    <span className="truncate">{account.label || account.username}</span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => void removeAccount(account)}
                      disabled={deletingAccountId === account.id}
                    >
                      {deletingAccountId === account.id ? "削除中..." : "削除"}
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>
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
              {saving ? "接続確認中..." : "登録 / 接続確認"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
