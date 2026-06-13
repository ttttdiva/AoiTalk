package expo.modules.apkinstaller

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.io.File

class ApkInstallerModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("ApkInstaller")

    AsyncFunction("installApk") { url: String ->
      val context = appContext.reactContext ?: throw Exception("Context is null")
      try {
        val downloadDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
        val existingFile = File(downloadDir, "aoitalk-mobile-update.apk")
        if (existingFile.exists()) existingFile.delete()
      } catch (_: Exception) {
      }

      val request = DownloadManager.Request(Uri.parse(url))
        .setTitle("AoiTalk 更新")
        .setDescription("APKをダウンロードしています...")
        .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
        .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "aoitalk-mobile-update.apk")
        .setMimeType("application/vnd.android.package-archive")

      val dm = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
      dm.enqueue(request)
    }
  }
}
