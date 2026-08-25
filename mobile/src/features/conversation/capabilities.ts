import type { ConversationSession } from "../../types/api";
import type {
  ConnectionCapability,
  ConversationCommand,
  ConversationDiagnostics,
  RunState,
  SessionCapability,
  SessionKind,
  TransportState,
  UserCapability,
} from "./models";

export type ConnectionStatusPresentation = {
  label: string;
  detail: string;
  color: string;
  /** 正常接続。true のときヘッダーはドットだけ表示しラベルを省く。 */
  healthy: boolean;
};

export function inferSessionKind(session: ConversationSession | null): SessionKind {
  if (!session) return "normal";
  const characterName = session.character_name || "";
  const title = session.title || "";
  if ((session as ConversationSession & { is_group_chat?: boolean }).is_group_chat) {
    return "group";
  }
  if (characterName.startsWith("trpg_room_") || title.startsWith("[TRPG]")) {
    return "trpg-linked";
  }
  if (/^scenario_roleplay:[^:]+:[^:]+$/.test(characterName)) {
    return "scenario-roleplay";
  }
  if (
    characterName.startsWith("scenario_") ||
    title.startsWith("[シナリオ]") ||
    title.startsWith("[執筆]")
  ) {
    return "scenario-writing";
  }
  return "normal";
}

export function buildUserCapability(
  isAuthenticated: boolean,
  role?: string | null,
): UserCapability {
  if (!isAuthenticated) return "anonymous";
  if (role === "admin") return "admin";
  return "authenticated";
}

export function buildConnectionCapability(args: {
  isAuthenticated: boolean;
  isConnected: boolean;
  online?: boolean;
  serverReachable?: boolean;
}): ConnectionCapability {
  if (!args.isAuthenticated) return "local-only";
  if (args.isConnected) return "online";
  if (args.online === false) return "offline";
  return args.serverReachable === false ? "degraded" : "online";
}

export function buildTransportState(args: {
  isAuthenticated: boolean;
  isConnected: boolean;
  online?: boolean;
}): TransportState {
  if (!args.isAuthenticated || args.online === false) return "offline";
  return args.isConnected ? "websocket-connected" : "rest-dispatch";
}

export function buildConnectionStatusPresentation(
  diagnostics: ConversationDiagnostics,
): ConnectionStatusPresentation {
  if (diagnostics.connectionCapability === "local-only") {
    return {
      label: "ローカル",
      detail: "端末内で保存",
      color: "#89b4fa",
      healthy: false,
    };
  }
  if (diagnostics.connectionCapability === "offline") {
    return {
      label: "オフライン",
      detail: "ネットワークなし",
      color: "#f9e2af",
      healthy: false,
    };
  }
  if (
    diagnostics.connectionCapability === "degraded" &&
    !diagnostics.serverCheckedAt
  ) {
    return {
      label: "サーバー確認中",
      detail: "接続を確認しています",
      color: "#a6adc8",
      healthy: false,
    };
  }
  if (diagnostics.connectionCapability === "degraded") {
    return {
      label: "サーバー未接続",
      detail: "ローカル保存で継続",
      color: "#f38ba8",
      healthy: false,
    };
  }
  if (diagnostics.transportState === "websocket-connected") {
    return {
      label: "サーバー接続済み",
      detail: "リアルタイム",
      color: "#a6e3a1",
      healthy: true,
    };
  }
  return {
    label: "サーバー利用可",
    detail: "REST接続",
    color: "#a6e3a1",
    healthy: true,
  };
}

export function buildSessionCapabilities(args: {
  sessionKind: SessionKind;
  isAuthenticated: boolean;
  selectedProjectId?: string | null;
}): SessionCapability[] {
  const capabilities = new Set<SessionCapability>(["canChat"]);
  if (args.isAuthenticated) {
    capabilities.add("canSync");
    capabilities.add("canEdit");
    capabilities.add("canUseTools");
    capabilities.add("canResearch");
    capabilities.add("canManageRuntime");
  }
  if (args.selectedProjectId) {
    capabilities.add("canAttach");
  }
  if (args.sessionKind === "group") {
    capabilities.add("canGroupRespond");
  }
  if (
    args.sessionKind === "scenario-roleplay" ||
    args.sessionKind === "scenario-writing" ||
    args.sessionKind === "trpg-linked"
  ) {
    capabilities.add("canSteer");
  }
  return Array.from(capabilities);
}

function capabilityEnabled(
  capabilities: SessionCapability[],
  capability: SessionCapability,
) {
  return capabilities.includes(capability);
}

export function buildCommandRegistry(args: {
  isAuthenticated: boolean;
  sessionKind: SessionKind;
  capabilities: SessionCapability[];
  transportState: TransportState;
  runState: RunState;
  selectedProjectId?: string | null;
}): ConversationCommand[] {
  const busy = args.runState !== "idle";
  const serverRequiredReason = "サーバーログイン中のみ利用できます";
  const projectRequiredReason = "プロジェクト範囲を選ぶと利用できます";
  const busyReason = "現在の実行が終わるまで待機してください";
  const canServer = args.isAuthenticated && args.transportState !== "offline";
  const canSend = capabilityEnabled(args.capabilities, "canChat") && !busy;
  const canProject = Boolean(args.selectedProjectId);

  return [
    {
      id: "send-message",
      label: "送信",
      description: "通常メッセージをサーバー優先で送信し、失敗時はローカルに残します。",
      icon: "send",
      category: "message",
      enabled: canSend,
      reason: busy ? busyReason : undefined,
    },
    {
      id: "project-context",
      label: "Project Context",
      description: "現在のプロジェクト文脈を送信 payload に含めます。",
      icon: "folder-outline",
      category: "context",
      enabled: canProject,
      reason: canProject ? undefined : projectRequiredReason,
    },
    {
      id: "attach-file",
      label: "添付",
      description: "ファイルや画像を会話に添付します。",
      icon: "paperclip",
      category: "context",
      enabled: capabilityEnabled(args.capabilities, "canAttach"),
      reason: capabilityEnabled(args.capabilities, "canAttach")
        ? undefined
        : projectRequiredReason,
    },
    {
      id: "deep-research",
      label: "Deep Research",
      description: "長時間ジョブとして調査を開始し、進捗を会話タイムラインに残します。",
      icon: "magnify-scan",
      category: "job",
      enabled: canServer && capabilityEnabled(args.capabilities, "canResearch") && !busy,
      reason: !canServer ? serverRequiredReason : busy ? busyReason : undefined,
    },
    {
      id: "group-respond",
      label: "Group Respond",
      description: "グループチャットの複数キャラクター応答を実行します。",
      icon: "account-group-outline",
      category: "session",
      enabled:
        args.sessionKind === "group" &&
        capabilityEnabled(args.capabilities, "canGroupRespond") &&
        canServer &&
        !busy,
      reason:
        args.sessionKind !== "group"
          ? "グループチャットで利用できます"
          : !canServer
            ? serverRequiredReason
            : busy
              ? busyReason
              : undefined,
    },
    {
      id: "scenario-steering",
      label: "Steering",
      description: "シナリオ/RP/TRPG の進行設定を開きます。",
      icon: "tune-variant",
      category: "session",
      enabled: capabilityEnabled(args.capabilities, "canSteer"),
      reason: capabilityEnabled(args.capabilities, "canSteer")
        ? undefined
        : "シナリオ/RP/TRPG セッションで利用できます",
    },
    {
      id: "edit-message",
      label: "Edit",
      description: "既存メッセージから新しい分岐を作ります。",
      icon: "pencil-outline",
      category: "message",
      enabled: canServer && capabilityEnabled(args.capabilities, "canEdit") && !busy,
      reason: !canServer ? serverRequiredReason : busy ? busyReason : undefined,
    },
    {
      id: "rerun-message",
      label: "Rerun",
      description: "直前のユーザー入力を再実行します。",
      icon: "refresh",
      category: "message",
      enabled: canServer && !busy,
      reason: !canServer ? serverRequiredReason : busy ? busyReason : undefined,
    },
    {
      id: "switch-model",
      label: "Model",
      description: "再実行時に利用するモデルを指定します。",
      icon: "chip",
      category: "settings",
      enabled: canServer,
      reason: canServer ? undefined : serverRequiredReason,
    },
    {
      id: "sync-now",
      label: "Sync",
      description: "ローカル保存・未送信キュー・サーバー履歴を同期します。",
      icon: "cloud-sync-outline",
      category: "context",
      enabled: args.isAuthenticated,
      reason: args.isAuthenticated ? undefined : serverRequiredReason,
    },
    {
      id: "open-settings",
      label: "Settings",
      description: "チャット、権限、ジョブ、同期の設定を開きます。",
      icon: "cog-outline",
      category: "settings",
      enabled: true,
    },
  ];
}
