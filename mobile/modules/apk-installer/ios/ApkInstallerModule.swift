import ExpoModulesCore

public class ApkInstallerModule: Module {
  public func definition() -> ModuleDefinition {
    Name("ApkInstaller")

    AsyncFunction("installApk") { (_: String) in
      throw NSError(domain: "ApkInstaller", code: 1, userInfo: [
        NSLocalizedDescriptionKey: "APK installation is only supported on Android."
      ])
    }
  }
}
