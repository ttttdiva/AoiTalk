"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { TrpgWorkspaceShell } from "@/components/trpg/trpg-workspace";
import {
  playGeneratedMediaUrl,
  trpgPlayApi,
  type TrpgPlayEvent,
  type TrpgPlayGmPrivateState,
  type TrpgPlayParticipant,
  type TrpgPlayPrivateState,
  type TrpgPlaySession,
  type TrpgPlayWhisper,
} from "@/lib/trpg/play-api";
import {
  applyGmPrivateStateView,
  gmPrivateStateDisplayName,
  removeGmPrivateStateForParticipant,
  resolvePlayViewerParticipant,
} from "@/lib/trpg/gm-private-state";
import { TrpgPlayWebSocket } from "@/lib/trpg/play-websocket";

const ACTION_KINDS = ["speech", "action", "ooc"] as const;

function eventImageMeta(event: TrpgPlayEvent) {
  const meta = event.meta ?? {};
  const mediaId = typeof meta.generated_media_id === "string" ? meta.generated_media_id : null;
  const imageStatus = typeof meta.image_status === "string" ? meta.image_status : null;
  return { mediaId, imageStatus };
}

export function TrpgPlaySessionPage({ sessionId }: { sessionId: string }) {
  const router = useRouter();
  const currentUserId = useCurrentUserId();
  const [session, setSession] = useState<TrpgPlaySession | null>(null);
  const [events, setEvents] = useState<TrpgPlayEvent[]>([]);
  const [whispers, setWhispers] = useState<TrpgPlayWhisper[]>([]);
  const [privateState, setPrivateState] = useState<TrpgPlayPrivateState["state"]>({ entries: {} });
  const [gmPrivateStates, setGmPrivateStates] = useState<TrpgPlayGmPrivateState[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [actionKind, setActionKind] = useState<(typeof ACTION_KINDS)[number]>("speech");
  const [actionText, setActionText] = useState("");
  const [diceExpr, setDiceExpr] = useState("1d100");
  const [diceNote, setDiceNote] = useState("");
  const [whisperText, setWhisperText] = useState("");
  const [whisperTargetId, setWhisperTargetId] = useState("");
  const [snapshotJson, setSnapshotJson] = useState("{}");
  const [imageGenerating, setImageGenerating] = useState(false);
  const wsRef = useRef<TrpgPlayWebSocket | null>(null);

  const participants = session?.participants ?? [];
  const myParticipant = useMemo(
    () => resolvePlayViewerParticipant(session, currentUserId),
    [session, currentUserId],
  );

  const canManageSession = useMemo(() => {
    if (!session || !myParticipant) return false;
    if (myParticipant.role === "spectator") return false;
    return (
      myParticipant.role === "gm" ||
      (currentUserId != null && session.host_user_id === currentUserId)
    );
  }, [session, myParticipant, currentUserId]);

  const isSpectator = myParticipant?.role === "spectator";
  const isGm = myParticipant?.role === "gm";
  const canPlay = Boolean(session?.status === "active" && myParticipant && !isSpectator);

  const imageEnabled = Boolean(session?.image_settings?.enabled);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const detail = await trpgPlayApi.getSession(sessionId);
      setSession(detail);
      setEvents(detail.recent_events ?? []);
      setSnapshotJson(JSON.stringify(detail.snapshot ?? {}, null, 2));
      const whisperList = await trpgPlayApi.listWhispers(sessionId);
      setWhispers(whisperList);

      const viewer = resolvePlayViewerParticipant(detail, currentUserId);
      const viewerIsSpectator = viewer?.role === "spectator";
      const viewerIsGm = viewer?.role === "gm";

      if (viewerIsSpectator) {
        setPrivateState({ entries: {} });
      } else {
        try {
          const ownPrivate = await trpgPlayApi.getPrivateState(sessionId);
          setPrivateState(ownPrivate.state ?? { entries: {} });
        } catch {
          setPrivateState({ entries: {} });
        }
      }

      if (viewerIsGm) {
        try {
          const gmStates = await trpgPlayApi.listGmPrivateStates(sessionId);
          setGmPrivateStates(gmStates);
        } catch {
          setGmPrivateStates([]);
        }
      } else {
        setGmPrivateStates([]);
      }
    } catch {
      setSession(null);
      setGmPrivateStates([]);
    } finally {
      setLoading(false);
    }
  }, [sessionId, currentUserId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const ws = new TrpgPlayWebSocket();
    wsRef.current = ws;
    ws.setOnConnectionChange(setConnected);
    ws.setOnMessage((payload) => {
      const type = String(payload.type || "");
      if (type === "sync" && payload.session) {
        const syncSession = payload.session as TrpgPlaySession;
        setSession(syncSession);
        setEvents(syncSession.recent_events ?? []);
        setSnapshotJson(JSON.stringify(syncSession.snapshot ?? {}, null, 2));
      }
      if (type === "event" && payload.event) {
        setEvents((prev) => [...prev, payload.event as TrpgPlayEvent]);
      }
      if (type === "whisper" && payload.whisper) {
        setWhispers((prev) => [...prev, payload.whisper as TrpgPlayWhisper]);
      }
      if (type === "snapshot" && payload.session) {
        const next = payload.session as TrpgPlaySession;
        setSession(next);
        setSnapshotJson(JSON.stringify(next.snapshot ?? {}, null, 2));
      }
      if (type === "ended" && payload.session) {
        setSession(payload.session as TrpgPlaySession);
      }
      if (type === "join" && payload.participant) {
        const participant = payload.participant as TrpgPlayParticipant;
        setSession((prev) =>
          prev
            ? {
                ...prev,
                participants: [...(prev.participants ?? []), participant],
              }
            : prev,
        );
      }
      if (type === "leave") {
        const nextParticipants = Array.isArray(payload.participants)
          ? (payload.participants as TrpgPlayParticipant[])
          : null;
        if (nextParticipants) {
          setSession((prev) => (prev ? { ...prev, participants: nextParticipants } : prev));
        }
        const leftId = typeof payload.participant_id === "string" ? payload.participant_id : "";
        if (leftId && myParticipant?.role === "gm") {
          setGmPrivateStates((prev) => removeGmPrivateStateForParticipant(prev, leftId));
        }
      }
      if (type === "private_state" && payload.private_state) {
        const row = payload.private_state as TrpgPlayPrivateState & {
          display_name?: string | null;
        };
        if (row.participant_id === myParticipant?.id && !payload.gm_view) {
          setPrivateState(row.state ?? { entries: {} });
        }
        if (payload.gm_view && myParticipant?.role === "gm") {
          setGmPrivateStates((prev) =>
            applyGmPrivateStateView(prev, {
              participant_id: row.participant_id,
              display_name: row.display_name ?? null,
              state: row.state ?? { entries: {} },
              updated_at: row.updated_at ?? null,
            }),
          );
        }
      }
    });
    ws.connect(sessionId);
    return () => ws.disconnect();
  }, [sessionId, myParticipant?.id, myParticipant?.role]);

  const runAction = async (action: () => Promise<void>) => {
    setActionError(null);
    try {
      await action();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "操作に失敗しました");
    }
  };

  const handleJoin = async () => {
    if (!inviteCode.trim() || !displayName.trim()) return;
    await trpgPlayApi.joinSession(sessionId, {
      invite_code: inviteCode.trim().toUpperCase(),
      display_name: displayName.trim(),
    });
    await load();
  };

  const handleStart = async () => {
    await runAction(async () => {
      const next = await trpgPlayApi.startSession(sessionId);
      setSession(next);
    });
  };

  const handleEnd = async () => {
    await runAction(async () => {
      const next = await trpgPlayApi.endSession(sessionId);
      setSession(next);
    });
  };

  const handleLeave = async () => {
    await runAction(async () => {
      await trpgPlayApi.leaveSession(sessionId);
      router.push("/trpg/play");
    });
  };

  const handleAction = async () => {
    if (!actionText.trim()) return;
    await runAction(async () => {
      const created = await trpgPlayApi.postAction(sessionId, {
        kind: actionKind,
        text: actionText.trim(),
      });
      setEvents((prev) => [...prev, ...created]);
      setActionText("");
    });
  };

  const handleDice = async () => {
    await runAction(async () => {
      const event = await trpgPlayApi.rollDice(sessionId, {
        expression: diceExpr,
        note: diceNote || undefined,
      });
      setEvents((prev) => [...prev, event]);
    });
  };

  const handleWhisper = async () => {
    if (!whisperText.trim() || !whisperTargetId) return;
    await runAction(async () => {
      const whisper = await trpgPlayApi.postWhisper(sessionId, {
        body: whisperText.trim(),
        recipient_participant_ids: [whisperTargetId],
      });
      setWhispers((prev) => [...prev, whisper]);
      setWhisperText("");
    });
  };

  const handleSnapshotSave = async () => {
    await runAction(async () => {
      const snapshot = JSON.parse(snapshotJson) as Record<string, unknown>;
      const next = await trpgPlayApi.patchSnapshot(sessionId, snapshot);
      setSession(next);
    });
  };

  const handleImageToggle = async (enabled: boolean) => {
    await runAction(async () => {
      const next = await trpgPlayApi.patchImageSettings(sessionId, {
        ...(session?.image_settings ?? {}),
        enabled,
        engine: "comfyui",
      });
      setSession(next);
    });
  };

  const handleGenerateImage = async () => {
    setImageGenerating(true);
    try {
      await runAction(async () => {
        const result = await trpgPlayApi.generateImage(sessionId);
        if (result.event) {
          setEvents((prev) => [...prev, result.event as TrpgPlayEvent]);
        }
      });
    } finally {
      setImageGenerating(false);
    }
  };

  const handlePrivateStateSave = async () => {
    if (isSpectator) return;
    await runAction(async () => {
      const saved = await trpgPlayApi.patchPrivateState(sessionId, privateState);
      setPrivateState(saved.state ?? { entries: {} });
    });
  };

  if (loading) {
    return (
      <TrpgWorkspaceShell playMode>
        <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" aria-hidden="true" />
          卓を読み込み中…
        </div>
      </TrpgWorkspaceShell>
    );
  }

  if (!session) {
    return (
      <TrpgWorkspaceShell playMode>
        <div className="p-6">
          <p className="text-sm text-muted-foreground">卓が見つからないか、参加権限がありません。</p>
          <div className="mt-4 grid gap-3 max-w-md">
            <Label htmlFor="display-name">表示名</Label>
            <Input id="display-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
            <Label htmlFor="invite-code">招待コード</Label>
            <Input id="invite-code" value={inviteCode} onChange={(e) => setInviteCode(e.target.value)} />
            <Button onClick={() => void handleJoin()}>参加する</Button>
          </div>
        </div>
      </TrpgWorkspaceShell>
    );
  }

  return (
    <TrpgWorkspaceShell playMode>
      <div className="mx-auto grid max-w-[1480px] gap-4 p-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <section className="rounded-lg border border-border bg-card">
          <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
            <div>
              <h1 className="text-lg font-semibold">{session.title}</h1>
              <p className="text-xs text-muted-foreground">
                {session.status} · {session.gm_mode} GM · コード {session.invite_code}
                {connected ? " · 接続中" : " · 切断"}
              </p>
            </div>
            <div className="flex gap-2">
              {session.status === "lobby" && canManageSession ? (
                <Button size="sm" onClick={() => void handleStart()}>開始</Button>
              ) : null}
              {session.status !== "ended" && canManageSession ? (
                <Button size="sm" variant="outline" onClick={() => void handleEnd()}>終了</Button>
              ) : null}
              {session.status !== "ended" && myParticipant ? (
                <Button size="sm" variant="outline" onClick={() => void handleLeave()}>退出</Button>
              ) : null}
            </div>
          </header>
          {isSpectator ? (
            <p className="border-b border-border px-4 py-2 text-xs text-muted-foreground">
              観戦モードです。発言・ダイス・共有状態の編集はできません。
            </p>
          ) : null}
          {actionError ? (
            <p className="border-b border-border px-4 py-2 text-xs text-destructive">{actionError}</p>
          ) : null}
          <div className="max-h-[60vh] space-y-2 overflow-y-auto p-4 text-sm">
            {events.map((event) => {
              const { mediaId, imageStatus } = eventImageMeta(event);
              return (
                <div key={event.id} className="rounded border border-border/70 bg-muted/20 px-3 py-2">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    {event.kind}
                    {event.actor_display_name ? ` · ${event.actor_display_name}` : ""}
                  </div>
                  <div className="mt-1 whitespace-pre-wrap">{event.body}</div>
                  {imageStatus === "pending" ? (
                    <p className="mt-2 text-xs text-muted-foreground">画像生成中…</p>
                  ) : null}
                  {imageStatus === "failed" ? (
                    <p className="mt-2 text-xs text-destructive">画像生成に失敗しました</p>
                  ) : null}
                  {mediaId && imageStatus === "succeeded" ? (
                    <img
                      src={playGeneratedMediaUrl(mediaId)}
                      alt="卓の場面画像"
                      className="mt-2 max-h-64 rounded border border-border object-contain"
                    />
                  ) : null}
                </div>
              );
            })}
          </div>
          {canPlay ? (
            <div className="border-t border-border p-4 space-y-3">
              <div className="flex flex-wrap gap-2">
                {ACTION_KINDS.map((kind) => (
                  <Button
                    key={kind}
                    size="sm"
                    variant={actionKind === kind ? "default" : "outline"}
                    onClick={() => setActionKind(kind)}
                  >
                    {kind}
                  </Button>
                ))}
              </div>
              <Textarea value={actionText} onChange={(e) => setActionText(e.target.value)} rows={3} />
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => void handleAction()}>送信</Button>
                <Button
                  variant="outline"
                  disabled={!imageEnabled || imageGenerating}
                  onClick={() => void handleGenerateImage()}
                >
                  {imageGenerating ? "生成中…" : "この場面の画像"}
                </Button>
              </div>
            </div>
          ) : null}
        </section>

        <aside className="space-y-4">
          {session.status === "lobby" && canManageSession ? (
            <section className="rounded-lg border border-border bg-card p-4 space-y-2">
              <h2 className="text-sm font-semibold">卓画像</h2>
              <p className="text-xs text-muted-foreground">
                作品の挿絵設定とは独立です。OFF のときは自動・手動とも生成しません。
              </p>
              <Button
                size="sm"
                variant={imageEnabled ? "default" : "outline"}
                onClick={() => void handleImageToggle(!imageEnabled)}
              >
                {imageEnabled ? "画像 ON" : "画像 OFF"}
              </Button>
            </section>
          ) : null}

          <section className="rounded-lg border border-border bg-card p-4">
            <h2 className="text-sm font-semibold">参加者</h2>
            <ul className="mt-2 space-y-1 text-sm">
              {participants.map((participant) => (
                <li key={participant.id}>
                  {participant.display_name} ({participant.role})
                </li>
              ))}
            </ul>
          </section>

          <section className="rounded-lg border border-border bg-card p-4 space-y-2">
            <h2 className="text-sm font-semibold">ダイス</h2>
            <Input value={diceExpr} onChange={(e) => setDiceExpr(e.target.value)} disabled={!canPlay} />
            <Input value={diceNote} onChange={(e) => setDiceNote(e.target.value)} placeholder="メモ" disabled={!canPlay} />
            <Button size="sm" onClick={() => void handleDice()} disabled={!canPlay}>振る</Button>
          </section>

          {!isSpectator ? (
          <section className="rounded-lg border border-border bg-card p-4 space-y-2">
            <h2 className="text-sm font-semibold">Whisper</h2>
            <select
              className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
              value={whisperTargetId}
              onChange={(e) => setWhisperTargetId(e.target.value)}
            >
              <option value="">宛先を選択</option>
              {participants
                .filter((item) => item.id !== myParticipant?.id)
                .map((item) => (
                  <option key={item.id} value={item.id}>{item.display_name}</option>
                ))}
            </select>
            <Textarea value={whisperText} onChange={(e) => setWhisperText(e.target.value)} rows={2} />
            <Button size="sm" onClick={() => void handleWhisper()}>送る</Button>
            <ul className="space-y-1 text-xs text-muted-foreground">
              {whispers.map((whisper) => (
                <li key={whisper.id}>{whisper.body}</li>
              ))}
            </ul>
          </section>
          ) : null}

          {!isSpectator ? (
          <section className="rounded-lg border border-border bg-card p-4 space-y-2">
            <h2 className="text-sm font-semibold">非公開状態</h2>
            <Textarea
              value={JSON.stringify(privateState, null, 2)}
              onChange={(e) => {
                try {
                  setPrivateState(JSON.parse(e.target.value) as TrpgPlayPrivateState["state"]);
                } catch {
                  // keep previous while typing invalid JSON
                }
              }}
              rows={6}
              disabled={isSpectator}
            />
            <Button
              size="sm"
              variant="outline"
              onClick={() => void handlePrivateStateSave()}
              disabled={isSpectator}
            >
              保存
            </Button>
          </section>
          ) : null}

          {isGm ? (
          <section className="rounded-lg border border-border bg-card p-4 space-y-2">
            <h2 className="text-sm font-semibold">GM共有の非公開状態</h2>
            {gmPrivateStates.length === 0 ? (
              <p className="text-xs text-muted-foreground">共有されている非公開状態はありません</p>
            ) : (
              <ul className="space-y-3 text-sm">
                {gmPrivateStates.map((item) => {
                  const entries = item.state?.entries ?? {};
                  return (
                    <li key={item.participant_id} className="space-y-1">
                      <div className="font-medium">{gmPrivateStateDisplayName(item, participants)}</div>
                      <ul className="space-y-0.5 text-xs text-muted-foreground">
                        {Object.entries(entries).map(([key, entry]) => (
                          <li key={key}>
                            {key}: {JSON.stringify(entry?.value)}
                          </li>
                        ))}
                      </ul>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
          ) : null}

          {canManageSession ? (
          <section className="rounded-lg border border-border bg-card p-4 space-y-2">
            <h2 className="text-sm font-semibold">共有スナップショット</h2>
            <Textarea value={snapshotJson} onChange={(e) => setSnapshotJson(e.target.value)} rows={6} />
            <Button size="sm" variant="outline" onClick={() => void handleSnapshotSave()}>保存</Button>
          </section>
          ) : null}
        </aside>
      </div>
    </TrpgWorkspaceShell>
  );
}
