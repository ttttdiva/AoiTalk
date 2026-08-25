import { requireNativeModule } from "expo";

type FileExporterNativeModule = {
  listDisplayNames(directoryUri: string): Promise<string[]>;
  copyFileToContentUri(
    sourceUri: string,
    destinationUri: string,
  ): Promise<void>;
};

export default {
  listDisplayNames(directoryUri: string): Promise<string[]> {
    let native: FileExporterNativeModule;
    try {
      native = requireNativeModule<FileExporterNativeModule>("FileExporter");
    } catch {
      return Promise.reject(
        new Error("ファイル保存機能がこのビルドに含まれていません。"),
      );
    }
    return native.listDisplayNames(directoryUri);
  },

  copyFileToContentUri(
    sourceUri: string,
    destinationUri: string,
  ): Promise<void> {
    let native: FileExporterNativeModule;
    try {
      native = requireNativeModule<FileExporterNativeModule>("FileExporter");
    } catch {
      return Promise.reject(
        new Error("ファイル保存機能がこのビルドに含まれていません。"),
      );
    }
    return native.copyFileToContentUri(sourceUri, destinationUri);
  },
};
