"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AppSelect } from "@/components/ui/app-select";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type Share = {
  id: string;
  user_id: string;
  permission: "read" | "write" | string;
  user?: { username?: string | null; display_name?: string | null; email?: string | null };
};
type UserOption = { id: string; username: string; display_name?: string | null; email?: string | null };

export function DocsShareDialog({
  open,
  nodeId,
  nodeTitle,
  apiFetch,
  onOpenChange,
}: {
  open: boolean;
  nodeId: string | null;
  nodeTitle?: string;
  apiFetch: <T>(path: string, init?: RequestInit) => Promise<T>;
  onOpenChange: (open: boolean) => void;
}) {
  const [shares, setShares] = useState<Share[]>([]);
  const [users, setUsers] = useState<UserOption[]>([]);
  const [query, setQuery] = useState("");
  const [selectedUserId, setSelectedUserId] = useState("");
  const [permission, setPermission] = useState<"read" | "write">("read");
  const [loading, setLoading] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !nodeId) return;
    let cancelled = false;
    // Keep the first tick asynchronous so the dialog can paint immediately.
    // DocsWorkspace keys this component by node and handleOpenChange clears
    // picker state on close; resetting state here could clobber a query typed
    // during the opening frame.
    const timer = window.setTimeout(() => {
      if (cancelled) return;
      setLoading(true);
      setError(null);
      void apiFetch<{ shares: Share[] }>(`/api/docs/shares/${nodeId}`)
        .then((data) => {
          if (!cancelled) setShares(data.shares ?? []);
        })
        .catch((reason) => {
          if (!cancelled) setError(reason instanceof Error ? reason.message : "共有設定を読み込めません");
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [apiFetch, nodeId, open]);

  useEffect(() => {
    const normalizedQuery = query.trim();
    if (!open || !nodeId || !normalizedQuery) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      if (cancelled) return;
      setSearchLoading(true);
      setSearchError(null);
      void apiFetch<{ users: UserOption[] }>(
        `/api/users/search?q=${encodeURIComponent(normalizedQuery)}`,
      )
        .then((data) => {
          if (cancelled) return;
          const nextUsers = data.users ?? [];
          setUsers(nextUsers);
          setSelectedUserId((current) =>
            current && nextUsers.some((candidate) => candidate.id === current)
              ? current
              : "",
          );
        })
        .catch((reason) => {
          if (!cancelled) {
            setUsers([]);
            setSearchError(
              reason instanceof Error
                ? reason.message
                : "ユーザー検索に失敗しました",
            );
          }
        })
        .finally(() => {
          if (!cancelled) setSearchLoading(false);
        });
    }, 180);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [apiFetch, nodeId, open, query]);

  const selectableUsers = users.filter(
    (candidate) => !shares.some((share) => share.user_id === candidate.id),
  );
  const selectedUserIsAvailable = selectableUsers.some(
    (candidate) => candidate.id === selectedUserId,
  );

  async function addShare() {
    if (!nodeId || !selectedUserIsAvailable) return;
    setError(null);
    try {
      const data = await apiFetch<{ share: Share }>(`/api/docs/shares/${nodeId}`, {
        method: "POST",
        body: JSON.stringify({ user_id: selectedUserId, permission }),
      });
      if (data.share) {
        setShares((current) => [
          ...current.filter((item) => item.user_id !== data.share.user_id),
          data.share,
        ]);
      }
      setSelectedUserId("");
      setQuery("");
      setUsers([]);
      setSearchLoading(false);
      setSearchError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "共有設定を保存できません");
    }
  }

  async function updateShare(share: Share, nextPermission: string) {
    if (!nodeId) return;
    try {
      await apiFetch(`/api/docs/shares/${nodeId}/${share.id}`, {
        method: "PATCH",
        body: JSON.stringify({ permission: nextPermission }),
      });
      setShares((current) => current.map((item) => item.id === share.id ? { ...item, permission: nextPermission } : item));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "共有権限を変更できません");
    }
  }

  async function revokeShare(share: Share) {
    if (!nodeId) return;
    try {
      await apiFetch(`/api/docs/shares/${nodeId}/${share.id}`, { method: "DELETE" });
      setShares((current) => current.filter((item) => item.id !== share.id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "共有を解除できません");
    }
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setShares([]);
      setUsers([]);
      setQuery("");
      setSelectedUserId("");
      setPermission("read");
      setLoading(false);
      setSearchLoading(false);
      setSearchError(null);
      setError(null);
    }
    onOpenChange(nextOpen);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Docsを共有{nodeTitle ? `: ${nodeTitle}` : ""}</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <label className="text-xs text-muted-foreground" htmlFor="docs-share-user-search">ユーザーを追加</label>
            <Input
              id="docs-share-user-search"
              value={query}
              onChange={(event) => {
                const nextQuery = event.target.value;
                setQuery(nextQuery);
                setSelectedUserId("");
                setUsers([]);
                setSearchLoading(Boolean(nextQuery.trim()));
                setSearchError(null);
              }}
              placeholder="名前・メールアドレス・ユーザー名"
            />
            {searchLoading ? (
              <p className="text-xs text-muted-foreground">ユーザーを検索中…</p>
            ) : null}
            {!searchLoading && query.trim() && selectableUsers.length === 0 && !searchError ? (
              <p className="text-xs text-muted-foreground">一致するユーザーがいません。</p>
            ) : null}
            {selectableUsers.length > 0 ? (
              <AppSelect
                value={selectedUserIsAvailable ? selectedUserId : ""}
                onChange={(event) => setSelectedUserId(event.target.value)}
                className="h-9 w-full rounded border bg-background px-2 text-sm"
                aria-label="共有するユーザー"
              >
                <option value="">ユーザーを選択…</option>
                {selectableUsers.map((candidate) => (
                  <option key={candidate.id} value={candidate.id}>
                    {candidate.display_name || candidate.username}{candidate.email ? ` (${candidate.email})` : ""}
                  </option>
                ))}
              </AppSelect>
            ) : null}
            {searchError ? <p className="text-xs text-destructive">{searchError}</p> : null}
            <div className="flex items-center gap-2">
              <AppSelect
                value={permission}
                onChange={(event) => setPermission(event.target.value as "read" | "write")}
                className="h-9 rounded border bg-background px-2 text-sm"
                aria-label="共有権限"
              >
                <option value="read">閲覧のみ</option>
                <option value="write">編集可能</option>
              </AppSelect>
              <Button type="button" size="sm" onClick={() => void addShare()} disabled={!selectedUserIsAvailable}>追加</Button>
            </div>
          </div>
          <div className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground">共有中のユーザー</p>
            {loading ? <p className="text-sm text-muted-foreground">読み込み中…</p> : null}
            {!loading && shares.length === 0 ? <p className="text-sm text-muted-foreground">まだ共有されていません。</p> : null}
            {shares.map((share) => (
              <div key={share.id} className="flex items-center justify-between gap-2 rounded border px-2 py-1.5 text-sm">
                <span className="min-w-0 truncate">{share.user?.display_name || share.user?.username || share.user?.email || share.user_id}</span>
                <div className="flex shrink-0 items-center gap-1">
                  <AppSelect
                    value={share.permission}
                    onChange={(event) => void updateShare(share, event.target.value)}
                    className="h-8 rounded border bg-background px-1.5 text-xs"
                    aria-label="共有権限を変更"
                  >
                    <option value="read">閲覧のみ</option>
                    <option value="write">編集可能</option>
                  </AppSelect>
                  <Button type="button" variant="ghost" size="sm" className="h-8 px-2 text-xs" onClick={() => void revokeShare(share)}>解除</Button>
                </div>
              </div>
            ))}
            {error ? <p className="text-xs text-destructive">{error}</p> : null}
          </div>
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => handleOpenChange(false)}>閉じる</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
