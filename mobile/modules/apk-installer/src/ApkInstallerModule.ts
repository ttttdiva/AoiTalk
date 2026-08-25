import { requireNativeModule } from 'expo';

type ApkInstallerNativeModule = {
  installApk(url: string): Promise<void>;
};

// ネイティブモジュールがビルドに含まれていない場合、requireNativeModule は
// 同期 throw する。モジュール読み込み時・呼び出し時のクラッシュを避けるため、
// 呼び出し時に解決し、失敗は rejected Promise として返す。
export default {
  installApk(url: string): Promise<void> {
    let native: ApkInstallerNativeModule;
    try {
      native = requireNativeModule<ApkInstallerNativeModule>('ApkInstaller');
    } catch {
      return Promise.reject(
        new Error(
          'APKインストーラーがこのビルドに含まれていません。ブラウザから直接ダウンロードしてください。',
        ),
      );
    }
    return native.installApk(url);
  },
};
