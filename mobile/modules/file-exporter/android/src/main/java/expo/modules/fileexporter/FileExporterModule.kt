package expo.modules.fileexporter

import android.content.ContentResolver
import android.net.Uri
import android.provider.DocumentsContract
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.io.File
import java.io.FileInputStream

class FileExporterModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("FileExporter")

    AsyncFunction("listDisplayNames") { directoryUriString: String ->
      val context = appContext.reactContext ?: throw Exception("Context is null")
      val resolver = context.contentResolver
      val directoryUri = Uri.parse(directoryUriString)
      val treeDocumentId = DocumentsContract.getTreeDocumentId(directoryUri)
      val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(
        directoryUri,
        treeDocumentId
      )
      val names = mutableListOf<String>()
      resolver.query(
        childrenUri,
        arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME),
        null,
        null,
        null
      )?.use { cursor ->
        val nameIndex = cursor.getColumnIndex(
          DocumentsContract.Document.COLUMN_DISPLAY_NAME
        )
        while (cursor.moveToNext()) {
          if (nameIndex >= 0 && !cursor.isNull(nameIndex)) {
            names.add(cursor.getString(nameIndex))
          }
        }
      }
      names
    }

    AsyncFunction("copyFileToContentUri") { sourceUriString: String, destinationUriString: String ->
      val context = appContext.reactContext ?: throw Exception("Context is null")
      val resolver = context.contentResolver
      val sourceUri = Uri.parse(sourceUriString)
      val destinationUri = Uri.parse(destinationUriString)

      try {
        val input = if (sourceUri.scheme == ContentResolver.SCHEME_FILE) {
          val sourcePath = sourceUri.path ?: throw Exception("Source path is empty")
          FileInputStream(File(sourcePath))
        } else {
          resolver.openInputStream(sourceUri)
            ?: throw Exception("Source file could not be opened")
        }
        input.use { source ->
          val output = resolver.openOutputStream(destinationUri, "w")
            ?: throw Exception("Destination file could not be opened")
          output.use { destination ->
            source.copyTo(destination)
          }
        }
      } catch (error: Exception) {
        try {
          DocumentsContract.deleteDocument(resolver, destinationUri)
        } catch (_: Exception) {
        }
        throw Exception("ファイルの保存に失敗しました", error)
      }
    }
  }
}
