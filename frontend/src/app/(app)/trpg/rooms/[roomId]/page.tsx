"use client";

/* eslint-disable @next/next/no-img-element */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import {
  TRPGUIModulePanel,
  type TRPGUIModule,
} from "@/components/trpg/ui-module-panel";
import {
  Dices,
  Send,
  Sparkles,
  Users,
  ArrowLeft,
  SkipForward,
  PlayCircle,
  Music,
  VolumeX,
  Archive,
  Image as ImageIcon,
} from "lucide-react";
import {
  COC_KEY_SKILLS,
  collectUiModules,
  defaultDiceExpression,
  getUiModuleState,
  intValue,
  isCocScenario,
  isRecord,
  py,
  scenarioImageSrc,
  uiModuleList,
  type Disclosure,
  type Participant,
  type PlayLog,
  type PrivateMessage,
  type Room,
} from "@/lib/trpg-room-utils";
import { useConfirm } from "@/hooks/use-confirm";
import { LogLine } from "@/components/trpg/log-line";
import { ParticipantsPanel } from "@/components/trpg/participants-panel";
import { DisclosurePanel } from "@/components/trpg/disclosure-panel";
import { PrivateChatPanel } from "@/components/trpg/private-chat-panel";
import { GenericSheetCard } from "@/components/trpg/generic-sheet-card";
import { CocSheetCard } from "@/components/trpg/coc-sheet-card";
import { NpcStrategyCard } from "@/components/trpg/npc-strategy-card";
import { JoinRoomDialog } from "@/components/trpg/join-room-dialog";
import { AddNPCDialog } from "@/components/trpg/add-npc-dialog";
import { useTrpgBgm } from "@/components/trpg/hooks/use-trpg-bgm";
import { useTrpgRoomSocket } from "@/components/trpg/hooks/use-trpg-room-socket";
import { useCocActions } from "@/components/trpg/hooks/use-coc-actions";

// ─── Main Page ───

export default function TRPGRoomPage() {
  const confirm = useConfirm();
  const params = useParams<{ roomId: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const roomId = params.roomId;
  const inviteCode = searchParams.get("invite_code") || "";

  const [room, setRoom] = useState<Room | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [myParticipantId, setMyParticipantId] = useState<string>("");
  const [showJoin, setShowJoin] = useState(false);
  const [myAvatarDraft, setMyAvatarDraft] = useState("");
  const [avatarSaving, setAvatarSaving] = useState(false);

  const [showAddNPC, setShowAddNPC] = useState(false);

  const [actionText, setActionText] = useState("");
  const [actionKind, setActionKind] = useState<"action" | "speech" | "ooc">(
    "action"
  );
  const [submitting, setSubmitting] = useState(false);
  const [gmThinking, setGmThinking] = useState(false);
  const [imageGenerating, setImageGenerating] = useState(false);

  const [diceExp, setDiceExp] = useState("1d100");
  const [diceTarget, setDiceTarget] = useState("");
  const [diceDifficulty, setDiceDifficulty] = useState<
    "regular" | "hard" | "extreme"
  >("regular");
  const [diceNote, setDiceNote] = useState("");
  const [sessionBusy, setSessionBusy] = useState(false);
  const [disclosures, setDisclosures] = useState<Disclosure[]>([]);
  const [privateMessages, setPrivateMessages] = useState<PrivateMessage[]>([]);
  const [uiModuleDraft, setUiModuleDraft] = useState("");
  const [uiModuleBusy, setUiModuleBusy] = useState(false);

  const logScrollRef = useRef<HTMLDivElement>(null);

  // ── BGM（use-trpg-bgm フックへ抽出）──
  const {
    bgmAutoEnabled,
    currentBgm,
    bgmBusy,
    playBgmTrack,
    handleBgmLog,
    handleBgmAutoToggle,
    stopBgm,
  } = useTrpgBgm({ room, setRoom });

  const loadDisclosures = useCallback(async () => {
    if (!room?.id) return;
    const query = myParticipantId
      ? `?viewer_participant_id=${encodeURIComponent(myParticipantId)}`
      : "";
    try {
      const data = await py<{ disclosures: Disclosure[] }>(
        `/api/trpg/rooms/${room.id}/disclosures${query}`,
      );
      setDisclosures(data.disclosures ?? []);
    } catch (e) {
      console.warn("TRPG disclosures load failed", e);
    }
  }, [myParticipantId, room?.id]);

  const loadPrivateMessages = useCallback(async () => {
    if (!room?.id || !myParticipantId) {
      setPrivateMessages([]);
      return;
    }
    try {
      const data = await py<{ messages: PrivateMessage[] }>(
        `/api/trpg/rooms/${room.id}/private-messages?viewer_participant_id=${encodeURIComponent(myParticipantId)}`,
      );
      setPrivateMessages(data.messages ?? []);
    } catch (e) {
      console.warn("TRPG private messages load failed", e);
    }
  }, [myParticipantId, room?.id]);

  useEffect(() => {
    void loadDisclosures();
  }, [loadDisclosures]);

  useEffect(() => {
    void loadPrivateMessages();
  }, [loadPrivateMessages]);

  // ── ルーム読み込み ──
  const loadRoom = useCallback(async () => {
    try {
      const query = inviteCode
        ? `?invite_code=${encodeURIComponent(inviteCode)}`
        : "";
      const data = await py<Room>(`/api/trpg/rooms/${roomId}${query}`);
      setRoom(data);
      setError("");
      // localStorage から自分の participant_id を復元
      const key = `trpg-participant-${data.id}`;
      const saved = typeof window !== "undefined"
        ? window.localStorage.getItem(key)
        : null;
      if (
        saved &&
        data.participants.some(
          (p) => p.id === saved && p.is_active_participant,
        )
      ) {
        setMyParticipantId(saved);
        setShowJoin(false);
      } else {
        setMyParticipantId("");
        setShowJoin(true);
      }
    } catch (e) {
      console.error(e);
      setError("ルーム情報の取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }, [inviteCode, roomId]);

  useEffect(() => {
    loadRoom();
  }, [loadRoom]);

  useEffect(() => {
    if (!room) return;
    setDiceExp((current) =>
      current === "1d100" && !isCocScenario(room)
        ? defaultDiceExpression(room)
        : current,
    );
  }, [room]);

  // ── WebSocket 接続（use-trpg-room-socket フックへ抽出）──
  useTrpgRoomSocket({
    roomId: room?.id,
    inviteCode,
    bgmAutoEnabled,
    setRoom,
    setGmThinking,
    setImageGenerating,
    handleBgmLog,
    playBgmTrack,
    loadRoom,
    loadDisclosures,
    loadPrivateMessages,
  });

  // ── 自動スクロール ──
  useEffect(() => {
    if (logScrollRef.current) {
      logScrollRef.current.scrollTop = logScrollRef.current.scrollHeight;
    }
  }, [room?.logs.length]);

  // ── 入室完了後の参加者ID保存とルーム再取得 ──
  const handleJoined = useCallback(
    async (participantId: string) => {
      if (!room) return;
      setMyParticipantId(participantId);
      window.localStorage.setItem(`trpg-participant-${room.id}`, participantId);
      setShowJoin(false);
      await loadRoom();
    },
    [room, loadRoom],
  );

  // ── 退出 ──
  const handleLeave = useCallback(async () => {
    if (!room) return;
    if (!(await confirm({ description: "ルームを退出しますか？" }))) return;
    if (!myParticipantId) {
      window.localStorage.removeItem(`trpg-participant-${room.id}`);
      router.push("/trpg");
      return;
    }
    try {
      await py(`/api/trpg/rooms/${room.id}/leave/${myParticipantId}`, {
        method: "POST",
      });
      window.localStorage.removeItem(`trpg-participant-${room.id}`);
      router.push("/trpg");
    } catch (e) {
      console.error(e);
      alert("退出に失敗しました");
    }
  }, [room, myParticipantId, router, confirm]);

  // ── 行動宣言 ──
  const handleSubmitAction = useCallback(async () => {
    if (!room || !actionText.trim() || !myParticipantId) return;
    setSubmitting(true);
    setGmThinking(actionKind !== "ooc");
    try {
      await py(`/api/trpg/rooms/${room.id}/actions`, {
        method: "POST",
        body: JSON.stringify({
          participant_id: myParticipantId,
          action_text: actionText.trim(),
          action_kind: actionKind,
        }),
      });
      setActionText("");
    } catch (e) {
      console.error(e);
      alert("送信に失敗しました");
      setGmThinking(false);
    } finally {
      setSubmitting(false);
    }
  }, [room, actionText, actionKind, myParticipantId]);

  // ── GM 進行 ──
  const handleGMAdvance = useCallback(async () => {
    if (!room) return;
    setGmThinking(true);
    try {
      await py(`/api/trpg/rooms/${room.id}/gm/advance`, {
        method: "POST",
        body: JSON.stringify({ user_request: "" }),
      });
    } catch (e) {
      console.error(e);
      setGmThinking(false);
    }
  }, [room]);

  const handleGenerateCurrentImage = useCallback(async () => {
    if (!room || !myParticipantId) return;
    setImageGenerating(true);
    try {
      await py(`/api/trpg/rooms/${room.id}/images/current`, {
        method: "POST",
        body: JSON.stringify({
          participant_id: myParticipantId,
          user_prompt: "",
        }),
      });
    } catch (e) {
      console.error(e);
      setImageGenerating(false);
      alert("現在状況の画像生成に失敗しました");
    }
  }, [room, myParticipantId]);

  // ── ダイス ──
  const handleRoll = useCallback(
    async (
      expression?: string,
      options?: {
        target?: number | null;
        difficulty?: "regular" | "hard" | "extreme";
        note?: string;
      },
    ) => {
      if (!room || !myParticipantId) return;
      const expr = (expression || diceExp).trim();
      if (!expr) return;
      try {
        await py(`/api/trpg/rooms/${room.id}/dice`, {
          method: "POST",
          body: JSON.stringify({
            expression: expr,
            participant_id: myParticipantId,
            target:
              options?.target !== undefined
                ? options.target
                : diceTarget
                  ? Number(diceTarget)
                  : null,
            difficulty: options?.difficulty || diceDifficulty,
            note: options?.note ?? diceNote,
          }),
        });
      } catch (e) {
        console.error(e);
        alert("ダイスロールに失敗しました");
      }
    },
    [room, myParticipantId, diceExp, diceTarget, diceDifficulty, diceNote]
  );

  // ── オープニング開始 ──
  const handleStart = useCallback(async () => {
    if (!room) return;
    setGmThinking(true);
    try {
      await py(`/api/trpg/rooms/${room.id}/start`, { method: "POST" });
    } catch (e) {
      console.error(e);
      setGmThinking(false);
    }
  }, [room]);

  const handleCompleteRoom = useCallback(async () => {
    if (!room) return;
    if (!(await confirm({ description: "セッションを終了しますか？" }))) return;
    setSessionBusy(true);
    try {
      const completedRoom = await py<Room>(
        `/api/trpg/rooms/${room.id}/complete`,
        {
          method: "POST",
          body: JSON.stringify({ outcome: "completed", summary: "" }),
        },
      );
      setRoom(completedRoom);
    } catch (e) {
      console.error(e);
      alert("セッション終了に失敗しました");
    } finally {
      setSessionBusy(false);
    }
  }, [room, confirm]);

  // ── ターン進行 ──
  const handleAdvanceTurn = useCallback(async () => {
    if (!room) return;
    try {
      await py(`/api/trpg/rooms/${room.id}/turn/advance`, { method: "POST" });
    } catch (e) {
      console.error(e);
    }
  }, [room]);

  const activeParticipants = useMemo(
    () =>
      room
        ? [...room.participants]
            .filter((p) => p.is_active_participant)
            .sort((a, b) => a.seat_index - b.seat_index)
        : [],
    [room]
  );
  const myParticipant = useMemo(
    () => activeParticipants.find((p) => p.id === myParticipantId) || null,
    [activeParticipants, myParticipantId],
  );
  useEffect(() => {
    setMyAvatarDraft(myParticipant?.avatar_url || "");
  }, [myParticipant?.avatar_url]);
  const visibleDisclosureTargets = activeParticipants.filter(
    (participant) => participant.role !== "npc",
  );

  const handleUiModuleAction = useCallback(
    async (
      module: TRPGUIModule,
      actionType: string,
      payload: Record<string, unknown> = {},
    ) => {
      if (!room) return;
      try {
        const result = await py<{
          shared_state?: Record<string, unknown>;
          log?: PlayLog;
        }>(
          `/api/trpg/rooms/${room.id}/ui-modules/${encodeURIComponent(module.id)}/actions`,
          {
            method: "POST",
            body: JSON.stringify({
              participant_id: myParticipantId || undefined,
              action_type: actionType,
              payload,
            }),
          },
        );
        if (result.shared_state) {
          setRoom((prev) => prev ? { ...prev, shared_state: result.shared_state || {} } : prev);
        }
        if (result.log) {
          setRoom((prev) => {
            if (!prev || prev.logs.some((log) => log.id === result.log?.id)) return prev;
            return { ...prev, logs: [...prev.logs, result.log as PlayLog] };
          });
        }
      } catch (e) {
        console.error(e);
        alert("UIモジュール操作に失敗しました");
      }
    },
    [myParticipantId, room],
  );

  const handleAddUiModule = useCallback(async () => {
    if (!room || !uiModuleDraft.trim()) return;
    setUiModuleBusy(true);
    try {
      const parsed = JSON.parse(uiModuleDraft) as unknown;
      const modules = Array.isArray(parsed) ? parsed : [parsed];
      const validModules = modules.filter(
        (module): module is TRPGUIModule =>
          isRecord(module) && typeof module.id === "string" && module.id.trim().length > 0,
      );
      if (validModules.length === 0) {
        alert("id を持つ UI モジュール JSON を入力してください");
        return;
      }
      const currentModules = uiModuleList(room.shared_state?.ui_modules);
      const merged = new Map(currentModules.map((uiModule) => [uiModule.id, uiModule]));
      for (const uiModule of validModules) {
        merged.set(uiModule.id, uiModule);
      }
      const sharedState = await py<Record<string, unknown>>(
        `/api/trpg/rooms/${room.id}/shared_state`,
        {
          method: "PUT",
          body: JSON.stringify({
            updates: {
              ui_modules: Array.from(merged.values()),
            },
          }),
        },
      );
      setRoom((prev) => prev ? { ...prev, shared_state: sharedState } : prev);
      setUiModuleDraft("");
    } catch (e) {
      console.error(e);
      alert("UIモジュールJSONの保存に失敗しました");
    } finally {
      setUiModuleBusy(false);
    }
  }, [room, uiModuleDraft]);

  const myCocState = isRecord(myParticipant?.pc_state)
    && myParticipant?.pc_state.sheet_format === "coc_investigator_v1"
    ? myParticipant.pc_state
    : null;
  const myGenericState = isRecord(myParticipant?.pc_state)
    && myParticipant?.pc_state.sheet_format === "generic_pc_v1"
    ? myParticipant.pc_state
    : null;
  const genericSkillMap = useMemo(
    () =>
      isRecord(myGenericState?.skills)
        ? (myGenericState.skills as Record<string, unknown>)
        : {},
    [myGenericState],
  );
  const genericSkillNames = useMemo(
    () =>
      Object.keys(genericSkillMap).filter((skill) => skill.trim().length > 0),
    [genericSkillMap],
  );
  const uiModules = useMemo(() => collectUiModules(room), [room]);
  const uiModuleState = useMemo(
    () => getUiModuleState(room?.shared_state || {}),
    [room?.shared_state],
  );
  const canUseGmModules = myParticipant?.role === "gm";
  const moduleImageSrc = useCallback((path?: string) => {
    if (!path) return "";
    if (/^https?:\/\//i.test(path) || path.startsWith("/api/")) {
      return path;
    }
    return scenarioImageSrc(path);
  }, []);

  // ── CoC 操作群（use-coc-actions フックへ抽出）──
  const cocActions = useCocActions({
    room,
    setRoom,
    myParticipantId,
    myCocState,
    diceDifficulty,
    diceNote,
  });
  const { cocSkillMap, handleCocSkillCheck } = cocActions;

  const handleUpdateMyAvatar = useCallback(async () => {
    if (!room || !myParticipantId) return;
    setAvatarSaving(true);
    try {
      const participant = await py<Participant>(
        `/api/trpg/participants/${myParticipantId}`,
        {
          method: "PUT",
          body: JSON.stringify({ avatar_url: myAvatarDraft.trim() }),
        },
      );
      setRoom((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          participants: prev.participants.map((p) =>
            p.id === participant.id ? participant : p,
          ),
        };
      });
    } catch (e) {
      console.error(e);
      alert("アイコンの更新に失敗しました");
    } finally {
      setAvatarSaving(false);
    }
  }, [room, myParticipantId, myAvatarDraft]);

  if (loading) {
    return (
      <div className="p-6 text-sm text-muted-foreground">読み込み中…</div>
    );
  }
  if (error || !room) {
    return (
      <div className="p-6">
        <div className="text-sm text-destructive">{error || "ルームが見つかりません"}</div>
        <Button variant="outline" className="mt-4" onClick={() => router.push("/trpg")}>
          ルーム一覧へ戻る
        </Button>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* ヘッダー */}
      <div className="flex shrink-0 items-center justify-between border-b px-4 py-2">
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => router.push("/trpg")}
          >
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <div className="flex items-center gap-2">
              <span className="font-semibold">{room.room_title}</span>
              <Badge variant="outline">{room.room_code}</Badge>
              <Badge variant="secondary">
                {room.gm_mode === "ai" ? "AI GM" : "人間 GM"}
              </Badge>
              {room.status === "completed" && (
                <Badge variant="outline">終了</Badge>
              )}
            </div>
            <div className="text-xs text-muted-foreground">
              {room.scenario?.title}
              {room.current_scene && ` / ${room.current_scene.title}`}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {room.logs.length === 0 && (
            <Button size="sm" onClick={handleStart}>
              <PlayCircle className="mr-1 h-4 w-4" /> セッション開始
            </Button>
          )}
          {!myParticipantId && (
            <Button size="sm" onClick={() => setShowJoin(true)}>
              <Users className="mr-1 h-4 w-4" /> キャラ作成 / 入室
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={handleAdvanceTurn}>
            <SkipForward className="mr-1 h-4 w-4" /> ターン進行
          </Button>
          <Button size="sm" variant="outline" onClick={handleGMAdvance}>
            <Sparkles className="mr-1 h-4 w-4" /> GM描写
          </Button>
          {room.status !== "completed" && (
            <Button
              size="sm"
              variant="outline"
              onClick={handleCompleteRoom}
              disabled={sessionBusy}
            >
              <Archive className="mr-1 h-4 w-4" /> 終了
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            onClick={handleGenerateCurrentImage}
            disabled={!myParticipantId || imageGenerating}
          >
            <ImageIcon className="mr-1 h-4 w-4" />
            {imageGenerating ? "画像生成中" : "現在画像"}
          </Button>
          <Button size="sm" variant="ghost" className="text-destructive" onClick={handleLeave}>
            退出
          </Button>
        </div>
      </div>

      {/* 本体 3カラム */}
      <div className="grid min-h-0 flex-1 grid-cols-[240px_minmax(0,1fr)_320px] overflow-hidden">
        {/* 左: 参加者パネル */}
        <ParticipantsPanel
          participants={activeParticipants}
          myParticipantId={myParticipantId}
          currentTurnParticipantId={room.current_turn_participant_id}
          myAvatarDraft={myAvatarDraft}
          avatarSaving={avatarSaving}
          onMyAvatarDraftChange={setMyAvatarDraft}
          onSaveMyAvatar={handleUpdateMyAvatar}
          onOpenAddNpc={() => setShowAddNPC(true)}
        />

        {/* 中央: ログ + 入力 */}
        <section className="flex min-h-0 min-w-0 flex-col overflow-hidden">
          <div
            ref={logScrollRef}
            data-testid="trpg-log-scroll"
            className="min-h-0 flex-1 space-y-2 overflow-y-auto overflow-x-hidden p-4"
          >
            {room.logs.map((log) => (
              <LogLine
                key={log.id}
                log={log}
                participants={room.participants}
                myParticipantId={myParticipantId}
              />
            ))}
            {gmThinking && (
              <div className="italic text-muted-foreground">
                GM が描写を考えています…
              </div>
            )}
            {room.logs.length === 0 && (
              <div className="text-center text-sm text-muted-foreground">
                ログはまだありません。[セッション開始] を押してオープニングを生成しましょう。
              </div>
            )}
          </div>
          {/* 入力エリア */}
          <div className="shrink-0 space-y-2 border-t p-3">
            <div className="flex items-center gap-2 text-xs">
              <Button
                size="sm"
                variant={actionKind === "action" ? "default" : "outline"}
                onClick={() => setActionKind("action")}
              >
                Do
              </Button>
              <Button
                size="sm"
                variant={actionKind === "speech" ? "default" : "outline"}
                onClick={() => setActionKind("speech")}
              >
                Say
              </Button>
              <Button
                size="sm"
                variant={actionKind === "ooc" ? "default" : "outline"}
                onClick={() => setActionKind("ooc")}
              >
                OOC
              </Button>
            </div>
            <div className="flex items-end gap-2">
              <Textarea
                value={actionText}
                onChange={(e) => setActionText(e.target.value)}
                placeholder={
                  actionKind === "action"
                    ? "行動を宣言… (例: 扉を慎重に開ける)"
                    : actionKind === "speech"
                    ? "セリフを入力… (例: 何者だ、名乗れ!)"
                    : "雑談/メタ発言…"
                }
                rows={2}
                className="resize-none"
              />
              <Button
                onClick={handleSubmitAction}
                disabled={submitting || !actionText.trim() || !myParticipantId}
              >
                <Send className="h-4 w-4" />
              </Button>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-muted-foreground">よく使うダイス:</span>
              {["1d100", "1d20", "2d6", "1d6"].map((d) => (
                <Button
                  key={d}
                  size="sm"
                  variant="outline"
                  onClick={() => handleRoll(d)}
                  disabled={!myParticipantId}
                >
                  <Dices className="mr-1 h-3 w-3" />
                  {d}
                </Button>
              ))}
              <Input
                value={diceExp}
                onChange={(e) => setDiceExp(e.target.value)}
                className="h-8 w-24"
                placeholder="NdM+K"
              />
              <Input
                value={diceTarget}
                onChange={(e) => setDiceTarget(e.target.value)}
                className="h-8 w-20"
                placeholder="目標"
              />
              <select
                value={diceDifficulty}
                onChange={(e) =>
                  setDiceDifficulty(
                    e.target.value as "regular" | "hard" | "extreme"
                  )
                }
                className="h-8 rounded border bg-background px-2 text-xs"
                aria-label="判定難度"
              >
                <option value="regular">通常</option>
                <option value="hard">困難</option>
                <option value="extreme">極限</option>
              </select>
              <Input
                value={diceNote}
                onChange={(e) => setDiceNote(e.target.value)}
                className="h-8 w-32"
                placeholder="判定名"
              />
              <Button
                size="sm"
                variant="outline"
                onClick={() => handleRoll()}
                disabled={!myParticipantId}
              >
                ロール
              </Button>
            </div>
            {!myParticipantId && (
              <div className="text-xs text-muted-foreground">
                ダイスロールはキャラクター作成後に使えます。
              </div>
            )}
            {myCocState && (
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="text-muted-foreground">CoC技能:</span>
                {COC_KEY_SKILLS.map((skill) => {
                  const value = intValue(cocSkillMap[skill], -1);
                  if (value < 0) return null;
                  return (
                    <Button
                      key={skill}
                      size="sm"
                      variant="outline"
                      onClick={() => handleCocSkillCheck(skill)}
                    >
                      {skill} {value}
                    </Button>
                  );
                })}
              </div>
            )}
            {myGenericState && genericSkillNames.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="text-muted-foreground">技能ロール:</span>
                {genericSkillNames.map((skill) => {
                  const target = intValue(genericSkillMap[skill], 0);
                  return (
                    <Button
                      key={skill}
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        handleRoll(undefined, {
                          target: target > 0 ? target : null,
                          note: skill,
                        })
                      }
                    >
                      {skill}
                      {target > 0 ? ` ${target}` : ""}
                    </Button>
                  );
                })}
              </div>
            )}
          </div>
        </section>

        {/* 右: シーン + 情報 */}
        <aside className="min-h-0 space-y-3 overflow-auto border-l p-3">
          <DisclosurePanel
            room={room}
            myParticipantId={myParticipantId}
            disclosures={disclosures}
            setDisclosures={setDisclosures}
            loadDisclosures={loadDisclosures}
            visibleTargets={visibleDisclosureTargets}
          />
          <PrivateChatPanel
            room={room}
            myParticipantId={myParticipantId}
            privateMessages={privateMessages}
            activeParticipants={activeParticipants}
            visibleTargets={visibleDisclosureTargets}
            loadPrivateMessages={loadPrivateMessages}
          />
          {myGenericState && (
            <GenericSheetCard
              myGenericState={myGenericState}
              genericSkillNames={genericSkillNames}
              genericSkillMap={genericSkillMap}
              onRoll={handleRoll}
            />
          )}
          {myCocState && (
            <CocSheetCard
              coc={cocActions}
              myCocState={myCocState}
              activeParticipants={activeParticipants}
              myParticipantId={myParticipantId}
            />
          )}
          {room.current_scene && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">
                  現在のシーン: {room.current_scene.title}
                </CardTitle>
              </CardHeader>
              <CardContent className="text-xs text-muted-foreground whitespace-pre-wrap">
                {room.current_scene.description}
              </CardContent>
            </Card>
          )}
          <TRPGUIModulePanel
            modules={uiModules}
            moduleState={uiModuleState}
            disclosures={disclosures}
            isGm={canUseGmModules}
            myParticipantId={myParticipantId}
            imageSrc={moduleImageSrc}
            onAction={handleUiModuleAction}
          />
          {canUseGmModules && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">UIモジュール追加</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-xs">
                <Textarea
                  value={uiModuleDraft}
                  onChange={(e) => setUiModuleDraft(e.target.value)}
                  rows={5}
                  className="resize-none font-mono text-[11px]"
                  placeholder='{"id":"vault_keypad","module":"keypad","title":"地下金庫","config":{"successCode":"0427"},"onSuccess":[{"setState":{"vault.open":true}},{"appendLog":"地下金庫のロックが解除された。"}]}'
                />
                <Button
                  type="button"
                  size="sm"
                  className="w-full"
                  onClick={() => void handleAddUiModule()}
                  disabled={uiModuleBusy || !uiModuleDraft.trim()}
                >
                  保存
                </Button>
              </CardContent>
            </Card>
          )}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Music className="h-4 w-4" />
                BGM
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-xs">
              <label className="flex items-center justify-between gap-3">
                <span className="text-muted-foreground">AI自動切替</span>
                <Checkbox
                  checked={bgmAutoEnabled}
                  onCheckedChange={(checked) =>
                    void handleBgmAutoToggle(Boolean(checked))
                  }
                  aria-label="AI BGM自動切替"
                />
              </label>
              <div className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate">
                  {currentBgm?.track || "未選択"}
                </span>
                <Button
                  size="icon"
                  variant="ghost"
                  className="h-7 w-7"
                  onClick={stopBgm}
                  disabled={bgmBusy}
                  aria-label="BGM停止"
                >
                  <VolumeX className="h-4 w-4" />
                </Button>
              </div>
            </CardContent>
          </Card>
          <NpcStrategyCard room={room} setRoom={setRoom} />
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">共有状態</CardTitle>
            </CardHeader>
            <CardContent className="text-xs space-y-1">
              {Object.entries(room.shared_state || {}).map(([k, v]) => (
                <div key={k} className="flex items-center justify-between">
                  <span className="text-muted-foreground">{k}</span>
                  <span>{typeof v === "string" ? v : JSON.stringify(v)}</span>
                </div>
              ))}
            </CardContent>
          </Card>
          {room.scenario && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">シナリオ</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-xs text-muted-foreground">
                {scenarioImageSrc(room.scenario.cover_image_path) && (
                  <div className="aspect-[16/9] overflow-hidden rounded border bg-muted">
                    <img
                      src={scenarioImageSrc(room.scenario.cover_image_path)}
                      alt={room.scenario.title}
                      className="h-full w-full object-cover"
                      loading="lazy"
                    />
                  </div>
                )}
                <div className="whitespace-pre-wrap">{room.scenario.description}</div>
              </CardContent>
            </Card>
          )}
        </aside>
      </div>

      {/* 入室ダイアログ */}
      <JoinRoomDialog
        open={showJoin}
        onOpenChange={setShowJoin}
        room={room}
        inviteCode={inviteCode}
        onJoined={handleJoined}
      />

      {/* NPC追加ダイアログ */}
      <AddNPCDialog
        open={showAddNPC}
        onOpenChange={setShowAddNPC}
        room={room}
        onAdded={loadRoom}
      />
    </div>
  );
}
