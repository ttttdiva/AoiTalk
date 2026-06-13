"use client";

import {
  useEffect,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { PlayLog, Room } from "@/lib/trpg-room-utils";

// TRPG ルームの WebSocket 接続・購読ライフサイクルを管理するカスタムフック
export function useTrpgRoomSocket({
  roomId,
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
}: {
  roomId: string | undefined;
  inviteCode: string;
  bgmAutoEnabled: boolean;
  setRoom: Dispatch<SetStateAction<Room | null>>;
  setGmThinking: Dispatch<SetStateAction<boolean>>;
  setImageGenerating: Dispatch<SetStateAction<boolean>>;
  handleBgmLog: (log: PlayLog) => void;
  playBgmTrack: (track: string, volume?: number) => Promise<void>;
  loadRoom: () => Promise<void> | void;
  loadDisclosures: () => Promise<void> | void;
  loadPrivateMessages: () => Promise<void> | void;
}) {
  const wsRef = useRef<WebSocket | null>(null);

  // ── WebSocket 接続 ──
  useEffect(() => {
    if (!roomId) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const directNextPort =
      window.location.port &&
      !["3000", "6002"].includes(window.location.port);
    const wsHost = directNextPort
      ? `${window.location.hostname}:3000`
      : window.location.host;
    const wsQuery = inviteCode
      ? `?invite_code=${encodeURIComponent(inviteCode)}`
      : "";
    const wsUrl = `${proto}://${wsHost}/ws/trpg/${roomId}${wsQuery}`;
    let ws: WebSocket | null = null;
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      console.error("WebSocket open failed", e);
      return;
    }
    wsRef.current = ws;

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "state_sync" && msg.room) {
          setRoom((prev) => (prev ? { ...prev, ...msg.room } : msg.room));
        } else if (msg.type === "log_append" && msg.log) {
          setRoom((prev) =>
            prev && !prev.logs.some((log) => log.id === msg.log.id)
              ? { ...prev, logs: [...prev.logs, msg.log] }
              : prev
          );
          if (msg.log.log_type === "bgm") {
            handleBgmLog(msg.log);
          }
          setGmThinking(false);
          if (msg.log.log_type === "image") {
            setImageGenerating(false);
          }
        } else if (msg.type === "participant_update" && msg.participant) {
          setRoom((prev) => {
            if (!prev) return prev;
            const idx = prev.participants.findIndex(
              (p) => p.id === msg.participant.id
            );
            const next = [...prev.participants];
            if (idx >= 0) next[idx] = msg.participant;
            else next.push(msg.participant);
            return { ...prev, participants: next };
          });
        } else if (msg.type === "scene_change") {
          loadRoom();
        } else if (msg.type === "shared_state" && msg.shared_state) {
          setRoom((prev) =>
            prev ? { ...prev, shared_state: msg.shared_state } : prev
          );
        } else if (msg.type === "disclosure_refresh") {
          void loadDisclosures();
        } else if (msg.type === "private_refresh") {
          void loadPrivateMessages();
        } else if (msg.type === "gm_markers" && msg.markers) {
          if (typeof msg.markers.bgm === "string" && bgmAutoEnabled) {
            void playBgmTrack(msg.markers.bgm, 0.45);
          }
          if (msg.markers.scene_change) {
            void loadRoom();
          }
        } else if (msg.type === "gm_thinking") {
          setGmThinking(true);
        } else if (msg.type === "image_generating") {
          setImageGenerating(true);
        }
      } catch (e) {
        console.error("WS parse error", e);
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
    };

    return () => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.close();
      }
    };
  }, [
    bgmAutoEnabled,
    handleBgmLog,
    loadDisclosures,
    loadPrivateMessages,
    loadRoom,
    playBgmTrack,
    inviteCode,
    roomId,
    setGmThinking,
    setImageGenerating,
    setRoom,
  ]);
}
