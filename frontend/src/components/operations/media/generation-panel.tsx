"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  Film,
  Image as ImageIcon,
  Loader2,
  Plus,
  RefreshCw,
  Send,
} from "lucide-react";

import {
  isOpaqueExternalId,
  mediaGenerationApi,
  type CreativeRecipe,
  type GenerationOutput,
  type GenerationCatalogItem,
  type GenerationKind,
  type GenerationPlan,
  type GenerationReconcileInput,
  type GenerationRequestSpec,
  type GenerationRun,
  type GenerationWorkspace,
} from "@/lib/media-operations-generation-api";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

function newIdempotencyKey(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }

  return `media-generation-${Date.now()}-${Math.random()
    .toString(36)
    .slice(2)}`;
}

function readableError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "Generation Studio APIでエラーが発生しました";
}

function statusLabel(status: string | null | undefined): string {
  const normalized = status?.trim().toLowerCase();
  if (!normalized) return "未設定";
  const labels: Record<string, string> = {
    configured: "設定済み",
    verified: "検証済み",
    unavailable: "利用不可",
    unsupported: "未対応",
    draft: "Draft",
    queued: "待機中",
    claimed: "割当済み",
    submitted: "送信済み",
    running: "実行中",
    output_pending: "出力待ち",
    succeeded: "成功",
    failed: "失敗",
    cancelled: "キャンセル済み",
    quarantined: "隔離済み",
    uncertain: "要照合",
  };
  return labels[normalized] ?? status ?? "未設定";
}

function StatusPill({ status }: { status: string | null | undefined }) {
  return (
    <span
      className="inline-flex items-center rounded-full border border-border bg-muted/50 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
      data-generation-status={status ?? "unknown"}
    >
      {statusLabel(status)}
    </span>
  );
}

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div
      role="alert"
      className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
    >
      {readableError(error)}
    </div>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-7 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

function FieldLabel({
  htmlFor,
  children,
}: {
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <label htmlFor={htmlFor} className="text-xs font-medium text-foreground">
      {children}
    </label>
  );
}

function opaqueLabel(
  value: string | null | undefined,
  prefix: "wsp_" | "prj_" | "run_" | "ast_" | "outv_",
): string {
  return isOpaqueExternalId(value, prefix) ? value! : "unavailable";
}

function isSafeModelSelectionId(value: string, kind: GenerationKind): boolean {
  return kind === "video"
    ? /^wsl_[A-Za-z0-9_-]{8,160}$/u.test(value)
    : /^(?:ims_|imd_|ien_)[A-Za-z0-9_-]{8,160}$/u.test(value);
}

function normalizeGenerationKind(
  value: unknown,
  fallback: GenerationKind = "image",
): GenerationKind {
  return value === "video" ? "video" : value === "image" ? "image" : fallback;
}

function safeCatalogItem(
  item: GenerationCatalogItem,
  expectedKind: GenerationKind,
): GenerationCatalogItem | null {
  const raw = item as unknown as Record<string, unknown>;
  const kind = normalizeGenerationKind(raw.kind, expectedKind);
  if (kind !== expectedKind) return null;
  const modelSelectionId =
    typeof raw.model_selection_id === "string"
      ? raw.model_selection_id
      : typeof raw.image_model_selection_id === "string"
        ? raw.image_model_selection_id
        : "";
  if (!isSafeModelSelectionId(modelSelectionId, expectedKind)) return null;
  return {
    kind,
    model_selection_id: modelSelectionId,
    ...(expectedKind === "image"
      ? { image_model_selection_id: modelSelectionId }
      : {}),
    display_name:
      typeof raw.display_name === "string" ? raw.display_name : null,
    status: typeof raw.status === "string" ? raw.status : "unavailable",
  };
}

function safeMediaPreviewUrl(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const candidate = value.trim();
  // A preview must be served by the AoiTalk proxy. Never put a provider URL,
  // signed query, local path, or object URL in a durable/UI authority.
  if (!candidate.startsWith("/api/python-proxy/")) return null;
  if (typeof window === "undefined") return null;
  try {
    const parsed = new URL(candidate, window.location.origin);
    if (
      parsed.origin !== window.location.origin ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    ) {
      return null;
    }
    return parsed.pathname;
  } catch {
    return null;
  }
}

function safeOutput(output: GenerationOutput): GenerationOutput | null {
  const assetId = output.external_asset_id;
  const version = output.external_output_version;
  if (!isOpaqueExternalId(assetId, "ast_") || !isOpaqueExternalId(version, "outv_")) {
    return null;
  }
  if (!/^[0-9a-f]{64}$/iu.test(output.sha256 || "")) return null;
  const mime = typeof output.mime_type === "string" ? output.mime_type.trim().toLowerCase() : "";
  // Keep the browser projection aligned with the server's closed MIME
  // contract.  In particular, arbitrary `video/*` values must not become a
  // playable preview because they could represent an unverified provider
  // payload rather than a typed Generation Studio output.
  const validVideoMime = new Set([
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-matroska",
    "video/other",
  ]);
  if (!/^image\/[a-z0-9][a-z0-9.+-]*$/u.test(mime) && !validVideoMime.has(mime)) return null;
  const kind = normalizeGenerationKind(
    output.generation_kind,
    mime.startsWith("video/") ? "video" : "image",
  );
  if ((kind === "video" && !mime.startsWith("video/")) || (kind === "image" && !mime.startsWith("image/"))) {
    return null;
  }
  return {
    ...output,
    mime_type: mime,
    generation_kind: kind,
    // Only a server-proxied preview is allowed to reach the DOM.
    preview_url: safeMediaPreviewUrl(output.preview_url ?? output.deep_link),
    deep_link: null,
  };
}

function safeRun(run: GenerationRun): GenerationRun {
  return {
    ...run,
    // Provider result links are not a browser authority. A future server
    // preview must be exposed through preview_url and is checked by
    // safeOutput before it reaches the DOM.
    result_deep_link: null,
    outputs: (run.outputs ?? [])
      .map(safeOutput)
      .filter((item): item is GenerationOutput => item !== null),
  };
}

function OutputRow({
  output,
  selected,
  selecting,
  onSelect,
}: {
  output: GenerationOutput;
  selected: boolean;
  selecting: boolean;
  onSelect: () => void;
}) {
  const assetId = opaqueLabel(output.external_asset_id, "ast_");
  const outputVersion = opaqueLabel(
    output.external_output_version,
    "outv_",
  );
  const kind = normalizeGenerationKind(
    output.generation_kind,
    output.mime_type?.startsWith("video/") ? "video" : "image",
  );

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border/70 bg-background/50 px-3 py-2.5">
      <div className="min-w-0">
        {output.preview_url ? (
          <div className="mb-2 overflow-hidden rounded border border-border/70 bg-muted/20">
            {kind === "video" ? (
              <video
                controls
                preload="metadata"
                className="max-h-48 w-full object-contain"
                src={output.preview_url}
                aria-label="Generated video preview"
                data-generation-preview="video"
              />
            ) : (
              // The URL has already been constrained to the AoiTalk proxy.
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={output.preview_url}
                alt="Generated image preview"
                className="max-h-48 w-full object-contain"
                data-generation-preview="image"
              />
            )}
          </div>
        ) : null}
        <div className="flex flex-wrap items-center gap-2 font-mono text-xs">
          <span data-generation-asset-id={assetId}>{assetId}</span>
          <span className="text-muted-foreground">·</span>
          <span data-generation-output-version={outputVersion}>
            {outputVersion}
          </span>
        </div>
        <p className="mt-1 text-[11px] text-muted-foreground">
          {output.mime_type || "unavailable"}
          {output.width && output.height
            ? ` · ${output.width}×${output.height}`
            : ""}
          {output.sha256 ? ` · sha256 ${output.sha256.slice(0, 12)}…` : ""}
        </p>
      </div>
      <Button
        type="button"
        variant={selected ? "secondary" : "outline"}
        size="sm"
        disabled={selecting || assetId === "unavailable"}
        onClick={onSelect}
      >
        {selecting ? (
          <Loader2 className="size-3.5 animate-spin" />
        ) : selected ? (
          <Check className="size-3.5" />
        ) : null}
        {selected ? "選択済み" : "採用候補に選択"}
      </Button>
    </div>
  );
}

export function MediaGenerationPanel() {
  const [workspaces, setWorkspaces] = useState<GenerationWorkspace[]>([]);
  const [recipes, setRecipes] = useState<CreativeRecipe[]>([]);
  const [plans, setPlans] = useState<GenerationPlan[]>([]);
  const [runs, setRuns] = useState<GenerationRun[]>([]);
  const [catalog, setCatalog] = useState<GenerationCatalogItem[]>([]);

  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState("");
  const [selectedRecipeId, setSelectedRecipeId] = useState("");
  const [selectedPlanId, setSelectedPlanId] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedOutputId, setSelectedOutputId] = useState("");

  const [prompt, setPrompt] = useState("");
  const [negativePrompt, setNegativePrompt] = useState("");
  const [seed, setSeed] = useState("");
  const [modelSelectionId, setModelSelectionId] = useState("");
  const [personaRevisionId, setPersonaRevisionId] = useState("");
  const [sizePresetId, setSizePresetId] = useState("normal_square");
  const [width, setWidth] = useState("");
  const [height, setHeight] = useState("");
  const [aspectRatio, setAspectRatio] = useState("");
  const [requestedOutputs, setRequestedOutputs] = useState("1");
  const [acceptMeteredGeneration, setAcceptMeteredGeneration] =
    useState(false);
  const [acknowledgeMeteredGeneration, setAcknowledgeMeteredGeneration] =
    useState(false);
  const [durationSeconds, setDurationSeconds] = useState("");
  const [frameCount, setFrameCount] = useState("");
  const [storyboard, setStoryboard] = useState("");

  const [loading, setLoading] = useState(true);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [submitReceipt, setSubmitReceipt] = useState<{
    planId: string;
    status: string;
    requestHash?: string | null;
  } | null>(null);
  const [busy, setBusy] = useState<"plan" | "submit" | "refresh" | "reconcile" | "select" | null>(
    null,
  );
  const [error, setError] = useState<unknown>(null);
  const [catalogError, setCatalogError] = useState<unknown>(null);
  const planRequestRef = useRef(0);
  const runRequestRef = useRef(0);
  const catalogRequestRef = useRef(0);
  const createPlanRequestRef = useRef(0);
  const submitRequestRef = useRef(0);
  const reconcileRequestRef = useRef(0);
  const selectRequestRef = useRef(0);

  const selectedWorkspace = useMemo(
    () =>
      workspaces.find((workspace) => workspace.id === selectedWorkspaceId) ??
      null,
    [selectedWorkspaceId, workspaces],
  );
  const selectedRecipe = useMemo(
    () => recipes.find((recipe) => recipe.id === selectedRecipeId) ?? null,
    [recipes, selectedRecipeId],
  );
  const selectedPlan = useMemo(
    () => plans.find((plan) => plan.id === selectedPlanId) ?? null,
    [plans, selectedPlanId],
  );
  const selectedRun = useMemo(
    () => runs.find((run) => run.id === selectedRunId) ?? null,
    [runs, selectedRunId],
  );
  const selectedRevision = selectedRecipe?.current_revision ?? null;
  const generationKind = normalizeGenerationKind(selectedRevision?.recipe_type);
  const resolvedModelSelectionId =
    modelSelectionId.trim() ||
    selectedRevision?.model_selection_id?.trim() ||
    selectedRevision?.image_model_selection_id?.trim() ||
    "";

  const loadBindings = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [nextWorkspaces, nextRecipes] = await Promise.all([
        mediaGenerationApi.listWorkspaces(),
        mediaGenerationApi.listRecipes(),
      ]);
      setWorkspaces(nextWorkspaces);
      setRecipes(nextRecipes);
      setSelectedWorkspaceId((current) =>
        nextWorkspaces.some((item) => item.id === current)
          ? current
          : nextWorkspaces[0]?.id ?? "",
      );
      setSelectedRecipeId((current) =>
        nextRecipes.some((item) => item.id === current)
          ? current
          : nextRecipes[0]?.id ?? "",
      );
    } catch (nextError) {
      setError(nextError);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadPlans = useCallback(async () => {
    const requestId = ++planRequestRef.current;
    if (!selectedWorkspaceId) {
      setPlans([]);
      setSelectedPlanId("");
      return;
    }
    try {
      const nextPlans = await mediaGenerationApi.listPlans({
        workspaceId: selectedWorkspaceId,
      });
      if (requestId !== planRequestRef.current) return;
      setPlans(nextPlans);
      setSelectedPlanId((current) =>
        nextPlans.some((item) => item.id === current)
          ? current
          : nextPlans[0]?.id ?? "",
      );
    } catch (nextError) {
      setError(nextError);
    }
  }, [selectedWorkspaceId]);

  const loadRuns = useCallback(async () => {
    const requestId = ++runRequestRef.current;
    if (!selectedPlanId) {
      setRuns([]);
      setSelectedRunId("");
      return;
    }
    try {
      const nextRuns = await mediaGenerationApi.listRuns(selectedPlanId);
      if (requestId !== runRequestRef.current) return;
      setRuns(nextRuns.map(safeRun));
      setSelectedRunId((current) =>
        nextRuns.some((item) => item.id === current)
          ? current
          : nextRuns[0]?.id ?? "",
      );
    } catch (nextError) {
      setError(nextError);
    }
  }, [selectedPlanId]);

  useEffect(() => {
    const task = window.setTimeout(() => void loadBindings(), 0);
    return () => window.clearTimeout(task);
  }, [loadBindings]);

  useEffect(() => {
    const task = window.setTimeout(() => void loadPlans(), 0);
    return () => window.clearTimeout(task);
  }, [loadPlans]);

  useEffect(() => {
    const task = window.setTimeout(() => void loadRuns(), 0);
    return () => window.clearTimeout(task);
  }, [loadRuns]);

  useEffect(() => {
    // Recipe type is the authority for the media kind. Clear transient form
    // state when changing revisions so a video selector cannot leak into an
    // image request (or vice versa).
    const task = window.setTimeout(() => {
      setModelSelectionId("");
      setDurationSeconds(
        selectedRevision?.duration_seconds != null
          ? String(selectedRevision.duration_seconds)
          : "",
      );
      setFrameCount(
        selectedRevision?.frame_count != null
          ? String(selectedRevision.frame_count)
          : "",
      );
      setStoryboard((selectedRevision?.storyboard ?? []).join("\n"));
      setAcknowledgeMeteredGeneration(false);
      setCatalog([]);
      setCatalogError(null);
    }, 0);
    return () => window.clearTimeout(task);
  }, [selectedRecipeId, selectedRevision]);

  useEffect(() => {
    const task = window.setTimeout(() => setSelectedOutputId(""), 0);
    return () => window.clearTimeout(task);
  }, [selectedRunId]);

  const fetchCatalog = async () => {
    if (!selectedWorkspaceId) {
      catalogRequestRef.current += 1;
      setCatalog([]);
      setCatalogError(new Error("Generation Workspaceを選択してください"));
      return;
    }
    const requestId = ++catalogRequestRef.current;
    const workspaceId = selectedWorkspaceId;
    const kind = generationKind;
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      // Keep compatibility with image-only mocks/rolling deployments while
      // preferring the explicit kind-aware endpoint.
      const genericCatalog = (
        mediaGenerationApi as typeof mediaGenerationApi & {
          listGenerationCatalog?: (
            workspaceId: string,
            kind: GenerationKind,
          ) => Promise<GenerationCatalogItem[]>;
          listCatalog?: (
            workspaceId: string,
            kind: GenerationKind,
          ) => Promise<GenerationCatalogItem[]>;
        }
      ).listGenerationCatalog ??
        (mediaGenerationApi as typeof mediaGenerationApi & {
          listCatalog?: (
            workspaceId: string,
            kind: GenerationKind,
          ) => Promise<GenerationCatalogItem[]>;
        }).listCatalog;
      const items = genericCatalog
        ? await genericCatalog(workspaceId, kind)
        : kind === "video"
          ? typeof mediaGenerationApi.listVideoCatalog === "function"
            ? await mediaGenerationApi.listVideoCatalog(workspaceId)
            : []
          : await mediaGenerationApi.listImageCatalog(workspaceId);
      if (requestId !== catalogRequestRef.current || workspaceId !== selectedWorkspaceId || kind !== generationKind) {
        return;
      }
      setCatalog(
        items
          .map((item) => safeCatalogItem(item, kind))
          .filter((item): item is GenerationCatalogItem => item !== null),
      );
    } catch (nextError) {
      if (requestId !== catalogRequestRef.current) return;
      setCatalog([]);
      setCatalogError(nextError);
    } finally {
      if (requestId === catalogRequestRef.current) setCatalogLoading(false);
    }
  };

  const createPlan = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const revision = selectedRevision;
    const outputCount = Number(requestedOutputs);
    if (!selectedWorkspaceId || !selectedRecipeId || !revision) {
      setError(new Error("Workspace・Creative Recipe・current revisionを選択してください"));
      return;
    }
    const pinnedPersonaRevisionId = revision.persona_revision_id || personaRevisionId.trim();
    if (!pinnedPersonaRevisionId || !prompt.trim()) {
      setError(new Error("Persona revisionとPromptが必要です"));
      return;
    }
    if (!resolvedModelSelectionId) {
      setError(
        new Error(
          `${generationKind === "video" ? "Video" : "Image"} model selection IDが必要です`,
        ),
      );
      return;
    }
    if (!Number.isInteger(outputCount) || outputCount < 1 || outputCount > 20) {
      setError(new Error("Requested outputsは1〜20で指定してください"));
      return;
    }

    const numericSeed = seed.trim() ? Number(seed) : null;
    const numericWidth = width.trim() ? Number(width) : null;
    const numericHeight = height.trim() ? Number(height) : null;
    if (numericSeed !== null && !Number.isFinite(numericSeed)) {
      setError(new Error("Seedは数値で指定してください"));
      return;
    }
    if (
      (numericWidth !== null && !Number.isInteger(numericWidth)) ||
      (numericHeight !== null && !Number.isInteger(numericHeight))
    ) {
      setError(new Error("Width / Heightは整数で指定してください"));
      return;
    }
    if (generationKind === "image" && sizePresetId !== "custom" && (numericWidth !== null || numericHeight !== null)) {
      setError(new Error("Width / Heightを指定する場合はcustom sizeを選択してください"));
      return;
    }
    if (
      generationKind === "image" &&
      sizePresetId === "custom" &&
      (numericWidth === null ||
        numericHeight === null ||
        numericWidth < 64 ||
        numericWidth > 2048 ||
        numericHeight < 64 ||
        numericHeight > 2048 ||
        numericWidth % 64 !== 0 ||
        numericHeight % 64 !== 0)
    ) {
      setError(new Error("custom sizeは64〜2048の64の倍数で指定してください"));
      return;
    }
    if (generationKind === "image" && aspectRatio.trim() && !/^[1-9][0-9]{0,2}:[1-9][0-9]{0,2}$/u.test(aspectRatio.trim())) {
      setError(new Error("Aspect ratioは例: 1:1 の形式で指定してください"));
      return;
    }
    if (!isSafeModelSelectionId(resolvedModelSelectionId, generationKind)) {
      setError(
        new Error(
          generationKind === "video"
            ? "Video model selection IDはwsl_ opaque IDで指定してください"
            : "Image model selection IDは許可されたopaque IDで指定してください",
        ),
      );
      return;
    }

    const numericDuration = durationSeconds.trim() ? Number(durationSeconds) : null;
    const numericFrameCount = frameCount.trim() ? Number(frameCount) : null;
    const storyboardLines = storyboard
      .split(/\r?\n/u)
      .map((line) => line.trim())
      .filter(Boolean);
    if (generationKind === "video") {
      if (
        numericDuration !== null &&
        (!Number.isInteger(numericDuration) || numericDuration < 1 || numericDuration > 86_400)
      ) {
        setError(new Error("Video durationは1〜86400秒の整数で指定してください"));
        return;
      }
      if (
        numericFrameCount !== null &&
        (!Number.isInteger(numericFrameCount) || numericFrameCount < 1 || numericFrameCount > 100_000)
      ) {
        setError(new Error("Frame countは1〜100000の整数で指定してください"));
        return;
      }
      if (storyboardLines.length > 100) {
        setError(new Error("Storyboardは100行以内で指定してください"));
        return;
      }
    }

    setBusy("plan");
    setError(null);
    const createRequestId = ++createPlanRequestRef.current;
    const workspaceSnapshot = selectedWorkspaceId;
    const recipeSnapshot = selectedRecipeId;
    try {
      const requestSpec: GenerationRequestSpec = {
        prompt: prompt.trim(),
        negative_prompt: negativePrompt.trim(),
        seed: numericSeed,
        ...(generationKind === "video"
          ? { model_selection_id: resolvedModelSelectionId }
          : { image_model_selection_id: resolvedModelSelectionId }),
        generation_settings: {},
        accept_metered_generation: acceptMeteredGeneration,
        ...(generationKind === "video"
          ? {
              duration_seconds: numericDuration,
              frame_count: numericFrameCount,
              storyboard: storyboardLines,
            }
          : {
              size_preset_id: sizePresetId as "normal_square" | "normal_landscape" | "normal_portrait" | "custom",
              width: numericWidth,
              height: numericHeight,
              aspect_ratio: aspectRatio.trim() || null,
            }),
      };
      const created = await mediaGenerationApi.createPlan(
        {
          persona_revision_id: pinnedPersonaRevisionId,
          creative_recipe_revision_id: revision.id,
          workspace_id: selectedWorkspaceId,
          requested_outputs: outputCount,
          request_spec: requestSpec,
        },
        newIdempotencyKey(),
      );
      if (
        createRequestId !== createPlanRequestRef.current ||
        selectedWorkspaceId !== workspaceSnapshot ||
        selectedRecipeId !== recipeSnapshot
      ) {
        return;
      }
      planRequestRef.current += 1;
      setPlans((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setSelectedPlanId(created.id);
      setSubmitReceipt(null);
    } catch (nextError) {
      if (createRequestId === createPlanRequestRef.current) setError(nextError);
    } finally {
      if (createRequestId === createPlanRequestRef.current) setBusy(null);
    }
  };

  const submitPlan = async () => {
    if (!selectedPlan) return;
    const planSnapshot = selectedPlan;
    const requestId = ++submitRequestRef.current;
    const metered = Boolean(planSnapshot.request_spec?.accept_metered_generation);
    if (metered && !acknowledgeMeteredGeneration) {
      setError(
        new Error(
          "Metered generationは人間の明示承認が必要です。送信前に承認してください",
        ),
      );
      return;
    }
    setBusy("submit");
    setError(null);
    try {
      const submitInput = {
        expected_plan_hash: planSnapshot.plan_hash ?? null,
        ...(metered
          ? { acknowledge_metered_generation: acknowledgeMeteredGeneration }
          : {}),
      };
      const receipt = await mediaGenerationApi.submitPlan(
        planSnapshot.id,
        submitInput,
        newIdempotencyKey(),
      );
      if (requestId !== submitRequestRef.current || selectedPlanId !== planSnapshot.id) {
        return;
      }
      const safeOutputs = (receipt.outputs ?? [])
        .map(safeOutput)
        .filter((item): item is GenerationOutput => item !== null);
      const run = receipt.run
        ? {
            ...safeRun(receipt.run),
            outputs: safeOutputs.length ? safeOutputs : safeRun(receipt.run).outputs ?? [],
          }
        : null;
      runRequestRef.current += 1;
      if (run?.id) {
        setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
        setSelectedRunId(run.id);
      } else {
        // An unavailable/uncertain receipt may have no provider run. Keep the
        // durable plan visible but never manufacture a run or external ref.
        setRuns((current) =>
          current.filter((item) => item.plan_id !== planSnapshot.id),
        );
        setSelectedRunId("");
      }
      setSubmitReceipt({
        planId: planSnapshot.id,
        status: receipt.status,
        requestHash: receipt.intent?.request_hash ?? null,
      });
      const nextPlanStatus =
        receipt.plan?.status ??
        (receipt.status === "unavailable" ? "unavailable" : "submitted");
      setPlans((current) =>
        current.map((item) =>
          item.id === planSnapshot.id
            ? receipt.plan?.id === planSnapshot.id
              ? receipt.plan
              : { ...item, status: nextPlanStatus }
            : item,
        ),
      );
    } catch (nextError) {
      if (requestId === submitRequestRef.current) setError(nextError);
    } finally {
      if (requestId === submitRequestRef.current) setBusy(null);
    }
  };

  const refreshRun = async () => {
    if (!selectedRun) return;
    const runSnapshot = selectedRun;
    const requestId = ++runRequestRef.current;
    setBusy("refresh");
    setError(null);
    try {
      const run = await mediaGenerationApi.refreshRun(
        runSnapshot.id,
        newIdempotencyKey(),
      );
      if (requestId !== runRequestRef.current || selectedRunId !== runSnapshot.id) return;
      const nextRun = safeRun(run);
      setRuns((current) => current.map((item) => (item.id === nextRun.id ? nextRun : item)));
      setSelectedRunId(nextRun.id);
    } catch (nextError) {
      if (requestId === runRequestRef.current) setError(nextError);
    } finally {
      if (requestId === runRequestRef.current) setBusy(null);
    }
  };

  const reconcilePlan = async () => {
    if (!selectedPlan) return;
    const planSnapshot = selectedPlan;
    const requestId = ++reconcileRequestRef.current;
    const metered = Boolean(planSnapshot.request_spec?.accept_metered_generation);
    if (metered && !acknowledgeMeteredGeneration) {
      setError(
        new Error(
          "Metered generationのreconcileにも人間の明示承認が必要です",
        ),
      );
      return;
    }
    const expectedPlanHash = planSnapshot.plan_hash?.trim() || "";
    const expectedIntentRequestHash = submitReceipt?.requestHash?.trim() || "";
    if (
      !/^[0-9a-f]{64}$/iu.test(expectedPlanHash) ||
      !/^[0-9a-f]{64}$/iu.test(expectedIntentRequestHash)
    ) {
      // The route intentionally requires both hashes. Never send a malformed
      // reconcile request when an old/partial receipt omitted one of them.
      setError(new Error("reconcileにはPlan hashとintent request hashの再取得が必要です"));
      return;
    }
    setBusy("reconcile");
    setError(null);
    try {
      const reconcileInput: GenerationReconcileInput = {
        expected_plan_hash: expectedPlanHash,
        expected_intent_request_hash: expectedIntentRequestHash,
        acknowledge_metered_generation: metered && acknowledgeMeteredGeneration,
      };
      const receipt = await mediaGenerationApi.reconcilePlan(
        planSnapshot.id,
        reconcileInput,
        newIdempotencyKey(),
      );
      if (requestId !== reconcileRequestRef.current || selectedPlanId !== planSnapshot.id) {
        return;
      }
      const safeOutputs = (receipt.outputs ?? [])
        .map(safeOutput)
        .filter((item): item is GenerationOutput => item !== null);
      const run = receipt.run
        ? {
            ...safeRun(receipt.run),
            outputs: safeOutputs.length ? safeOutputs : safeRun(receipt.run).outputs ?? [],
          }
        : null;
      runRequestRef.current += 1;
      if (run?.id) {
        setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
        setSelectedRunId(run.id);
      }
      setSubmitReceipt({
        planId: planSnapshot.id,
        status: receipt.status,
        requestHash: receipt.intent?.request_hash ?? submitReceipt?.requestHash ?? null,
      });
      const nextPlanStatus = receipt.plan?.status ?? receipt.status;
      setPlans((current) =>
        current.map((item) =>
          item.id === planSnapshot.id
            ? receipt.plan?.id === planSnapshot.id
              ? receipt.plan
              : { ...item, status: nextPlanStatus }
            : item,
        ),
      );
    } catch (nextError) {
      if (requestId === reconcileRequestRef.current) setError(nextError);
    } finally {
      if (requestId === reconcileRequestRef.current) setBusy(null);
    }
  };

  const selectOutput = async (output: GenerationOutput) => {
    if (!selectedRun) return;
    const runSnapshot = selectedRun;
    const requestId = ++selectRequestRef.current;
    setBusy("select");
    setError(null);
    try {
      await mediaGenerationApi.selectOutput(
        runSnapshot.id,
        output.id,
        newIdempotencyKey(),
      );
      if (requestId !== selectRequestRef.current || selectedRunId !== runSnapshot.id) return;
      setSelectedOutputId(output.id);
    } catch (nextError) {
      if (requestId === selectRequestRef.current) setError(nextError);
    } finally {
      if (requestId === selectRequestRef.current) setBusy(null);
    }
  };

  return (
    <div className="space-y-4" data-testid="operations-media-generation-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">Creative / Generation</h2>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            Personaのexact revisionとGeneration Studio bindingを固定して、画像・動画Generation Plan・Run・Outputの監査可能な参照だけを扱います。
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void loadBindings()}
          disabled={loading}
        >
          <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
          Bindingを更新
        </Button>
      </div>

      <ErrorNotice error={error} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(20rem,28rem)]">
        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-sm">Generation binding</CardTitle>
            <CardDescription>
              設定済みWorkspaceとCreative Recipe revisionを選択します。認証情報やprovider内部値は入力・表示しません。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 pt-1">
            <div className="space-y-1.5">
              <FieldLabel htmlFor="generation-workspace-select">Generation Workspace</FieldLabel>
              <AppSelect
                id="generation-workspace-select"
                aria-label="Generation Workspace"
                value={selectedWorkspaceId}
                onChange={(event) => setSelectedWorkspaceId(event.target.value)}
                placeholder="Workspaceを選択"
              >
                <option value="">Workspaceを選択</option>
                {workspaces.map((workspace) => (
                  <option key={workspace.id} value={workspace.id}>
                    {workspace.status === "unavailable" ? "利用不可" : "設定済み"} · {opaqueLabel(workspace.external_workspace_id, "wsp_")}
                  </option>
                ))}
              </AppSelect>
              {selectedWorkspace ? (
                <p className="text-[11px] text-muted-foreground">
                  binding: <code>{opaqueLabel(selectedWorkspace.external_workspace_id, "wsp_")}</code>
                  {selectedWorkspace.external_project_id
                    ? ` · ${opaqueLabel(selectedWorkspace.external_project_id, "prj_")}`
                    : ""}
                  {" · "}
                  <StatusPill status={selectedWorkspace.status} />
                </p>
              ) : null}
            </div>

            <div className="space-y-1.5">
              <FieldLabel htmlFor="generation-recipe-select">Creative Recipe</FieldLabel>
              <AppSelect
                id="generation-recipe-select"
                aria-label="Creative Recipe"
                value={selectedRecipeId}
                onChange={(event) => setSelectedRecipeId(event.target.value)}
                placeholder="Recipeを選択"
              >
                <option value="">Recipeを選択</option>
                {recipes.map((recipe) => (
                  <option key={recipe.id} value={recipe.id}>
                    {recipe.current_revision?.recipe_type ?? "image"} · v{recipe.current_revision?.version ?? "—"}
                  </option>
                ))}
              </AppSelect>
              {selectedRevision ? (
                <p className="text-[11px] text-muted-foreground">
                  <span className="mr-2 inline-flex items-center gap-1 rounded border border-border/70 px-1.5 py-0.5 font-medium" data-generation-kind={generationKind}>
                    {generationKind === "video" ? <Film className="size-3" /> : <ImageIcon className="size-3" />}
                    {generationKind}
                  </span>
                  exact revision: v{selectedRevision.version}
                  {selectedRevision.content_hash
                    ? ` · ${selectedRevision.content_hash.slice(0, 12)}…`
                    : ""}
                  {selectedRevision.persona_revision_id
                    ? ` · Persona revision ${selectedRevision.persona_revision_id}`
                    : ""}
                </p>
              ) : null}
            </div>

            <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11px] leading-4 text-amber-800 dark:text-amber-200">
              Studioが利用できない場合はunavailableとして表示します。ブラウザから外部ポートへ直接接続せず、サーバー側のsafe adapterだけを利用します。
            </div>
          </CardContent>
        </Card>

        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-sm">Live {generationKind} catalog</CardTitle>
            <CardDescription>
              保存しない読み取り専用catalog。利用できない・未対応の場合はfail-closedで状態だけを表示します。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 pt-1">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void fetchCatalog()}
              disabled={catalogLoading || !selectedWorkspaceId}
            >
              {catalogLoading ? <Loader2 className="size-3.5 animate-spin" /> : generationKind === "video" ? <Film className="size-3.5" /> : <ImageIcon className="size-3.5" />}
              {generationKind} catalogを取得
            </Button>
            <ErrorNotice error={catalogError} />
            {selectedWorkspace?.status === "unavailable" ? <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-2.5 py-2 text-xs text-amber-800 dark:text-amber-200" data-generation-catalog-state="unavailable">Generation Studio catalogは利用不可です。model selectionを推測して送信しません。</p> : null}
            {catalogError ? <p className="text-[11px] text-muted-foreground" data-generation-catalog-state="unavailable">catalogはunavailable/unsupportedとして扱われます。</p> : null}
            {catalog.length ? (
              <div className="space-y-1.5">
                {catalog.map((item) => (
                  <div key={`${item.kind}:${item.model_selection_id}`} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border/70 px-3 py-2 text-xs">
                    <div><div className="font-mono">{item.model_selection_id}</div>
                    <p className="mt-1 text-[11px] text-muted-foreground">{item.display_name || `${generationKind === "video" ? "Video" : "Image"} model selection`} · {statusLabel(item.status)}</p></div>
                    <Button type="button" variant="ghost" size="sm" disabled={!(["available", "configured", "verified"] as string[]).includes(item.status ?? "")} onClick={() => setModelSelectionId(item.model_selection_id)}>選択</Button>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState>catalogは未取得です。</EmptyState>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(19rem,25rem)]">
        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-sm">{generationKind} GenerationPlanを作成</CardTitle>
            <CardDescription>選択中のWorkspaceとRecipe current revisionをPlanに固定します。kindはRecipeから決まり、UIで偽装できません。</CardDescription>
          </CardHeader>
          <CardContent className="pt-1">
            <form className="space-y-3" onSubmit={createPlan} aria-label="GenerationPlan form">
              <div className="space-y-1.5">
                <FieldLabel htmlFor="generation-prompt">Prompt</FieldLabel>
                <Textarea id="generation-prompt" aria-label="GenerationPlan prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder={generationKind === "video" ? "生成したい動画の内容" : "生成したい画像の内容"} rows={3} required />
              </div>
              <div className="space-y-1.5">
                <FieldLabel htmlFor="generation-negative-prompt">Negative prompt（任意）</FieldLabel>
                <Textarea id="generation-negative-prompt" aria-label="GenerationPlan negative prompt" value={negativePrompt} onChange={(event) => setNegativePrompt(event.target.value)} placeholder="避けたい要素" rows={2} />
              </div>
              <div className="space-y-1.5">
                <FieldLabel htmlFor="generation-persona-revision">Persona revision ID</FieldLabel>
                <Input id="generation-persona-revision" aria-label="Persona revision ID" value={personaRevisionId || selectedRevision?.persona_revision_id || ""} onChange={(event) => setPersonaRevisionId(event.target.value)} placeholder="Persona revision UUID" required={!selectedRevision?.persona_revision_id} />
                <p className="text-[10px] text-muted-foreground">RecipeにPersona revisionが含まれない場合は、ここで明示的に固定します。</p>
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                <div className="space-y-1.5"><FieldLabel htmlFor="generation-model-selection">{generationKind === "video" ? "Video model selection ID" : "Image model selection ID"}</FieldLabel><Input id="generation-model-selection" aria-label={`${generationKind === "video" ? "Video" : "Image"} model selection ID`} value={modelSelectionId || selectedRevision?.model_selection_id || selectedRevision?.image_model_selection_id || ""} onChange={(event) => setModelSelectionId(event.target.value)} placeholder={generationKind === "video" ? "wsl_…" : "ims_…"} required /></div>
                <div className="space-y-1.5"><FieldLabel htmlFor="generation-seed">Seed（任意）</FieldLabel><Input id="generation-seed" aria-label="Generation seed" inputMode="numeric" value={seed} onChange={(event) => setSeed(event.target.value)} placeholder="random" /></div>
              </div>
              {generationKind === "video" ? (
                <div className="space-y-2 rounded-md border border-border/70 bg-muted/20 p-3" data-generation-video-fields>
                  <div className="flex items-center gap-1.5 text-xs font-medium"><Film className="size-3.5" /> Video request</div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <div className="space-y-1.5"><FieldLabel htmlFor="generation-duration-seconds">Video duration seconds（任意）</FieldLabel><Input id="generation-duration-seconds" aria-label="Video duration seconds" type="number" min={1} max={86400} value={durationSeconds} onChange={(event) => setDurationSeconds(event.target.value)} placeholder="5" /></div>
                    <div className="space-y-1.5"><FieldLabel htmlFor="generation-frame-count">Frame count（任意）</FieldLabel><Input id="generation-frame-count" aria-label="Video frame count" type="number" min={1} max={100000} value={frameCount} onChange={(event) => setFrameCount(event.target.value)} placeholder="24" /></div>
                  </div>
                  <div className="space-y-1.5"><FieldLabel htmlFor="generation-storyboard">Storyboard（1行1ショット・任意）</FieldLabel><Textarea id="generation-storyboard" aria-label="Video storyboard" value={storyboard} onChange={(event) => setStoryboard(event.target.value)} placeholder="Shot 1: …\nShot 2: …" rows={3} /></div>
                </div>
              ) : null}
              {generationKind === "image" ? <>
                <div className="grid gap-2 sm:grid-cols-3">
                  <div className="space-y-1.5"><FieldLabel htmlFor="generation-size-preset">Size preset</FieldLabel><AppSelect id="generation-size-preset" aria-label="Size preset" value={sizePresetId} onChange={(event) => setSizePresetId(event.target.value)}><option value="normal_square">normal square</option><option value="normal_landscape">normal landscape</option><option value="normal_portrait">normal portrait</option><option value="custom">custom</option></AppSelect></div>
                  <div className="space-y-1.5"><FieldLabel htmlFor="generation-width">Width（64〜2048 / 64の倍数）</FieldLabel><Input id="generation-width" aria-label="Generation width" inputMode="numeric" value={width} onChange={(event) => setWidth(event.target.value)} placeholder="1024" /></div>
                  <div className="space-y-1.5"><FieldLabel htmlFor="generation-height">Height（64〜2048 / 64の倍数）</FieldLabel><Input id="generation-height" aria-label="Generation height" inputMode="numeric" value={height} onChange={(event) => setHeight(event.target.value)} placeholder="1024" /></div>
                </div>
                <div className="grid gap-2 sm:grid-cols-3">
                  <div className="space-y-1.5"><FieldLabel htmlFor="generation-aspect-ratio">Aspect ratio（任意）</FieldLabel><Input id="generation-aspect-ratio" aria-label="Generation aspect ratio" value={aspectRatio} onChange={(event) => setAspectRatio(event.target.value)} placeholder="1:1" /></div>
                </div>
              </> : null}
              <div className="grid gap-2 sm:grid-cols-3">
                <div className="space-y-1.5"><FieldLabel htmlFor="generation-output-count">Requested outputs</FieldLabel><Input id="generation-output-count" aria-label="Requested outputs" type="number" min={1} max={20} value={requestedOutputs} onChange={(event) => setRequestedOutputs(event.target.value)} /></div>
                <label className="flex items-end gap-2 pb-1.5 text-xs"><Checkbox checked={acceptMeteredGeneration} onCheckedChange={(checked) => setAcceptMeteredGeneration(checked === true)} aria-label="Metered generationを許可" /> Metered generationを許可（Plan hint）</label>
              </div>
              <Button type="submit" size="sm" disabled={busy !== null || loading || !selectedWorkspaceId || !selectedRecipeId}>
                {busy === "plan" ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
                GenerationPlanを作成
              </Button>
            </form>
          </CardContent>
        </Card>

        <Card size="sm">
          <CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Plans</CardTitle><CardDescription>{plans.length}件 · immutable request</CardDescription></CardHeader>
          <CardContent className="space-y-1.5 pt-1">
            {plans.length ? plans.map((plan) => (
              <button key={plan.id} type="button" className={`group relative w-full rounded-md border-l-2 px-3 py-2.5 text-left transition-colors ${selectedPlanId === plan.id ? "border-primary bg-primary/5" : "border-transparent hover:border-border hover:bg-muted/40"}`} onClick={() => setSelectedPlanId(plan.id)} aria-current={selectedPlanId === plan.id ? "page" : undefined}>
                <div className="flex items-center justify-between gap-2"><span className="truncate font-mono text-xs">{plan.id}</span><StatusPill status={plan.status} /></div>
                <p className="mt-1 text-[10px] text-muted-foreground">{normalizeGenerationKind(plan.generation_kind ?? plan.request_spec?.generation_kind, generationKind)} · outputs {plan.requested_outputs} · recipe revision {plan.creative_recipe_revision_id}</p>
              </button>
            )) : <EmptyState>GenerationPlanはまだありません。</EmptyState>}
          </CardContent>
        </Card>
      </div>

      <Card size="sm">
        <CardHeader className="border-b border-border/70">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div><CardTitle className="text-sm">Run / Output</CardTitle><CardDescription>送信は選択中Planのexact revisionに対して一度だけ行い、不確実な結果を自動再送しません。既知Runはrefresh、uncertain intentは明示reconcileだけを使います。</CardDescription></div>
            <div className="flex flex-wrap items-center justify-end gap-1.5">
              {selectedPlan && Boolean(selectedPlan.request_spec?.accept_metered_generation) ? (
                <label className="flex items-center gap-1.5 rounded border border-amber-500/40 bg-amber-500/5 px-2 py-1 text-[11px] text-amber-800 dark:text-amber-200"><Checkbox checked={acknowledgeMeteredGeneration} onCheckedChange={(checked) => setAcknowledgeMeteredGeneration(checked === true)} aria-label="Generation Studio paid generationを承認" /> 有料生成を承認</label>
              ) : null}
              <Button type="button" size="sm" onClick={() => void submitPlan()} disabled={!selectedPlan || busy !== null || selectedPlan.status !== "draft" || (Boolean(selectedPlan.request_spec?.accept_metered_generation) && !acknowledgeMeteredGeneration)}><Send className="size-3.5" /> {busy === "submit" ? "送信中…" : "このPlanをsubmit"}</Button>
              <Button type="button" size="sm" variant="outline" onClick={() => void reconcilePlan()} disabled={!selectedPlan || busy !== null || (submitReceipt?.planId !== selectedPlan?.id && selectedPlan?.status !== "uncertain") || (submitReceipt?.status !== "uncertain" && selectedPlan?.status !== "uncertain")}><RefreshCw className={busy === "reconcile" ? "size-3.5 animate-spin" : "size-3.5"} /> {busy === "reconcile" ? "照合中…" : "uncertainをreconcile"}</Button>
              <Button type="button" size="sm" variant="outline" onClick={() => void refreshRun()} disabled={!selectedRun || busy !== null}><RefreshCw className={busy === "refresh" ? "size-3.5 animate-spin" : "size-3.5"} /> refresh</Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-3 pt-1">
          {selectedPlan ? <div className="rounded-md border border-border/70 bg-muted/20 px-3 py-2 text-xs"><div className="flex flex-wrap items-center gap-2"><span className="font-mono">Plan {selectedPlan.id}</span><StatusPill status={selectedPlan.status} /><span className="rounded border border-border/70 px-1.5 py-0.5" data-generation-kind={normalizeGenerationKind(selectedPlan.generation_kind ?? selectedPlan.request_spec?.generation_kind, generationKind)}>{normalizeGenerationKind(selectedPlan.generation_kind ?? selectedPlan.request_spec?.generation_kind, generationKind)}</span></div><p className="mt-1 text-[11px] text-muted-foreground">Persona revision {selectedPlan.persona_revision_id} · Recipe revision {selectedPlan.creative_recipe_revision_id} · Workspace {opaqueLabel(selectedWorkspace?.external_workspace_id, "wsp_")}</p></div> : <EmptyState>左側でPlanを選択してください。</EmptyState>}
          {submitReceipt && submitReceipt.planId === selectedPlan?.id && !selectedRun ? <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs" data-generation-submit-receipt={submitReceipt.status}><div className="flex flex-wrap items-center gap-2"><AlertTriangle className="size-3.5" /><span className="font-medium">Submit receipt</span><StatusPill status={submitReceipt.status} /></div><p className="mt-1 text-[11px] text-muted-foreground">{submitReceipt.status === "uncertain" ? "結果が不確実です。再送せず、上のuncertainをreconcileで同じintentを照合してください。" : "Runは作成されませんでした。Generation Studioの利用可能状態を確認してから、同じPlanを再送せずに状態を照合してください。"}</p></div> : null}
          {runs.length ? <div className="space-y-1.5"><p className="text-xs font-medium">Runs</p>{runs.map((run) => <button key={run.id} type="button" className={`flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-left ${selectedRunId === run.id ? "border-primary bg-primary/5" : "border-border/70 hover:bg-muted/40"}`} onClick={() => setSelectedRunId(run.id)}><span className="font-mono text-xs">{opaqueLabel(run.external_run_id, "run_")}</span><StatusPill status={run.status} /></button>)}</div> : null}
          {selectedRun ? <div className="space-y-2 rounded-md border border-border/70 p-3"><div className="flex flex-wrap items-center justify-between gap-2"><div><p className="text-xs font-medium">Run receipt</p><p className="mt-1 font-mono text-xs">{opaqueLabel(selectedRun.external_run_id, "run_")}</p></div><StatusPill status={selectedRun.status} /></div>{selectedRun.status === "unavailable" ? <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-2.5 py-2 text-xs text-amber-800 dark:text-amber-200">Generation Studio adapterは利用できません。Runはunavailableとして保存され、再送は行いません。</p> : null}{selectedRun.status === "uncertain" ? <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-2.5 py-2 text-xs text-amber-800 dark:text-amber-200">Runの結果が不確実です。refreshは状態照会のみで、再送は行いません。</p> : null}{selectedRun.error_code ? <p className="text-xs text-destructive">error: {selectedRun.error_code}</p> : null}{selectedRun.outputs?.length ? <div className="space-y-1.5"><p className="text-xs font-medium">Outputs（人間が最終選択）</p>{selectedRun.outputs.map((output) => <OutputRow key={output.id} output={output} selected={selectedOutputId === output.id} selecting={busy === "select"} onSelect={() => void selectOutput(output)} />)}</div> : <EmptyState>Outputはまだありません。</EmptyState>}</div> : null}
        </CardContent>
      </Card>
    </div>
  );
}
