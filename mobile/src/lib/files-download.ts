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
): Promise<FilesDownloadResult> {
  if (entry.type !== "file") {
    throw new Error("フォルダーはダウンロードできません");
  }

  const mimeType = entry.mimeType || "application/octet-stream";

  if (Platform.OS === "android") {
    const initialDirectoryUri =
      FileSystem.StorageAccessFramework.getUriForDirectoryInRoot("Download");
    const permission =
      await FileSystem.StorageAccessFramework.requestDirectoryPermissionsAsync(
        initialDirectoryUri,
      );
    if (!permission.granted) {
      return { status: "cancelled" };
    }

    const existingNames = await FileExporter.listDisplayNames(
      permission.directoryUri,
    );
    const destinationName = getUniqueDownloadName(entry.name, existingNames);
    const sourceUri = await resolveDownloadUri();
    const destinationUri =
      await FileSystem.StorageAccessFramework.createFileAsync(
        permission.directoryUri,
        // Android側にMIMEから拡張子を付与させると、text/plain扱いの
        // .md/.json/.py等が .txt に変わる。汎用MIMEと完全な名前を渡し、
        // 元のファイル名をそのまま保存する。
        destinationName,
        "application/octet-stream",
      );
    await FileExporter.copyFileToContentUri(sourceUri, destinationUri);
    return { status: "saved", uri: destinationUri };
  }

  if (!(await Sharing.isAvailableAsync())) {
    throw new Error("この端末ではファイル保存を利用できません");
  }
  const sourceUri = await resolveDownloadUri();
  await Sharing.shareAsync(sourceUri, {
    dialogTitle: `${entry.name} を保存`,
    mimeType,
  });
  return { status: "shared" };
}
