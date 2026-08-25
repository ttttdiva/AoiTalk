"use client";

import { useState, useEffect, useCallback } from "react";
import useSWR from "swr";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Bot, Loader2, Users } from "lucide-react";

interface CharacterInfo {
  id: string;
  name: string;
  slug: string;
  character_type: string;
  is_enabled: boolean;
}

interface UserInfo {
  id: string;
  username: string;
  display_name?: string | null;
  email?: string | null;
}

interface GroupChatDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreateGroup: (
    characterNames: string[],
    projectId?: string,
    userIds?: string[],
  ) => void;
  projectId?: string;
}

// SWR キャッシュキー。グループチャット作成ダイアログの参加候補は一意なので固定文字列。
const GROUP_CHAT_PARTICIPANTS_SWR_KEY = "chat/group-chat-participants";

type GroupChatParticipants = {
  characters: CharacterInfo[];
  users: UserInfo[];
};

const EMPTY_CHARACTERS: CharacterInfo[] = [];
const EMPTY_USERS: UserInfo[] = [];

async function pyFetch<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  return res.json();
}

// 参加候補（有効なキャラクター + 招待可能ユーザー）を取得する。
// 各リクエストは個別に catch して空配列へフォールバックする（旧実装の挙動を踏襲）。
async function fetchGroupChatParticipants(): Promise<GroupChatParticipants> {
  const [characterData, userData] = await Promise.all([
    pyFetch<{ success: boolean; characters: CharacterInfo[] }>(
      "/characters/manage",
    ).catch(() => ({ success: false, characters: [] })),
    pyFetch<{ users: UserInfo[] }>("/conversations/participants/users").catch(
      () => ({ users: [] }),
    ),
  ]);
  return {
    characters: (characterData.characters || []).filter((c) => c.is_enabled),
    users: userData.users || [],
  };
}

export function GroupChatDialog({
  open,
  onOpenChange,
  onCreateGroup,
  projectId,
}: GroupChatDialogProps) {
  const [selectedSlugs, setSelectedSlugs] = useState<Set<string>>(new Set());
  const [selectedUsers, setSelectedUsers] = useState<Set<string>>(new Set());
  const [creating, setCreating] = useState(false);

  // 取得・キャッシュ・重複排除は SWR に委譲する。自動 revalidation は全て無効化し、
  // 従来どおり「ダイアログを開くたびに再取得」する挙動は下の useEffect の mutate で駆動する。
  const { data, isValidating, mutate } = useSWR<GroupChatParticipants>(
    GROUP_CHAT_PARTICIPANTS_SWR_KEY,
    fetchGroupChatParticipants,
    {
      revalidateOnMount: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      keepPreviousData: true,
      dedupingInterval: 0,
    },
  );

  const characters = data?.characters ?? EMPTY_CHARACTERS;
  const users = data?.users ?? EMPTY_USERS;
  // 取得中はローディング表示。開くたびに mutate で再取得するため isValidating を使う
  // （初回・再開いずれも取得完了までスピナーを出す従来挙動を維持）。
  const loading = isValidating;

  useEffect(() => {
    if (!open) return;
    void mutate();
    setSelectedSlugs(new Set());
    setSelectedUsers(new Set());
  }, [open, mutate]);

  const toggleCharacter = useCallback((slug: string) => {
    setSelectedSlugs((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) {
        next.delete(slug);
      } else {
        next.add(slug);
      }
      return next;
    });
  }, []);

  const toggleUser = useCallback((id: string) => {
    setSelectedUsers((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleCreate = useCallback(async () => {
    const selectedCount = selectedSlugs.size + selectedUsers.size;
    if (selectedCount < 1) return;
    setCreating(true);
    try {
      onCreateGroup(
        Array.from(selectedSlugs),
        projectId,
        Array.from(selectedUsers),
      );
      onOpenChange(false);
    } finally {
      setCreating(false);
    }
  }, [selectedSlugs, selectedUsers, projectId, onCreateGroup, onOpenChange]);

  const selectedCount = selectedSlugs.size + selectedUsers.size;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Users className="size-4" />
            グループチャットを作成
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <div>
            <Label className="text-xs text-muted-foreground">
              自分以外に参加させるユーザー、AIキャラクターを選択してください
            </Label>
          </div>

          {loading ? (
            <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              参加候補を取得中...
            </div>
          ) : (
            <div className="max-h-80 space-y-4 overflow-auto pr-1">
              <section className="space-y-2">
                <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                  <Users className="size-3.5" />
                  ユーザー
                </div>
                {users.length === 0 ? (
                  <p className="text-xs text-muted-foreground">招待可能なユーザーがありません</p>
                ) : (
                  users.map((user) => (
                    <label
                      key={user.id}
                      className="flex cursor-pointer items-center gap-3 rounded-md border p-2.5 transition-colors hover:bg-muted/50"
                    >
                      <Checkbox
                        checked={selectedUsers.has(user.id)}
                        onCheckedChange={() => toggleUser(user.id)}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium">
                          {user.display_name || user.username}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {user.username}
                        </div>
                      </div>
                    </label>
                  ))
                )}
              </section>

              <section className="space-y-2">
                <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                  <Bot className="size-3.5" />
                  AIキャラクター
                </div>
                {characters.length === 0 ? (
                  <p className="text-xs text-muted-foreground">有効なキャラクターがありません</p>
                ) : (
                  characters.map((char) => (
                    <label
                      key={char.id}
                      className="flex cursor-pointer items-center gap-3 rounded-md border p-2.5 transition-colors hover:bg-muted/50"
                    >
                      <Checkbox
                        checked={selectedSlugs.has(char.slug)}
                        onCheckedChange={() => toggleCharacter(char.slug)}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-sm font-medium">{char.name}</span>
                          <Badge variant="outline" className="text-[10px]">
                            {char.character_type}
                          </Badge>
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {char.slug}
                        </div>
                      </div>
                    </label>
                  ))
                )}
              </section>

            </div>
          )}

          {selectedCount > 0 && (
            <div className="flex flex-wrap gap-1">
              {Array.from(selectedSlugs).map((slug) => {
                const char = characters.find((c) => c.slug === slug);
                return (
                  <Badge key={slug} variant="secondary" className="text-xs">
                    {char?.name || slug}
                  </Badge>
                );
              })}
              {Array.from(selectedUsers).map((id) => {
                const user = users.find((u) => u.id === id);
                return (
                  <Badge key={id} variant="secondary" className="text-xs">
                    {user?.display_name || user?.username || id}
                  </Badge>
                );
              })}
            </div>
          )}

          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onOpenChange(false)}
            >
              キャンセル
            </Button>
            <Button
              size="sm"
              onClick={handleCreate}
              disabled={selectedCount < 1 || creating}
            >
              {creating && <Loader2 className="size-3 animate-spin mr-1" />}
              作成（{selectedCount + 1}名）
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
