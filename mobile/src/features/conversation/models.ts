import type {
  ChatResponseModelOption,
  ChatResponseModelSelection,
  ConversationMessage,
  ConversationSession,
} from "../../types/api";
import type { DirectMobileLlmSelection } from "../../lib/mobile-llm";
import type { ChatResponseTarget } from "./chat-llm-preferences";
import type {
  ChatCommandCapability,
  SkillSlashCommand,
} from "./chat-commands";
import type { ChatAttachmentMetadata } from "../../lib/chat-api";

export type UserCapability = "anonymous" | "authenticated" | "admin" | "restricted";

export type SessionCapability =
  | "canChat"
  | "canAttach"
  | "canResearch"
  | "canUseTools"
  | "canManageRuntime"
  | "canGroupRespond"
  | "canSteer"
  | "canSync"
  | "canEdit";

export type ConnectionCapability =
  | "online"
  | "degraded"
  | "offline"
  | "local-only";

export type SessionKind =
  | "normal"
  | "group"
  | "scenario-writing"
  | "scenario-roleplay"
  | "trpg-linked";

export type TransportState =
  | "websocket-connected"
  | "reconnecting"
  | "rest-dispatch"
  | "offline";

export type MessageState =
  | "local-draft"
  | "queued"
  | "dispatched"
  | "persisted"
  | "failed";

export type RunState =
  | "idle"
  | "waiting"
  | "streaming"
  | "tool-running"
  | "permission-required"
  | "job-running"
  | "background-running";

export type SyncState =
  | "clean"
  | "pending-upload"
  | "pending-refresh"
  | "conflict";

export type LlmSelectionSyncStatus =
  | "idle"
  | "pending"
  | "syncing"
  | "synced"
  | "unsynced"
  | "rejected";

/** 直近の生成で実際に使われた provider/model/effort。 */
export type EffectiveGenerationRoute =
  | {
      kind: "server";
      provider?: string;
      model?: string;
      reasoningEffort?: string;
      fallback?: boolean;
    }
  | {
      kind: "direct";
      provider: string;
      model: string;
      reasoningEffort?: string;
      fallback?: boolean;
    };

export type ConversationCommandId =
  | "send-message"
  | "attach-file"
  | "deep-research"
  | "group-respond"
  | "scenario-steering"
  | "edit-message"
  | "rerun-message"
  | "switch-model"
  | "project-context"
  | "sync-now"
  | "open-settings";

export type ConversationCommand = {
  id: ConversationCommandId;
  label: string;
  description: string;
  icon: string;
  category: "message" | "job" | "context" | "session" | "settings";
  enabled: boolean;
  reason?: string;
};

export type ConversationDiagnostics = {
  userCapability: UserCapability;
  sessionKind: SessionKind;
  sessionCapabilities: SessionCapability[];
  connectionCapability: ConnectionCapability;
  transportState: TransportState;
  runState: RunState;
  syncState: SyncState;
  activeTool: string | null;
  activityMessage: string | null;
  pendingMessages: number;
  lastRefreshAt?: string | null;
  serverCheckedAt?: number | null;
};

export type PermissionRequest = {
  requestId: string;
  toolName: string;
  description: string;
  riskSummary?: string;
  toolArgs: Record<string, unknown>;
  receivedAt: string;
  status: "pending" | "approved" | "denied" | "expired" | "completed";
};

export type ConversationJobType =
  | "deep_research"
  | "agent_run"
  | "file_analysis"
  | "scenario_generation"
  | "sync_repair"
  | "future";

export type ConversationJob = {
  id: string;
  type: ConversationJobType;
  title: string;
  status: "queued" | "running" | "finalizing" | "completed" | "failed" | "cancelled";
  progress: number;
  progressText?: string;
  resultText?: string;
  error?: string | null;
  sourceScope?: string | null;
  createdAt: string;
  updatedAt: string;
};

export type ConversationEvent =
  | {
      id: string;
      kind: "permission";
      title: string;
      description: string;
      severity: "info" | "warning" | "danger";
      request: PermissionRequest;
    }
  | {
      id: string;
      kind: "job";
      title: string;
      description: string;
      severity: "info" | "warning" | "danger";
      job: ConversationJob;
    }
  | {
      id: string;
      kind: "sync";
      title: string;
      description: string;
      severity: "info" | "warning" | "danger";
    }
  | {
      id: string;
      kind: "tool";
      title: string;
      description: string;
      severity: "info" | "warning" | "danger";
      toolName: string;
    }
  | {
      id: string;
      kind: "progress";
      title: string;
      description: string;
      severity: "info" | "warning" | "danger";
    };

export type TimelineItem =
  | { id: string; type: "message"; message: ConversationMessage }
  | { id: string; type: "event"; event: ConversationEvent }
  | { id: string; type: "stream"; content: string };

export type SendConversationCommand = {
  message: string;
  projectId?: string | null;
  appId?: string | null;
  appTargetId?: string | null;
  includeProjectContext?: boolean;
  agentMode?: string;
  editMessageId?: string;
  target?:
    | { kind: "server"; responseModel?: ChatResponseModelSelection }
    | { kind: "direct"; selection: DirectMobileLlmSelection };
  commandCapabilities?: ChatCommandCapability[];
  attachments?: ChatAttachmentMetadata[];
};

export type ConversationControllerSnapshot = {
  session: ConversationSession | null;
  messages: ConversationMessage[];
  visibleMessages: ConversationMessage[];
  timeline: TimelineItem[];
  diagnostics: ConversationDiagnostics;
  commands: ConversationCommand[];
  pendingPermissions: PermissionRequest[];
  jobs: ConversationJob[];
  loading: boolean;
  error: string | null;
  streamContent: string;
  llmMode: string;
  llmModeOptions: string[];
  llmModeLabels: Record<string, string>;
  llmModeKind: string | null;
  llmModeSyncStatus: LlmSelectionSyncStatus;
  llmSelectionMessage: string | null;
  llmPreferencesReady: boolean;
  effectiveGeneration: EffectiveGenerationRoute | null;
  responseModelOptions: ChatResponseModelOption[];
  responseModelOptionsLoading: boolean;
  responseTarget: ChatResponseTarget;
  skillCommands: SkillSlashCommand[];
  retryingMessageIds: string[];
};
