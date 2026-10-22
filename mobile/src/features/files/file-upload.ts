import type { FilesEntry, FilesSource } from "../../lib/files-types";

export const FILES_DOCUMENT_PICKER_OPTIONS = {
  copyToCacheDirectory: true,
  multiple: true,
} as const;

export type FilesPickedAsset = {
  uri: string;
  name?: string | null;
  mimeType?: string | null;
};

export type FilesUploadAsset = {
  uri: string;
  name: string;
  mimeType?: string;
};

export type FilesUploadRequestOptions = {
  overwrite?: boolean;
};

export type FilesUploadApi = (
  source: FilesSource,
  parentPath: string,
  file: FilesUploadAsset,
  options?: FilesUploadRequestOptions,
) => Promise<unknown>;

export type FilesUploadFailure = {
  index: number;
  name: string;
  message: string;
};

export type FilesUploadSuccess = {
  index: number;
  name: string;
};

export type FilesUploadSkipped = {
  index: number;
  name: string;
};

export type FilesUploadConflict = {
  index: number;
  name: string;
};

export type FilesUploadConflictDecision = "overwrite" | "skip";

export type FilesBatchUploadOptions = {
  /** Resolve all collected collisions once. A false/skip result is skip. */
  confirmConflicts?: (
    conflicts: readonly FilesUploadConflict[],
  ) => Promise<FilesUploadConflictDecision | boolean>;
  /** Return false when the captured auth/location/destination is no longer current. */
  isCurrent?: () => boolean;
  /** Used by Local clipboard uploads to retain validation by input index. */
  validateAsset?: (
    asset: FilesUploadAsset,
    index: number,
  ) => string | undefined;
};

export type FilesBatchUploadResult = {
  totalCount: number;
  successCount: number;
  failureCount: number;
  skippedCount: number;
  successes: FilesUploadSuccess[];
  failures: FilesUploadFailure[];
  skipped: FilesUploadSkipped[];
};

type FilesUploadOne = (
  asset: FilesUploadAsset,
  options?: FilesUploadRequestOptions,
) => Promise<unknown>;

type FilesUploadOutcome =
  | { kind: "success"; index: number; name: string }
  | { kind: "failure"; index: number; name: string; message: string }
  | { kind: "skipped"; index: number; name: string };

function uploadErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "アップロードに失敗しました";
}

function isFilesUploadCollisionError(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error as Error & { code?: unknown; status?: unknown }).code ===
      "already_exists" &&
    (error as Error & { status?: unknown }).status === 409
  );
}

function resultFromOutcomes(
  totalCount: number,
  outcomes: Array<FilesUploadOutcome | undefined>,
): FilesBatchUploadResult {
  const successes: FilesUploadSuccess[] = [];
  const failures: FilesUploadFailure[] = [];
  const skipped: FilesUploadSkipped[] = [];

  for (const outcome of outcomes) {
    if (!outcome) continue;
    if (outcome.kind === "success") {
      successes.push({ index: outcome.index, name: outcome.name });
    } else if (outcome.kind === "failure") {
      failures.push({
        index: outcome.index,
        name: outcome.name,
        message: outcome.message,
      });
    } else {
      skipped.push({ index: outcome.index, name: outcome.name });
    }
  }

  successes.sort((a, b) => a.index - b.index);
  failures.sort((a, b) => a.index - b.index);
  skipped.sort((a, b) => a.index - b.index);
  return {
    totalCount,
    successCount: successes.length,
    failureCount: failures.length,
    skippedCount: skipped.length,
    successes,
    failures,
    skipped,
  };
}

/**
 * Upload every asset selected by the platform picker.
 *
 * The first pass is sequential. Only the exact typed collision error is held
 * for one confirmation; all other errors become failures immediately. A
 * retry is sent only for the colliding input indexes and never opens another
 * confirmation if the overwrite request fails.
 */
export async function uploadPickedFiles(
  assets: readonly FilesPickedAsset[],
  uploadOne: FilesUploadOne,
  options: FilesBatchUploadOptions = {},
): Promise<FilesBatchUploadResult> {
  const outcomes: Array<FilesUploadOutcome | undefined> = new Array(assets.length);
  const conflicts: Array<{
    index: number;
    asset: FilesUploadAsset;
    error: unknown;
  }> = [];

  const normalizedAssets = assets.map((asset, index) => ({
    uri: asset.uri,
    name: asset.name || `upload-${index + 1}`,
    mimeType: asset.mimeType || undefined,
  }));
  const markSkipped = (index: number) => {
    const asset = normalizedAssets[index];
    if (!asset || outcomes[index]) return;
    outcomes[index] = { kind: "skipped", index, name: asset.name };
  };
  const markRemainingSkipped = (startIndex: number) => {
    for (let index = startIndex; index < normalizedAssets.length; index += 1) {
      markSkipped(index);
    }
  };

  for (const [index, asset] of normalizedAssets.entries()) {
    if (options.isCurrent && !options.isCurrent()) {
      markRemainingSkipped(index);
      break;
    }
    const validationError = options.validateAsset?.(asset, index);
    if (validationError) {
      outcomes[index] = {
        kind: "failure",
        index,
        name: asset.name,
        message: validationError,
      };
      continue;
    }
    try {
      // Keep the initial callback shape compatible with the existing upload
      // adapters. The overwrite option is passed only on the retry pass.
      await uploadOne(asset);
      outcomes[index] = { kind: "success", index, name: asset.name };
    } catch (error) {
      if (isFilesUploadCollisionError(error)) {
        conflicts.push({ index, asset, error });
      } else {
        outcomes[index] = {
          kind: "failure",
          index,
          name: asset.name,
          message: uploadErrorMessage(error),
        };
      }
    }
  }

  if (conflicts.length > 0) {
    const canConfirm = Boolean(options.confirmConflicts);
    if (!canConfirm) {
      for (const { index, asset, error } of conflicts) {
        outcomes[index] = {
          kind: "failure",
          index,
          name: asset.name,
          message: uploadErrorMessage(error),
        };
      }
      return resultFromOutcomes(assets.length, outcomes);
    }
    let decision: FilesUploadConflictDecision = "skip";
    if (!options.isCurrent || options.isCurrent()) {
      try {
        const selected = await options.confirmConflicts!(
          conflicts.map(({ index, asset }) => ({ index, name: asset.name })),
        );
        decision = selected === true || selected === "overwrite" ? "overwrite" : "skip";
      } catch {
        // Dialog dismissal, unmount, and a stale scope all settle as skip.
        decision = "skip";
      }
    }

    if (
      decision === "overwrite" &&
      (!options.isCurrent || options.isCurrent())
    ) {
      for (const { index, asset } of conflicts) {
        if (options.isCurrent && !options.isCurrent()) {
          markSkipped(index);
          continue;
        }
        try {
          await uploadOne(asset, { overwrite: true });
          outcomes[index] = { kind: "success", index, name: asset.name };
        } catch (error) {
          // A retry failure is final for this batch. Do not collect it as a
          // new conflict and do not open a second dialog.
          outcomes[index] = {
            kind: "failure",
            index,
            name: asset.name,
            message: uploadErrorMessage(error),
          };
        }
      }
    } else {
      for (const { index } of conflicts) markSkipped(index);
    }
  }

  return resultFromOutcomes(assets.length, outcomes);
}

/**
 * Upload device-local clipboard files to a server directory without opening
 * DocumentPicker again. Invalid/non-file entries fail independently, while
 * valid files continue through the same sequential batch semantics as picker
 * uploads.
 */
export async function uploadLocalClipboardFiles(
  entries: readonly FilesEntry[],
  destinationPath: string,
  upload: FilesUploadApi,
  options: FilesBatchUploadOptions = {},
): Promise<FilesBatchUploadResult> {
  const validationErrors = entries.map((entry) => {
    if (entry.source !== "local") return "ローカルファイルではありません";
    if (entry.type !== "file") {
      return "フォルダーはサーバーへ貼り付けできません。ファイルを選択してください。";
    }
    if (!entry.path) return "ローカルファイルが見つかりません";
    return undefined;
  });
  const assets: FilesPickedAsset[] = entries.map((entry) => ({
    uri: entry.path || "invalid-local-clipboard://missing",
    name: entry.name,
    mimeType: entry.mimeType,
  }));

  return uploadPickedFiles(
    assets,
    async (asset, requestOptions) => {
      if (requestOptions?.overwrite === true) {
        await upload("server", destinationPath, asset, { overwrite: true });
      } else {
        await upload("server", destinationPath, asset);
      }
    },
    {
      ...options,
      validateAsset: (_asset, index) => validationErrors[index],
    },
  );
}

export type FilesBatchUploadAlert = {
  title: string;
  message: string;
};

export function createFilesBatchUploadAlert(
  result: FilesBatchUploadResult,
): FilesBatchUploadAlert | null {
  if (result.failureCount === 0 && result.skippedCount === 0) return null;
  const summary = [
    `成功: ${result.successCount}件`,
    `失敗: ${result.failureCount}件`,
    `スキップ: ${result.skippedCount}件`,
  ].join(" / ");
  const detailRows = [
    ...result.failures.map((failure) => ({
      index: failure.index,
      text: `${failure.name}: ${failure.message}`,
    })),
    ...result.skipped.map((entry) => ({
      index: entry.index,
      text: `${entry.name}: スキップ`,
    })),
  ].sort((left, right) => left.index - right.index);
  return {
    title:
      result.failureCount > 0
        ? result.successCount > 0
          ? "一部のアップロードに失敗しました"
          : "アップロードに失敗しました"
        : "アップロード結果",
    message: [
      summary,
      ...detailRows.map((row) => row.text),
    ].join("\n"),
  };
}
