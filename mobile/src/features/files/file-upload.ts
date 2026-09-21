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

export type FilesUploadFailure = {
  name: string;
  message: string;
};

export type FilesBatchUploadResult = {
  successCount: number;
  failures: FilesUploadFailure[];
};

function uploadErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "アップロードに失敗しました";
}

/**
 * Upload every asset selected by the platform picker.
 *
 * Files are processed sequentially to avoid creating an unbounded number of
 * simultaneous multipart requests when a user selects many files. A failed
 * file is recorded without preventing the remaining selections from running.
 */
export async function uploadPickedFiles(
  assets: readonly FilesPickedAsset[],
  uploadOne: (asset: FilesUploadAsset) => Promise<unknown>,
): Promise<FilesBatchUploadResult> {
  const failures: FilesUploadFailure[] = [];
  let successCount = 0;

  for (const [index, asset] of assets.entries()) {
    const normalized: FilesUploadAsset = {
      uri: asset.uri,
      name: asset.name || `upload-${index + 1}`,
      mimeType: asset.mimeType || undefined,
    };
    try {
      await uploadOne(normalized);
      successCount += 1;
    } catch (error) {
      failures.push({
        name: normalized.name,
        message: uploadErrorMessage(error),
      });
    }
  }

  return { successCount, failures };
}
