import { NativeModule, requireNativeModule } from 'expo';

declare class ApkInstallerModule extends NativeModule {
  installApk(url: string): Promise<void>;
}

export default requireNativeModule<ApkInstallerModule>('ApkInstaller');
