import * as FileSystem from "expo-file-system/legacy";
import * as Sharing from "expo-sharing";
import { Platform } from "react-native";
import FileExporter from "../../modules/file-exporter";
import type { FilesEntry } from "./files-types";

export type FilesDownloadResult =
  | { status: "cancelled" }
  | { status: "saved"; uri: string }
  | { status: "shared" };

type ResolveDownloadUri = () => Promise<string>;
type AssertCurrent = () => Promise<void>;

async function assertDownloadCurrent(
  signal?: AbortSignal,
  assertCurrent?: AssertCurrent,
): Promise<void> {
  const assertNotAborted = () => {
    if (!signal?.aborted) return;
    const error = new Error("ファイル取得を中止しました");
    error.name = "AbortError";
    throw error;
  };
  assertNotAborted();
  await assertCurrent?.();
  assertNotAborted();
}

async function deleteSafDestinationQuietly(uri: string): Promise<void> {
  try {
    await FileSystem.StorageAccessFramework.deleteAsync(uri, {
      idempotent: true,
    });
  } catch {
    // Preserve the abort/copy error if SAF cleanup also fails.
  }
}

export type FilesDownloadProgress = {
  operation: "picker" | "receive" | "create" | "copy" | "share";
  state: "started" | "progress" | "completed";
  receivedBytes?: number;
  totalBytes?: number;
  percent?: number;
};

type ReportProgress = (event: FilesDownloadProgress) => void;

function reportProgress(
  callback: ReportProgress | undefined,
  event: FilesDownloadProgress,
): void {
  try {
    callback?.(event);
  } catch {
    // Progress reporting must never change the native file operation result.
  }
}

export function getUniqueDownloadName(
  requestedName: string,
  existingNames: readonly string[],
): string {
  const occupied = new Set(existingNames.map((name) => name.toLocaleLowerCase()));
  if (!occupied.has(requestedName.toLocaleLowerCase())) return requestedName;

  const extensionIndex = requestedName.lastIndexOf(".");
  const hasExtension = extensionIndex > 0;
  const stem = hasExtension
    ? requestedName.slice(0, extensionIndex)
    : requestedName;
  const extension = hasExtension ? requestedName.slice(extensionIndex) : "";
  let suffix = 1;
  let candidate = `${stem} (${suffix})${extension}`;
  while (occupied.has(candidate.toLocaleLowerCase())) {
    suffix += 1;
    candidate = `${stem} (${suffix})${extension}`;
  }
  return candidate;
}

/**
 * アプリ内またはサーバー上のファイルを、ユーザーがアクセスできる場所へ保存する。
 *
 * Android は Storage Access Framework で保存先フォルダーを選び、iOS などは
 * 「ファイルに保存」を含む共有シートへ渡す。ファイル本体の取得方法は呼び出し元から
 * 注入し、ローカルファイルとサーバーファイルで同じ保存処理を利用する。
 */
export async function downloadFileToDevice(
  entry: FilesEntry,
  resolveDownloadUri: ResolveDownloadUri,
  assertCurrent?: AssertCurrent,
  reportExternalProgress?: ReportProgress,
  signal?: AbortSignal,
): Promise<FilesDownloadResult> {
  if (entry.type !== "file") {
    throw new Error("フォルダーはダウンロードできません");
  }
  await assertDownloadCurrent(signal, assertCurrent);

  const mimeType = entry.mimeType || "application/octet-stream";

  if (Platform.OS === "android") {
    const initialDirectoryUri =
      FileSystem.StorageAccessFramework.getUriForDirectoryInRoot("Download");
    reportProgress(reportExternalProgress, {
      operation: "picker",
      state: "started",
    });
    await assertDownloadCurrent(signal, assertCurrent);
    const permission =
      await FileSystem.StorageAccessFramework.requestDirectoryPermissionsAsync(
        initialDirectoryUri,
      );
    await assertDownloadCurrent(signal, assertCurrent);
    if (!permission.granted) {
      return { status: "cancelled" };
    }
    reportProgress(reportExternalProgress, {
      operation: "picker",
      state: "completed",
    });

    const existingNames = await FileExporter.listDisplayNames(
      permission.directoryUri,
    );
    await assertDownloadCurrent(signal, assertCurrent);
    const destinationName = getUniqueDownloadName(entry.name, existingNames);
    const sourceUri = await resolveDownloadUri();
    await assertDownloadCurrent(signal, assertCurrent);
    reportProgress(reportExternalProgress, {
      operation: "create",
      state: "started",
    });
    await assertDownloadCurrent(signal, assertCurrent);
    let createdDestinationUri: string | null = null;
    try {
      const destinationUri =
        await FileSystem.StorageAccessFramework.createFileAsync(
          permission.directoryUri,
          // Android側にMIMEから拡張子を付与させると、text/plain扱いの
          // .md/.json/.py等が .txt に変わる。汎用MIMEと完全な名前を渡し、
          // 元のファイル名をそのまま保存する。
          destinationName,
          "application/octet-stream",
        );
      createdDestinationUri = destinationUri;
      reportProgress(reportExternalProgress, {
        operation: "create",
        state: "completed",
      });
      await assertDownloadCurrent(signal, assertCurrent);
      reportProgress(reportExternalProgress, {
        operation: "copy",
        state: "started",
      });
      await assertDownloadCurrent(signal, assertCurrent);
      await FileExporter.copyFileToContentUri(sourceUri, destinationUri);
      reportProgress(reportExternalProgress, {
        operation: "copy",
        state: "completed",
      });
      await assertDownloadCurrent(signal, assertCurrent);
      return { status: "saved", uri: destinationUri };
    } catch (error) {
      if (createdDestinationUri) {
        await deleteSafDestinationQuietly(createdDestinationUri);
      }
      throw error;
    }
  }

  const sharingAvailable = await Sharing.isAvailableAsync();
  await assertDownloadCurrent(signal, assertCurrent);
  if (!sharingAvailable) {
    throw new Error("この端末ではファイル保存を利用できません");
  }
  const sourceUri = await resolveDownloadUri();
  await assertDownloadCurrent(signal, assertCurrent);
  reportProgress(reportExternalProgress, {
    operation: "share",
    state: "started",
  });
  await assertDownloadCurrent(signal, assertCurrent);
  await Sharing.shareAsync(sourceUri, {
    dialogTitle: `${entry.name} を保存`,
    mimeType,
  });
  reportProgress(reportExternalProgress, {
    operation: "share",
    state: "completed",
  });
  await assertDownloadCurrent(signal, assertCurrent);
  return { status: "shared" };
}
