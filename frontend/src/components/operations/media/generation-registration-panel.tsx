"use client";

import { useLayoutEffect, useRef, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { AppSelect } from "@/components/ui/app-select";
import { mediaGenerationApi } from "@/lib/media-operations-generation-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import {
  confirmRegistrationScope,
  emptyRecipeDraft,
  parseRegistrationAttempt,
  recipeRegistration,
  recipeRegistrationMatches,
  registrationScopeKey,
  registrationStorageKey,
  workspaceRegistration,
  workspaceRegistrationMatches,
  type RecipeRegistrationDraft,
  type RegistrationAttempt,
} from "@/lib/media-generation-registration";

export function GenerationRegistrationPanel({
  scope,
  onSaved,
}: {
  scope?: PersonaWorkspaceScope;
  onSaved: () => Promise<void>;
}) {
  if (!scope)
    return (
      <p className="text-sm text-muted-foreground">
        Workspace・Recipeの登録は、
        <Link className="underline" href="/operations?tab=studio">
          人格Workspace
        </Link>
        で対象人格を選択して行います。
      </p>
    );
  return (
    <RegistrationForm
      key={registrationScopeKey(scope)}
      scope={scope}
      onSaved={onSaved}
    />
  );
}

function RegistrationForm({
  scope,
  onSaved,
}: {
  scope: PersonaWorkspaceScope;
  onSaved: () => Promise<void>;
}) {
  const [kind, setKind] = useState<"workspace" | "recipe" | null>(null);
  const [origin, setOrigin] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [studioProject, setStudioProject] = useState("");
  const [draft, setDraft] = useState(emptyRecipeDraft);
  const [attempt, setAttempt] = useState<RegistrationAttempt | null>(null);
  const [attempted, setAttempted] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState<"check" | "save" | "reload" | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [storageError, setStorageError] = useState(false);
  const lifetime = useRef({ mounted: false, generation: 0 });
  const pending = useRef(false);
  // The parent key remounts this form when the authority tuple changes.
  // A fresh object for the same tuple must not invalidate an active request.
  const initialScope = useRef(scope).current;
  useLayoutEffect(() => {
    const active = lifetime.current;
    active.mounted = true;
    try {
      const stored = parseRegistrationAttempt(
        sessionStorage.getItem(registrationStorageKey(initialScope)),
        initialScope,
      );
      if (stored) {
        setAttempt(stored);
        setAttempted(true);
        setKind(stored.kind);
        setError(
          "前回の登録結果を確認できていません。同じ内容・同じキーで再試行できます。",
        );
      }
    } catch (cause) {
      setStorageError(true);
      setError(
        cause instanceof Error
          ? cause.message
          : "保存中の登録内容を確認できません。",
      );
    }
    return () => {
      active.mounted = false;
      active.generation++;
    };
  }, [initialScope]);
  const run = async (
    action: "check" | "save" | "reload",
    work: (current: () => boolean) => Promise<void>,
  ) => {
    if (pending.current || storageError || !lifetime.current.mounted) return;
    const generation = ++lifetime.current.generation;
    const current = () =>
      lifetime.current.mounted && lifetime.current.generation === generation;
    pending.current = true;
    setBusy(action);
    setError("");
    setNotice("");
    try {
      await work(current);
    } catch (cause) {
      if (current())
        setError(
          cause instanceof Error ? cause.message : "登録を確認できません。",
        );
    } finally {
      if (current()) {
        pending.current = false;
        setBusy(null);
      }
    }
  };
  const field = <K extends keyof RecipeRegistrationDraft>(
    key: K,
    value: RecipeRegistrationDraft[K],
  ) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setError("");
  };
  const check = async (current: () => boolean) => {
    const body =
      kind === "workspace"
        ? workspaceRegistration(scope, origin, workspaceId, studioProject)
        : recipeRegistration(scope, draft);
    await confirmRegistrationScope(scope);
    if (!current()) return;
    const key = `media-registration:${crypto.randomUUID()}`;
    const value: RegistrationAttempt =
      kind === "workspace"
        ? {
            version: 1,
            scopeKey: registrationScopeKey(scope),
            kind: "workspace",
            body: body as Parameters<
              typeof mediaGenerationApi.createWorkspace
            >[0],
            key,
          }
        : {
            version: 1,
            scopeKey: registrationScopeKey(scope),
            kind: "recipe",
            body: body as Parameters<typeof mediaGenerationApi.createRecipe>[0],
            key,
          };
    setAttempt(value);
    setConfirmed(false);
  };
  const save = async (current: () => boolean) => {
    if (!attempt || !confirmed)
      throw new Error("登録内容と所属を確認してください。");
    const snapshot = attempt;
    await confirmRegistrationScope(scope);
    if (!current()) return;
    // Persist before sending. A lost response, close or remount must retain
    // the exact request and key; no fresh mutation is silently substituted.
    sessionStorage.setItem(
      registrationStorageKey(scope),
      JSON.stringify(snapshot),
    );
    setAttempted(true);
    if (snapshot.kind === "workspace") {
      const created = await mediaGenerationApi.createWorkspace(
        snapshot.body,
        snapshot.key,
      );
      if (!current()) return;
      if (!workspaceRegistrationMatches(created, scope, snapshot.body))
        throw new Error(
          "登録応答のWorkspace・所有者・Projectを照合できません。同じ内容で再試行してください。",
        );
      const readback = await mediaGenerationApi.getWorkspace(created.id);
      if (!current()) return;
      if (
        readback.id !== created.id ||
        !workspaceRegistrationMatches(readback, scope, snapshot.body) ||
        !created.config_hash ||
        readback.config_hash !== created.config_hash
      )
        throw new Error(
          "Workspaceの保存状態が登録応答と一致しません。同じ内容で再試行してください。",
        );
    } else {
      const created = await mediaGenerationApi.createRecipe(
        snapshot.body,
        snapshot.key,
      );
      if (!current()) return;
      if (!recipeRegistrationMatches(created, scope, snapshot.body))
        throw new Error(
          "登録応答のRecipe・人格版・所属を照合できません。同じ内容で再試行してください。",
        );
      const readback = await mediaGenerationApi.getRecipe(created.id);
      if (!current()) return;
      if (
        readback.id !== created.id ||
        !recipeRegistrationMatches(readback, scope, snapshot.body) ||
        readback.current_revision?.id !== created.current_revision?.id ||
        readback.current_revision?.content_hash !==
          created.current_revision?.content_hash
      )
        throw new Error(
          "Recipeの保存版が登録応答と一致しません。同じ内容で再試行してください。",
        );
    }
    await confirmRegistrationScope(scope);
    if (!current()) return;
    sessionStorage.removeItem(registrationStorageKey(scope));
    setAttempt(null);
    setAttempted(false);
    setConfirmed(false);
    setKind(null);
    await onSaved();
    if (current())
      setNotice(
        "登録と読み戻しを確認しました。一覧から使用するWorkspace・Recipeを明示選択してください。",
      );
  };
  const close = () => {
    if (busy === "save") return;
    lifetime.current.generation++;
    pending.current = false;
    setBusy(null);
    setKind(null);
    setConfirmed(false);
    setError("");
    // Cancel discards every unsent input. An attempted request is retained
    // only through its persisted snapshot, never through these fields.
    setOrigin("");
    setWorkspaceId("");
    setStudioProject("");
    setDraft(emptyRecipeDraft());
    if (!attempted) setAttempt(null);
  };
  const open = (value: "workspace" | "recipe") => {
    if (!attempted && kind !== value) {
      setAttempt(null);
      setConfirmed(false);
    }
    setKind(attempted && attempt ? attempt.kind : value);
    setError("");
    setNotice("");
  };
  const textFields: Array<[keyof RecipeRegistrationDraft, string, number]> = [
    ["modelId", "モデル選択ID", 164],
    ["workflowId", "workflow参照ID（任意）", 255],
    ["aspectRatio", "縦横比（任意）", 16],
    ["width", "幅px（任意）", 5],
    ["height", "高さpx（任意）", 5],
    ["identityPack", "identity pack参照ID（任意）", 255],
    ["promptSchema", "prompt schema参照ID（任意）", 255],
  ];
  return (
    <section
      aria-label="Generation設定を登録"
      className="min-w-0 space-y-3 rounded-xl border bg-card p-4"
    >
      <h3 className="inline-flex items-center gap-1 text-sm font-semibold">
        生成に使うWorkspace・Recipeを登録
        <HelpPopover label="生成に使うWorkspace・Recipeを登録">
          {scope.displayName}のProjectにWorkspaceを登録し、この人格の現在版にRecipeを固定します。登録後に一覧から選択できます。
        </HelpPopover>
      </h3>
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">登録する所属と人格版</summary>
        <p className="mt-1 break-all">
          Owner {scope.ownerUserId} · AoiTalk Project {scope.projectId} · 人格{" "}
          {scope.personaId} · 現在版 {scope.personaRevisionId}
        </p>
      </details>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={Boolean(busy) || storageError}
          onClick={() => open("workspace")}
        >
          Workspaceを登録
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={Boolean(busy) || storageError}
          onClick={() => open("recipe")}
        >
          Recipeを登録
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={Boolean(busy) || storageError}
          onClick={() =>
            void run("reload", async (current) => {
              await confirmRegistrationScope(scope);
              if (current()) await onSaved();
            })
          }
        >
          登録一覧を再読込
        </Button>
      </div>
      {attempted && !kind && (
        <p className="text-sm">
          結果未確認の登録があります。登録ボタンから同じ内容を再確認できます。
        </p>
      )}
      {notice && (
        <p role="status" className="text-sm">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="break-words text-sm text-destructive">
          {error}
        </p>
      )}
      {kind && (
        <div
          aria-label={
            kind === "workspace"
              ? "Workspace登録フォーム"
              : "Recipe登録フォーム"
          }
          className="space-y-3 border-t pt-3"
        >
          {!attempt ? (
            <>
              {kind === "workspace" ? (
                <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                  <label className="space-y-1 text-sm sm:col-span-2">
                    <span>Generation Studioの接続先origin</span>
                    <Input
                      aria-label="Generation Studioの接続先origin"
                      maxLength={2048}
                      value={origin}
                      disabled={Boolean(busy)}
                      onChange={(event) => setOrigin(event.target.value)}
                      placeholder="https://studio.example.invalid"
                    />
                  </label>
                  <label className="space-y-1 text-sm">
                    <span>73 Workspace ID</span>
                    <Input
                      aria-label="73 Workspace ID"
                      maxLength={164}
                      value={workspaceId}
                      disabled={Boolean(busy)}
                      onChange={(event) => setWorkspaceId(event.target.value)}
                      placeholder="wsp_から始まる参照ID"
                    />
                  </label>
                  <label className="space-y-1 text-sm">
                    <span>73 Project ID（任意）</span>
                    <Input
                      aria-label="73 Project ID（任意）"
                      maxLength={164}
                      value={studioProject}
                      disabled={Boolean(busy)}
                      onChange={(event) => setStudioProject(event.target.value)}
                      placeholder="prj_から始まる参照ID"
                    />
                  </label>
                </div>
              ) : (
                <>
                  <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                    <label className="space-y-1 text-sm">
                      <span>Recipe名</span>
                      <Input
                        aria-label="Recipe名"
                        maxLength={255}
                        value={draft.name}
                        disabled={Boolean(busy)}
                        onChange={(event) => field("name", event.target.value)}
                      />
                    </label>
                    <label className="space-y-1 text-sm">
                      <span>Recipeの制作形式</span>
                      <AppSelect
                        aria-label="Recipeの制作形式"
                        value={draft.recipeType}
                        disabled={Boolean(busy)}
                        onValueChange={(value) => field("recipeType", value)}
                      >
                        <option value="image">画像</option>
                        <option value="image_set">画像セット</option>
                        <option value="comic">漫画</option>
                        <option value="thumbnail">サムネイル</option>
                        <option value="video">動画</option>
                      </AppSelect>
                    </label>
                  </div>
                  <label className="block space-y-1 text-sm">
                    <span>Recipeのprompt template</span>
                    <Textarea
                      aria-label="Recipeのprompt template"
                      maxLength={8000}
                      value={draft.prompt}
                      disabled={Boolean(busy)}
                      onChange={(event) => field("prompt", event.target.value)}
                      placeholder="生成時に使うpromptの雛形を入力"
                    />
                  </label>
                  <div className="relative">
                    <details className="space-y-3" open>
                    <summary className="cursor-pointer pr-7 text-sm font-medium">
                      モデル・workflow・出力条件
                    </summary>
                    <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                      {textFields.map(([key, label, max]) => (
                        <label key={key} className="space-y-1 text-sm">
                          <span>{label}</span>
                          <Input
                            aria-label={label}
                            maxLength={max}
                            value={String(draft[key])}
                            disabled={Boolean(busy)}
                            onChange={(event) => field(key, event.target.value)}
                          />
                        </label>
                      ))}
                      <label className="space-y-1 text-sm">
                        <span>候補数</span>
                        <Input
                          aria-label="候補数"
                          type="number"
                          min={1}
                          max={20}
                          value={draft.candidateCount}
                          disabled={Boolean(busy)}
                          onChange={(event) =>
                            field("candidateCount", event.target.value)
                          }
                        />
                      </label>
                      {draft.recipeType === "video" && (
                        <>
                          {(
                            [
                              ["duration", "動画の秒数（任意）", 86400],
                              ["frames", "フレーム数（任意）", 100000],
                            ] as const
                          ).map(([key, label, max]) => (
                            <label key={key} className="space-y-1 text-sm">
                              <span>{label}</span>
                              <Input
                                aria-label={label}
                                type="number"
                                min={1}
                                max={max}
                                value={draft[key]}
                                disabled={Boolean(busy)}
                                onChange={(event) =>
                                  field(key, event.target.value)
                                }
                              />
                            </label>
                          ))}
                          <label className="space-y-1 text-sm sm:col-span-2">
                            <span>絵コンテ（1行1場面・任意）</span>
                            <Textarea
                              aria-label="絵コンテ（1行1場面・任意）"
                              value={draft.storyboard}
                              disabled={Boolean(busy)}
                              onChange={(event) =>
                                field("storyboard", event.target.value)
                              }
                            />
                          </label>
                        </>
                      )}
                    </div>
                    </details>
                    <span className="absolute right-0 top-0"><HelpPopover label="モデル・workflow・出力条件">確認した参照IDを入力してください。画像はims_・imd_・ien_、動画はwsl_のモデル選択IDを使います。73側の設定は自動採用しません。</HelpPopover></span>
                  </div>
                  <details className="space-y-3">
                    <summary className="cursor-pointer text-sm font-medium">
                      避ける内容・参考素材
                    </summary>
                    <label className="block space-y-1 text-sm">
                      <span>避ける内容（任意）</span>
                      <Textarea
                        aria-label="避ける内容（任意）"
                        maxLength={8000}
                        value={draft.negative}
                        disabled={Boolean(busy)}
                        onChange={(event) =>
                          field("negative", event.target.value)
                        }
                      />
                    </label>
                    <label className="block space-y-1 text-sm">
                      <span>参考素材の参照ID（20件まで・任意）</span>
                      <Textarea
                        aria-label="参考素材の参照ID（20件まで・任意）"
                        value={draft.references}
                        disabled={Boolean(busy)}
                        onChange={(event) =>
                          field("references", event.target.value)
                        }
                      />
                    </label>
                  </details>
                  <details className="space-y-2" open>
                    <summary className="cursor-pointer text-sm font-medium">
                      費用・人review・来歴の方針
                    </summary>
                    {(
                      [
                        [
                          "metered",
                          "従量課金モデルは実行時の本人確認を必須にする",
                        ],
                        ["humanReview", "人reviewを必要とする方針を保存"],
                        [
                          "retainProvenance",
                          "制作物の来歴を保持する方針を保存",
                        ],
                      ] as const
                    ).map(([key, label]) => (
                      <label
                        key={key}
                        className="flex items-start gap-2 text-sm"
                      >
                        <Checkbox
                          checked={draft[key]}
                          disabled={Boolean(busy)}
                          onCheckedChange={(value) => field(key, value)}
                        />
                        <span>{label}</span>
                      </label>
                    ))}
                  </details>
                </>
              )}
              <Button
                size="sm"
                disabled={Boolean(busy) || storageError}
                onClick={() => void run("check", check)}
              >
                登録内容を確認
              </Button>
            </>
          ) : (
            <div
              aria-label="Generation登録内容の確認"
              className="space-y-3 rounded-lg border p-3 text-sm"
            >
              <p className="break-all">
                {scope.displayName} · Owner {scope.ownerUserId} · AoiTalk
                Project {scope.projectId} · 人格 {scope.personaId} · 現在版{" "}
                {scope.personaRevisionId}
              </p>
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 text-xs">
                {JSON.stringify(attempt.body, null, 2)}
              </pre>
              <p className="inline-flex items-center gap-1 text-xs">
                登録操作の範囲
                <HelpPopover label="登録操作の範囲">この操作はAoiTalkの設定登録です。保存後もモデル実行・生成・公開はそれぞれ本人の操作から進めます。</HelpPopover>
              </p>
              <label className="flex items-start gap-2">
                <Checkbox
                  checked={confirmed}
                  disabled={Boolean(busy)}
                  onCheckedChange={setConfirmed}
                />
                <span>この所属・人格版と登録内容を確認しました</span>
              </label>
              <div className="flex flex-wrap gap-2">
                <Button
                  disabled={Boolean(busy) || !confirmed || storageError}
                  onClick={() => void run("save", save)}
                >
                  {attempted
                    ? "同じ内容で登録を再試行"
                    : kind === "workspace"
                      ? "確認したWorkspaceを保存"
                      : "確認したRecipeを保存"}
                </Button>
                {!attempted && (
                  <Button
                    variant="outline"
                    disabled={Boolean(busy)}
                    onClick={() => {
                      setAttempt(null);
                      setConfirmed(false);
                    }}
                  >
                    入力へ戻る
                  </Button>
                )}
              </div>
            </div>
          )}
          <Button
            variant="ghost"
            size="sm"
            disabled={busy === "save"}
            onClick={close}
          >
            {attempted ? "閉じる（再試行内容を保持）" : "登録を取り消す"}
          </Button>
        </div>
      )}
    </section>
  );
}
