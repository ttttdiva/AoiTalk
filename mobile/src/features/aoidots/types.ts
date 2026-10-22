import type { ConversationMessage } from "../../types/api";
import type { components } from "../../types/api-types.gen";

export type AoiDotsProfile = components["schemas"]["AoiDotsProfile"];
export type AoiDotsRefs = components["schemas"]["AgentDialogueRefs"];
export type AoiDotsBudget = components["schemas"]["AgentDialogueBudgetDTO"];
export type AoiDotsInput = Omit<
  components["schemas"]["AgentDialogueInputDTO"],
  "choices" | "allow_multiple" | "allow_free_text"
> & {
  choices: string[];
  allow_multiple: boolean;
  allow_free_text: boolean;
};
export type AoiDotsArtifact = components["schemas"]["ArtifactSafeRef"];
export type AoiDotsWorkItem = Omit<
  components["schemas"]["AoiDotsWorkItem"],
  "allowed_actions" | "pending_inputs" | "artifacts"
> & {
  allowed_actions: Array<"stop" | "resume" | "rework" | "answer">;
  pending_inputs: AoiDotsInput[];
  artifacts: AoiDotsArtifact[];
};
export type AoiDotsWorkList = Omit<components["schemas"]["AoiDotsWorkList"], "items"> & {
  items: AoiDotsWorkItem[];
};

/** GET /api/agent-dialogues/{id} has the same canonical safe projection. */
export type AoiDotsDialogue = Omit<components["schemas"]["AgentDialogueResponse"], "allowed_actions" | "pending_inputs" | "artifacts"> & {
  allowed_actions: Array<"stop" | "resume" | "rework" | "answer">;
  pending_inputs: AoiDotsInput[];
  artifacts: AoiDotsArtifact[];
  messages?: ConversationMessage[];
};

export type AoiDotsRootPointer = {
  auth_scope: string;
  server_fingerprint: string;
  root_id: string;
  session_id: string;
  agent_id: string;
  cached_at: string;
};

export type AoiDotsCommand =
  | {
      kind: "answer";
      input_id: string;
      answer: string;
      expected_input_revision: number;
      expected_control_revision: number;
    }
  | {
      kind: "control";
      action: "stop" | "resume" | "rework";
      expected_control_revision: number;
      instruction?: string;
    };
