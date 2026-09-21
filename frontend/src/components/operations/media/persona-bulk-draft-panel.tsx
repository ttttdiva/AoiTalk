"use client";

import {
  useMemo,
  useState,
} from "react";
import {
  CheckCircle2,
  Loader2,
  RefreshCw,
  Upload,
} from "lucide-react";

import {
  mediaOperationsSetupApi,
  type PersonaBulkDraftPreview,
  type PersonaBulkDraftSlot,
} from "@/lib/media-operations-setup-api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";


function newIdempotencyKey(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }

  return `media-bulk-${Date.now()}-${Math.random()
    .toString(36)
    .slice(2)}`;
}

function readableError(
  error: unknown,
): string {
  if (
    error instanceof Error &&
    error.message
  ) {
    return error.message;
  }

  return "一括Persona draftの処理に失敗しました";
}

function parseSlots(
  value: string,
): PersonaBulkDraftSlot[] | { source: string; source_format: "auto" } {
  let parsed: unknown;

  try {
    parsed = JSON.parse(value);
  } catch {
    // The backend owns the conservative Markdown/YAML/free-form parser.  Do
    // not attempt to infer Persona fields in the browser or evaluate imported
    // prose; send the bounded source through for a typed preview instead.
    return { source: value, source_format: "auto" };
  }

  if (
    typeof parsed !== "object" ||
    parsed === null ||
    Array.isArray(parsed)
  ) {
    return { source: value, source_format: "auto" };
  }

  const slots = (
    parsed as {
      slots?: unknown;
    }
  ).slots;

  if (!Array.isArray(slots)) {
    return { source: value, source_format: "auto" };
  }

  return slots as PersonaBulkDraftSlot[];
}

export function PersonaBulkDraftPanel({
  onApplied,
}: {
  onApplied: () => void | Promise<void>;
}) {
  const [
    importJson,
    setImportJson,
  ] = useState("");
  const [
    correctionJson,
    setCorrectionJson,
  ] = useState("");
  const [
    preview,
    setPreview,
  ] =
    useState<PersonaBulkDraftPreview | null>(
      null,
    );
  const [
    importKey,
    setImportKey,
  ] = useState(
    newIdempotencyKey,
  );
  const [
    applyKey,
    setApplyKey,
  ] = useState(
    newIdempotencyKey,
  );
  const [
    busy,
    setBusy,
  ] = useState<
    "import" | "correct" | "apply" | null
  >(null);
  const [
    error,
    setError,
  ] = useState<unknown>(null);

  const inferredCount = useMemo(
    () =>
      preview?.issues.filter(
        (issue) =>
          issue.code ===
          "inferred_fact_requires_correction",
      ).length ?? 0,
    [preview],
  );

  const importDraft = async () => {
    setBusy("import");
    setError(null);

    try {
      const parsed = parseSlots(
        importJson,
      );

      const result =
        await mediaOperationsSetupApi.importPersonaBulkDraft(
          {
            ...(Array.isArray(parsed)
              ? { slots: parsed }
              : parsed),
            project_id: null,
            source_format: "auto",
          },
          importKey,
        );

      setPreview(result);
      setCorrectionJson(
        JSON.stringify(
          {
            slots:
              result.slots,
          },
          null,
          2,
        ),
      );
      setApplyKey(
        newIdempotencyKey(),
      );
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const correctDraft = async () => {
    if (!preview) {
      return;
    }

    setBusy("correct");
    setError(null);

    try {
      const parsed = parseSlots(
        correctionJson,
      );
      if (!Array.isArray(parsed)) {
        throw new Error("修正欄はpreviewされた9 slot JSONを入力してください");
      }

      const result =
        await mediaOperationsSetupApi.correctPersonaBulkDraft(
          preview.id,
          {
            expected_version:
              preview.version,
            slots: parsed,
          },
        );

      setPreview(result);
      setCorrectionJson(
        JSON.stringify(
          {
            slots:
              result.slots,
          },
          null,
          2,
        ),
      );
      setApplyKey(
        newIdempotencyKey(),
      );
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const applyDraft = async () => {
    if (!preview) {
      return;
    }

    setBusy("apply");
    setError(null);

    try {
      await mediaOperationsSetupApi.applyPersonaBulkDraft(
        preview.id,
        preview.version,
        applyKey,
      );

      const refreshed =
        await mediaOperationsSetupApi.getPersonaBulkDraft(
          preview.id,
        );

      setPreview(refreshed);
      await onApplied();
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card
      size="sm"
      data-testid="persona-bulk-draft-panel"
    >
      <CardHeader className="border-b border-border/70">
        <CardTitle className="text-sm">
          9人一括 draft
        </CardTitle>
          <CardDescription>
          JSON / YAML / Markdown / free-formの9 slot案をimportし、
          explicit / inferred / unknownを確認してから一括適用します。
          inferredは自動適用されません。
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4 pt-4">
        <div className="rounded-md border border-border/70 bg-muted/20 px-3 py-2 text-xs leading-5 text-muted-foreground">
          backendはPersona内容を推測・補完しません。
          unknownはvalue=null、inferredはevidence必須です。
          inferredが1件でも残っているdraftはApplyできません。
        </div>

        {!preview ? (
          <>
            <Textarea
          aria-label="9人Persona draft JSON"
              value={importJson}
              onChange={(event) => {
                setImportJson(
                  event.target.value,
                );
                setImportKey(
                  newIdempotencyKey(),
                );
              }}
              placeholder={'## Persona A\n名前: ...\n媒体: X, pixiv\n\n## Persona B\n名前: ...'}
              rows={14}
              className="font-mono text-xs"
            />

            <Button
              type="button"
              size="sm"
              onClick={() =>
                void importDraft()
              }
              disabled={
                busy !== null ||
                !importJson.trim()
              }
            >
              {busy === "import" ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Upload className="size-3.5" />
              )}
              Preview
            </Button>
          </>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="rounded-full border border-border px-2 py-0.5">
                version {preview.version}
              </span>
              <span className="rounded-full border border-border px-2 py-0.5">
                {preview.status}
              </span>
              <span
                className="rounded-full border border-border px-2 py-0.5"
                data-testid="bulk-draft-inferred-count"
              >
                inferred {inferredCount}
              </span>
              <span className="rounded-full border border-border px-2 py-0.5">
                issues {preview.issues.length}
              </span>
            </div>

            {preview.issues.length ? (
              <ul className="space-y-1.5">
                {preview.issues.map(
                  (
                    issue,
                    index,
                  ) => (
                    <li
                      key={`${issue.code}-${issue.slot}-${issue.field}-${index}`}
                      className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs"
                    >
                      SLOT {issue.slot} ·{" "}
                      {issue.field}:{" "}
                      {issue.message}
                    </li>
                  ),
                )}
              </ul>
            ) : (
              <div className="flex items-center gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs">
                <CheckCircle2 className="size-4" />
                Apply可能です。
              </div>
            )}

            <Textarea
              aria-label="Persona draft correction JSON"
              value={correctionJson}
              onChange={(event) =>
                setCorrectionJson(
                  event.target.value,
                )
              }
              rows={18}
              className="font-mono text-xs"
              disabled={
                preview.status ===
                "applied"
              }
            />

            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() =>
                  void correctDraft()
                }
                disabled={
                  busy !== null ||
                  preview.status ===
                    "applied"
                }
              >
                {busy === "correct" ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="size-3.5" />
                )}
                修正を再Preview
              </Button>

              <Button
                type="button"
                size="sm"
                onClick={() =>
                  void applyDraft()
                }
                disabled={
                  busy !== null ||
                  !preview.applyable ||
                  preview.status ===
                    "applied"
                }
              >
                {busy === "apply" ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <CheckCircle2 className="size-3.5" />
                )}
                Atomic Apply
              </Button>

              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => {
                  setPreview(null);
                  setCorrectionJson("");
                  setError(null);
                  setImportKey(
                    newIdempotencyKey(),
                  );
                }}
              >
                新しいdraft
              </Button>
            </div>
          </>
        )}

        {error ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive"
          >
            {readableError(error)}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
