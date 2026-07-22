"use client";

/* eslint-disable @next/next/no-img-element */

import { Suspense, useCallback, useEffect, useState } from "react";
import useSWR from "swr";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { useConfirm } from "@/hooks/use-confirm";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  BookOpen,
  CheckCircle2,
  Dices,
  Plus,
  Users,
  DoorOpen,
  Sparkles,
  Trash2,
  Loader2,
  MoreHorizontal,
} from "lucide-react";

// ─── Types ───

type Scenario = {
  id: string;
  title: string;
  description: string;
  scenario_kind?: "writing" | "trpg";
  ruleset?: string;
  genre: string;
  perspective: string;
  cover_image_path?: string;
};

type Room = {
  id: string;
  room_code: string;
  room_title: string;
  status: string;
  max_players: number;
  player_count: number;
  is_public: boolean;
  gm_mode: string;
  host_user_id: string | null;
  scenario: Scenario | null;
  updated_at: string | null;
};

async function py<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    throw new Error(`API Error: ${res.status}`);
  }
  return res.json();
}

function scenarioImageSrc(path?: string, size = 640): string {
  if (!path) return "/images/ui/scenario-trpg.png";
  if (/^https?:\/\//i.test(path) || path.startsWith("/api/")) {
    return path;
  }
  return `/api/python-proxy/filer/image-thumbnail?path=${encodeURIComponent(path)}&size=${size}`;
}

const EMPTY_ROOMS: Room[] = [];
const EMPTY_SCENARIOS: Scenario[] = [];

function RoomStatusBadge({ status }: { status: string }) {
  if (status === "completed") {
    return (
      <Badge
        variant="outline"
        className="border-emerald-200 bg-emerald-50 text-emerald-700"
      >
        <CheckCircle2 className="mr-1 h-3 w-3" />
        完了済み
      </Badge>
    );
  }

  if (status === "paused") {
    return <Badge variant="secondary">一時停止</Badge>;
  }

  return <Badge variant="secondary">進行中</Badge>;
}

function TRPGRoomListContent() {
  const confirm = useConfirm();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [creating, setCreating] = useState(false);
  const [deletingRoomId, setDeletingRoomId] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [joinCode, setJoinCode] = useState("");

  // 作成フォーム
  const [scenarioId, setScenarioId] = useState("");
  const [roomTitle, setRoomTitle] = useState("");
  const [maxPlayers, setMaxPlayers] = useState(4);
  const [isPublic, setIsPublic] = useState(true);
  const [gmMode, setGmMode] = useState<"ai" | "human">("ai");

  // 一覧取得は SWR に委譲（マウント時取得のみ・再取得や自動 revalidation はしない）。
  const {
    data: rooms = EMPTY_ROOMS,
    isLoading: loading,
    mutate: mutateRooms,
  } = useSWR<Room[]>(
    "trpg/rooms?status=all",
    async () =>
      (await py<{ rooms: Room[] }>("/api/trpg/rooms?status=all")).rooms || [],
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      shouldRetryOnError: false,
    },
  );
  const { data: scenarios = EMPTY_SCENARIOS } = useSWR<Scenario[]>(
    "trpg/scenarios",
    async () =>
      ((await py<{ scenarios: Scenario[] }>("/scenarios")).scenarios || []).filter(
        (s) => s.scenario_kind === "trpg",
      ),
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      shouldRetryOnError: false,
    },
  );

  useEffect(() => {
    const requestedScenarioId = searchParams.get("scenario_id");
    if (requestedScenarioId) {
      setScenarioId(requestedScenarioId);
    }
    if (searchParams.get("create") === "1") {
      setShowCreate(true);
    }
  }, [searchParams]);

  const handleCreate = useCallback(async () => {
    if (!scenarioId) return;
    setCreating(true);
    try {
      const room = await py<Room>("/api/trpg/rooms", {
        method: "POST",
        body: JSON.stringify({
          scenario_id: scenarioId,
          room_title: roomTitle,
          max_players: maxPlayers,
          gm_mode: gmMode,
          is_public: isPublic,
        }),
      });
      setShowCreate(false);
      router.push(`/trpg/rooms/${room.id}`);
    } catch (e) {
      console.error(e);
      alert("ルーム作成に失敗しました");
    } finally {
      setCreating(false);
    }
  }, [scenarioId, roomTitle, maxPlayers, gmMode, isPublic, router]);

  const handleJoinByCode = useCallback(async () => {
    const code = joinCode.trim().toUpperCase();
    if (!code) return;
    try {
      const room = await py<Room>(
        `/api/trpg/rooms/${code}?invite_code=${encodeURIComponent(code)}`,
      );
      router.push(`/trpg/rooms/${room.id}?invite_code=${encodeURIComponent(code)}`);
    } catch (e) {
      console.error(e);
      alert("コードに該当するルームが見つかりませんでした");
    }
  }, [joinCode, router]);

  const handleDeleteRoom = useCallback(async (room: Room) => {
    if (
      !(await confirm({
        description: `TRPGセッション「${room.room_title}」を削除しますか？`,
        destructive: true,
      }))
    ) {
      return;
    }

    setDeletingRoomId(room.id);
    try {
      await py<{ ok: boolean }>(`/api/trpg/rooms/${room.id}`, {
        method: "DELETE",
      });
      void mutateRooms(
        (current) =>
          (current ?? EMPTY_ROOMS).filter((item) => item.id !== room.id),
        { revalidate: false },
      );
    } catch (e) {
      console.error(e);
      alert("TRPGセッションの削除に失敗しました。ホストまたは管理者のみ削除できます。");
    } finally {
      setDeletingRoomId(null);
    }
  }, [confirm, mutateRooms]);

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <Dices className="h-6 w-6" /> TRPG セッション
          </h1>
          <p className="text-sm text-muted-foreground">
            AI GM と一緒にTRPGを遊ぶためのソロ / マルチ対応ルーム
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Input
            placeholder="入室コード (例: A3K7XQ)"
            value={joinCode}
            onChange={(e) => setJoinCode(e.target.value)}
            className="w-40"
          />
          <Button variant="outline" onClick={handleJoinByCode}>
            <DoorOpen className="mr-1 h-4 w-4" /> コードで入室
          </Button>
          <Button variant="outline" onClick={() => router.push("/trpg/reference")}>
            <BookOpen className="mr-1 h-4 w-4" /> ルール資料
          </Button>
          <Button onClick={() => setShowCreate(true)}>
            <Plus className="mr-1 h-4 w-4" /> ルーム作成
          </Button>
        </div>
      </div>

      {loading ? (
        <div className="rounded-2xl border border-border bg-card px-5 py-4 text-sm text-muted-foreground">
          読み込み中…
        </div>
      ) : rooms.length === 0 ? (
        <Card className="overflow-hidden">
          <CardContent className="flex flex-col items-center justify-center gap-3 p-0 pb-10 text-center">
            <img
              src="/images/ui/trpg-room.png"
              alt=""
              className="mb-4 aspect-[16/7] w-full object-cover"
            />
            <Sparkles className="h-10 w-10 text-primary" />
            <p className="text-muted-foreground">
              公開中のルームはありません。ルームを作成してAI GMとTRPGを始めましょう。
            </p>
            <Button onClick={() => setShowCreate(true)}>
              <Plus className="mr-1 h-4 w-4" /> ルーム作成
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {rooms.map((room) => {
            const coverSrc = scenarioImageSrc(room.scenario?.cover_image_path);
            return (
              <Card
                key={room.id}
                className="relative cursor-pointer overflow-hidden transition hover:border-primary/70 hover:bg-accent"
                onClick={() => router.push(`/trpg/rooms/${room.id}`)}
              >
                <div className="aspect-[16/9] w-full overflow-hidden bg-muted">
                  <img
                    src={coverSrc}
                    alt={room.scenario?.title ?? room.room_title}
                    className="h-full w-full object-cover"
                    loading="lazy"
                  />
                </div>
                <CardHeader>
                  <CardTitle className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-2">
                    <span className="min-w-0 truncate">{room.room_title}</span>
                    <span className="flex items-center gap-1">
                      <Badge variant="outline">{room.room_code}</Badge>
                      <DropdownMenu>
                        <DropdownMenuTrigger
                          className="inline-flex size-7 items-center justify-center rounded-md border bg-background/80 text-foreground shadow-xs transition-colors hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-50"
                          disabled={deletingRoomId === room.id}
                          title="操作"
                          aria-label={`${room.room_title}の操作`}
                          onClick={(e) => e.stopPropagation()}
                        >
                          {deletingRoomId === room.id ? (
                            <Loader2 className="size-3.5 animate-spin" />
                          ) : (
                            <MoreHorizontal className="size-4" />
                          )}
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem
                            mnemonic="D"
                            variant="destructive"
                            onClick={(e) => {
                              e.stopPropagation();
                              void handleDeleteRoom(room);
                            }}
                          >
                            <Trash2 className="size-4" />
                            削除
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </span>
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 text-sm">
                  <div className="text-muted-foreground">
                    {room.scenario?.title ?? "シナリオ未設定"}
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="flex items-center gap-1">
                      <Users className="h-3 w-3" />
                      {room.player_count}/{room.max_players}
                    </span>
                    <RoomStatusBadge status={room.status} />
                    <Badge variant="secondary">
                      {room.gm_mode === "ai" ? "AI GM" : "人間 GM"}
                    </Badge>
                    {room.is_public && (
                      <Badge variant="outline">公開</Badge>
                    )}
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新しいルームを作成</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label>シナリオ</Label>
              <select
                className="mt-1 h-9 w-full rounded border bg-transparent px-2 text-sm"
                value={scenarioId}
                onChange={(e) => setScenarioId(e.target.value)}
              >
                <option value="">-- 選択してください --</option>
                {scenarios.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.title} / {s.ruleset || "generic"}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <Label>ルーム名（任意）</Label>
              <Input
                value={roomTitle}
                onChange={(e) => setRoomTitle(e.target.value)}
                placeholder="空欄ならシナリオ名が使われます"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>最大プレイヤー数</Label>
                <Input
                  type="number"
                  min={1}
                  max={12}
                  value={maxPlayers}
                  onChange={(e) => setMaxPlayers(Number(e.target.value))}
                />
              </div>
              <div>
                <Label>GM モード</Label>
                <select
                  className="h-9 w-full rounded border bg-transparent px-2 text-sm"
                  value={gmMode}
                  onChange={(e) => setGmMode(e.target.value as "ai" | "human")}
                >
                  <option value="ai">AI GM</option>
                  <option value="human">人間 GM</option>
                </select>
              </div>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={isPublic}
                onChange={(e) => setIsPublic(e.target.checked)}
              />
              ルームを一覧に公開する
            </label>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setShowCreate(false)}>
                キャンセル
              </Button>
              <Button
                onClick={handleCreate}
                disabled={!scenarioId || creating}
              >
                {creating ? "作成中…" : "作成"}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function TRPGRoomListPage() {
  return (
    <Suspense fallback={null}>
      <TRPGRoomListContent />
    </Suspense>
  );
}
