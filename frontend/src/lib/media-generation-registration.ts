import { mediaOperationsApi } from "./media-operations-api";
import {
  personaScope,
  type PersonaWorkspaceScope,
} from "./persona-workspace-model";
import type {
  CreativeRecipe,
  CreativeRecipeCreateInput,
  GenerationWorkspace,
  mediaGenerationApi,
} from "./media-operations-generation-api";

export type WorkspaceRegistration = Parameters<
  typeof mediaGenerationApi.createWorkspace
>[0];
export type RecipeRegistrationDraft = {
  name: string;
  recipeType: string;
  modelId: string;
  workflowId: string;
  prompt: string;
  negative: string;
  candidateCount: string;
  aspectRatio: string;
  width: string;
  height: string;
  duration: string;
  frames: string;
  storyboard: string;
  references: string;
  identityPack: string;
  promptSchema: string;
  metered: boolean;
  humanReview: boolean;
  retainProvenance: boolean;
};
export type RegistrationAttempt = {
  version: 1;
  scopeKey: string;
  key: string;
} & (
  | { kind: "workspace"; body: WorkspaceRegistration }
  | { kind: "recipe"; body: CreativeRecipeCreateInput }
);
export const registrationScopeKey = (scope: PersonaWorkspaceScope) =>
  JSON.stringify([
    scope.ownerUserId,
    scope.projectId,
    scope.personaId,
    scope.personaRevisionId,
  ]);
export const registrationStorageKey = (scope: PersonaWorkspaceScope) =>
  `aoi:generation-registration:${registrationScopeKey(scope)}`;

const opaque = /^[A-Za-z][A-Za-z0-9._:-]{1,254}$/u;
const text = (value: string, label: string, max: number, required = false) => {
  const result = value.trim();
  if ((required && !result) || result.length > max || /\u0000/u.test(result))
    throw new Error(`${label}を確認してください。`);
  return result;
};
const ref = (value: string, label: string) => {
  const result = text(value, label, 255);
  if (result && !opaque.test(result))
    throw new Error(`${label}は通常の参照IDで指定してください。`);
  return result || null;
};
const integer = (
  value: string,
  label: string,
  max: number,
  required = false,
) => {
  if (!value.trim() && !required) return null;
  if (
    !/^\d+$/u.test(value.trim()) ||
    !Number.isSafeInteger(Number(value)) ||
    Number(value) < 1 ||
    Number(value) > max
  )
    throw new Error(`${label}は1〜${max}の整数で指定してください。`);
  return Number(value);
};
function assertScope(scope: PersonaWorkspaceScope) {
  if (
    ![
      scope.ownerUserId,
      scope.projectId,
      scope.personaId,
      scope.personaRevisionId,
    ].every((value) => typeof value === "string" && value.trim())
  )
    throw new Error("人格・所有者・Project・現在版を確認してください。");
}
export function workspaceRegistration(
  scope: PersonaWorkspaceScope,
  origin: string,
  workspace: string,
  project: string,
): WorkspaceRegistration {
  assertScope(scope);
  const external_workspace_id = text(workspace, "Workspace ID", 164, true);
  const external_project_id = text(project, "73 Project ID", 164) || null;
  if (
    !/^wsp_[A-Za-z0-9_-]{8,160}$/u.test(external_workspace_id) ||
    (external_project_id &&
      !/^prj_[A-Za-z0-9_-]{8,160}$/u.test(external_project_id))
  )
    throw new Error("73のWorkspace / Project参照IDを確認してください。");
  const base = text(origin, "接続先origin", 2048, true);
  let url: URL;
  try {
    url = new URL(base);
  } catch {
    throw new Error("接続先はhttp(s)のoriginを指定してください。");
  }
  if (
    /\s/u.test(base) ||
    !["http:", "https:"].includes(url.protocol) ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !["", "/"].includes(url.pathname)
  )
    throw new Error(
      "接続先は認証情報・パスを含まないhttp(s)のoriginを指定してください。",
    );
  return {
    project_id: scope.projectId,
    provider: "comfyui_workbench",
    external_workspace_id,
    external_project_id,
    base_url: url.origin,
  };
}
export const emptyRecipeDraft = (): RecipeRegistrationDraft => ({
  name: "",
  recipeType: "image",
  modelId: "",
  workflowId: "",
  prompt: "",
  negative: "",
  candidateCount: "1",
  aspectRatio: "",
  width: "",
  height: "",
  duration: "",
  frames: "",
  storyboard: "",
  references: "",
  identityPack: "",
  promptSchema: "",
  metered: false,
  humanReview: true,
  retainProvenance: true,
});
export function recipeRegistration(
  scope: PersonaWorkspaceScope,
  draft: RecipeRegistrationDraft,
): CreativeRecipeCreateInput {
  assertScope(scope);
  if (
    !["image", "image_set", "comic", "video", "thumbnail"].includes(
      draft.recipeType,
    )
  )
    throw new Error("制作形式を選択してください。");
  const video = draft.recipeType === "video";
  const model = text(draft.modelId, "モデル選択ID", 164, video);
  if (
    model &&
    !(
      video
        ? /^wsl_[A-Za-z0-9_-]{8,160}$/u
        : /^(?:ims|imd|ien)_[A-Za-z0-9_-]{8,160}$/u
    ).test(model)
  )
    throw new Error("制作形式に対応するモデル選択IDを指定してください。");
  const aspect = text(draft.aspectRatio, "縦横比", 16) || null;
  if (
    aspect &&
    (!/^\d{1,3}:\d{1,3}$/u.test(aspect) ||
      aspect.split(":").some((value) => Number(value) < 1))
  )
    throw new Error("縦横比は16:9のように正の整数で指定してください。");
  const references = [
    ...new Set(draft.references.split(/\s+/u).filter(Boolean)),
  ];
  if (references.length > 20 || references.some((value) => !opaque.test(value)))
    throw new Error("参考素材は参照IDを20件まで指定してください。");
  const storyboard = video
    ? draft.storyboard
        .split("\n")
        .map((value) => value.trim())
        .filter(Boolean)
    : [];
  if (
    storyboard.length > 100 ||
    storyboard.some((value) => value.length > 1000)
  )
    throw new Error("絵コンテは1行1000文字・100行までで指定してください。");
  return {
    persona_id: scope.personaId,
    persona_revision_id: scope.personaRevisionId,
    project_id: scope.projectId,
    name: text(draft.name, "Recipe名", 255, true),
    recipe_type: draft.recipeType,
    model_selection_id: model || null,
    workflow_id: ref(draft.workflowId, "workflow ID"),
    identity_pack_id: ref(draft.identityPack, "identity pack ID"),
    prompt_schema_id: ref(draft.promptSchema, "prompt schema ID"),
    prompt_template: text(draft.prompt, "prompt template", 8000, true),
    negative_requirements: text(draft.negative, "避ける内容", 8000) || null,
    reference_asset_ids: references,
    aspect_ratio: aspect,
    width: integer(draft.width, "幅", 16384),
    height: integer(draft.height, "高さ", 16384),
    duration_seconds: video
      ? integer(draft.duration, "動画の秒数", 86400)
      : null,
    frame_count: video ? integer(draft.frames, "フレーム数", 100000) : null,
    storyboard,
    candidate_count: integer(draft.candidateCount, "候補数", 20, true)!,
    cost_policy: {
      metered: draft.metered,
      requires_metered_ack: draft.metered,
    },
    human_review_policy: { required: draft.humanReview },
    provenance_retention_policy: { retain: draft.retainProvenance },
  };
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.entries(value)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`)
      .join(",")}}`;
  return JSON.stringify(value);
}
/** Stored pending requests are closed, scoped snapshots, never actor overrides. */
export function parseRegistrationAttempt(
  value: string | null,
  scope: PersonaWorkspaceScope,
): RegistrationAttempt | null {
  if (!value) return null;
  try {
    const stored = JSON.parse(value) as RegistrationAttempt;
    if (
      !stored ||
      Object.keys(stored).sort().join(",") !==
        "body,key,kind,scopeKey,version" ||
      stored.version !== 1 ||
      stored.scopeKey !== registrationScopeKey(scope) ||
      typeof stored.key !== "string" ||
      !/^media-registration:[a-f0-9-]{36}$/u.test(stored.key)
    )
      throw new Error();
    const body = stored.body;
    let normalized: WorkspaceRegistration | CreativeRecipeCreateInput;
    if (stored.kind === "workspace") {
      const input = body as WorkspaceRegistration;
      normalized = workspaceRegistration(
        scope,
        input.base_url,
        input.external_workspace_id,
        input.external_project_id ?? "",
      );
    } else if (stored.kind === "recipe") {
      const input = body as CreativeRecipeCreateInput;
      const policy = input.cost_policy as Record<string, unknown>;
      const human = input.human_review_policy as Record<string, unknown>;
      const provenance = input.provenance_retention_policy as Record<
        string,
        unknown
      >;
      normalized = recipeRegistration(scope, {
        ...emptyRecipeDraft(),
        name: input.name,
        recipeType: input.recipe_type,
        modelId: input.model_selection_id ?? "",
        workflowId: input.workflow_id ?? "",
        prompt: input.prompt_template,
        negative: input.negative_requirements ?? "",
        candidateCount: String(input.candidate_count),
        aspectRatio: input.aspect_ratio ?? "",
        width: input.width == null ? "" : String(input.width),
        height: input.height == null ? "" : String(input.height),
        duration:
          input.duration_seconds == null ? "" : String(input.duration_seconds),
        frames: input.frame_count == null ? "" : String(input.frame_count),
        storyboard: input.storyboard?.join("\n") ?? "",
        references: input.reference_asset_ids?.join("\n") ?? "",
        identityPack: input.identity_pack_id ?? "",
        promptSchema: input.prompt_schema_id ?? "",
        metered: policy?.metered === true,
        humanReview: human?.required === true,
        retainProvenance: provenance?.retain === true,
      });
    } else throw new Error();
    if (canonical(body) !== canonical(normalized)) throw new Error();
    return stored;
  } catch {
    throw new Error(
      "保存中の登録内容を照合できません。ページを再読込し、保存状態を確認してください。",
    );
  }
}
export async function confirmRegistrationScope(
  scope: PersonaWorkspaceScope,
): Promise<void> {
  const response = await fetch("/api/auth/status", {
    credentials: "include",
    cache: "no-store",
  });
  const session = await response.json();
  if (
    !response.ok ||
    session?.authenticated !== true ||
    session.user?.id !== scope.ownerUserId
  )
    throw new Error(
      "ログイン中の所有者がこの人格の所有者と一致しません。登録を停止しました。",
    );
  const persona = await mediaOperationsApi.getPersona(scope.personaId);
  const current = personaScope(persona);
  if (
    !current ||
    registrationScopeKey(current) !== registrationScopeKey(scope) ||
    ["paused", "archived"].includes(persona.state)
  )
    throw new Error(
      "人格・Project・現在版が変更されています。人格Workspaceを再読込してください。",
    );
}
export function workspaceRegistrationMatches(
  value: GenerationWorkspace,
  scope: PersonaWorkspaceScope,
  body: WorkspaceRegistration,
): boolean {
  return Boolean(
    value?.id &&
    value.owner_user_id === scope.ownerUserId &&
    value.project_id === scope.projectId &&
    value.status === "configured" &&
    typeof value.config_hash === "string" &&
    /^[a-f0-9]{64}$/u.test(value.config_hash) &&
    [
      "provider",
      "external_workspace_id",
      "external_project_id",
      "base_url",
    ].every((key) => value[key] === body[key as keyof WorkspaceRegistration]),
  );
}
export function recipeRegistrationMatches(
  value: CreativeRecipe,
  scope: PersonaWorkspaceScope,
  body: CreativeRecipeCreateInput,
): boolean {
  const revision = value?.current_revision;
  const expected = { ...body } as Record<string, unknown>;
  delete expected.name;
  delete expected.persona_id;
  delete expected.project_id;
  return Boolean(
    value?.id &&
    value.owner_user_id === scope.ownerUserId &&
    value.project_id === scope.projectId &&
    value.persona_id === scope.personaId &&
    value.name === body.name &&
    revision?.id &&
    revision.creative_recipe_id === value.id &&
    revision.persona_id === scope.personaId &&
    revision.owner_user_id === scope.ownerUserId &&
    revision.project_id === scope.projectId &&
    typeof revision.content_hash === "string" &&
    /^[a-f0-9]{64}$/u.test(revision.content_hash) &&
    Object.entries(expected).every(
      ([key, item]) => canonical(revision[key]) === canonical(item),
    ),
  );
}
