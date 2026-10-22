package expo.modules.nativemultipart

import android.content.ContentResolver
import android.net.Uri
import android.provider.OpenableColumns
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import okhttp3.Call
import okhttp3.CookieJar
import okhttp3.Headers
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okio.BufferedSink
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream

private const val RESPONSE_LIMIT_BYTES = 1024 * 1024
private const val COPY_BUFFER_BYTES = 64 * 1024

private class UriRequestBody(
  private val resolver: ContentResolver,
  private val uri: Uri,
  private val mediaType: MediaType?,
  private val knownLength: Long,
) : RequestBody() {
  override fun contentType(): MediaType? = mediaType

  override fun contentLength(): Long = knownLength

  override fun isOneShot(): Boolean = true

  override fun writeTo(sink: BufferedSink) {
    val input = resolver.openInputStream(uri)
      ?: throw IllegalArgumentException("upload file could not be opened")
    input.use { source ->
      val buffer = ByteArray(COPY_BUFFER_BYTES)
      while (true) {
        val count = source.read(buffer)
        if (count < 0) break
        if (count > 0) sink.write(buffer, 0, count)
      }
    }
  }
}

private class OneShotRequestBody(
  private val delegate: RequestBody,
) : RequestBody() {
  override fun contentType(): MediaType? = delegate.contentType()

  override fun contentLength(): Long = delegate.contentLength()

  override fun isOneShot(): Boolean = true

  override fun writeTo(sink: BufferedSink) = delegate.writeTo(sink)
}

private enum class RequestState {
  PREPARED,
  ACTIVE,
  CANCELLED,
}

class NativeMultipartModule : Module() {
  private val registryLock = Any()
  private var destroyed = false
  private val activeCalls = mutableMapOf<String, Call>()
  private val requestStates = mutableMapOf<String, RequestState>()
  private val client by lazy {
    OkHttpClient.Builder()
      .followRedirects(false)
      .followSslRedirects(false)
      .retryOnConnectionFailure(false)
      .cookieJar(CookieJar.NO_COOKIES)
      .build()
  }

  override fun definition() = ModuleDefinition {
    Name("AoiTalkNativeMultipart")

    AsyncFunction("upload") {
        requestId: String,
        url: String,
        headersJson: String,
        fieldsJson: String,
        fileUri: String,
        fileName: String,
        mimeType: String,
      ->
      executeUpload(requestId, url, headersJson, fieldsJson, fileUri, fileName, mimeType)
    }

    // AsyncFunction is dispatched to AppContext.modulesQueue. Register the
    // request synchronously so cancel() can mark a queued upload before its
    // body starts executing on that queue.
    Function("prepare") { requestId: String ->
      synchronized(registryLock) {
        if (destroyed) throw java.util.concurrent.CancellationException("upload module was destroyed")
        if (requestId.isBlank() || requestStates.containsKey(requestId)) {
          throw IllegalArgumentException("upload request is already registered")
        }
        requestStates[requestId] = RequestState.PREPARED
      }
    }

    Function("cancel") { requestId: String ->
      val call = synchronized(registryLock) {
        if (requestStates.containsKey(requestId)) {
          requestStates[requestId] = RequestState.CANCELLED
        }
        activeCalls.remove(requestId)
      }
      call?.cancel()
    }

    Function("release") { requestId: String ->
      val call = synchronized(registryLock) {
        requestStates.remove(requestId)
        activeCalls.remove(requestId)
      }
      call?.cancel()
    }

    OnDestroy {
      val calls = synchronized(registryLock) {
        destroyed = true
        val pendingCalls = activeCalls.values.toList()
        activeCalls.clear()
        requestStates.clear()
        pendingCalls
      }
      // Cancellation must not hold the registry lock while OkHttp notifies
      // callbacks. A call registered before destroy is in this snapshot;
      // registration after destroy is rejected under the same lock.
      calls.forEach { it.cancel() }
    }
  }

  private fun executeUpload(
    requestId: String,
    url: String,
    headersJson: String,
    fieldsJson: String,
    fileUri: String,
    fileName: String,
    mimeType: String,
  ): String {
    beginRequest(requestId)
    try {
      return executeUploadPending(requestId, url, headersJson, fieldsJson, fileUri, fileName, mimeType)
    } finally {
      synchronized(registryLock) { requestStates.remove(requestId) }
    }
  }

  private fun beginRequest(requestId: String) {
    synchronized(registryLock) {
      if (destroyed || requestStates[requestId] != RequestState.PREPARED) {
        throw java.util.concurrent.CancellationException("upload request was cancelled")
      }
      requestStates[requestId] = RequestState.ACTIVE
    }
  }

  private fun executeUploadPending(
    requestId: String,
    url: String,
    headersJson: String,
    fieldsJson: String,
    fileUri: String,
    fileName: String,
    mimeType: String,
  ): String {
    val parsedUrl = url.toHttpUrlOrNull()
      ?: throw IllegalArgumentException("upload URL is invalid")
    if (parsedUrl.scheme != "http" && parsedUrl.scheme != "https") {
      throw IllegalArgumentException("upload URL must use HTTP(S)")
    }
    if (parsedUrl.username.isNotEmpty() || parsedUrl.password.isNotEmpty() || parsedUrl.fragment != null) {
      throw IllegalArgumentException("upload URL contains prohibited credentials")
    }
    rejectHeaderInjection(fileName, "file name")
    rejectHeaderInjection(mimeType, "mime type")

    val headers = JSONObject(headersJson)
    val headerBuilder = Headers.Builder()
    val headerNames = HashSet<String>()
    val headerKeys = headers.keys()
    while (headerKeys.hasNext()) {
      val name = headerKeys.next()
      val value = headers.optString(name, "")
      rejectHeaderInjection(name, "header name")
      rejectHeaderInjection(value, "header value")
      val lower = name.lowercase()
      if (lower in setOf("content-type", "content-length", "transfer-encoding", "cookie", "host")) {
        throw IllegalArgumentException("multipart transport controls $lower")
      }
      if (!headerNames.add(lower)) throw IllegalArgumentException("duplicate upload header")
      headerBuilder.add(name, value)
    }

    val fields = JSONObject(fieldsJson)
    val multipart = MultipartBody.Builder()
      .setType(MultipartBody.FORM)
    val fieldKeys = fields.keys()
    while (fieldKeys.hasNext()) {
      val name = fieldKeys.next()
      val value = fields.opt(name)
      if (value !is String) throw IllegalArgumentException("multipart field must be text")
      rejectHeaderInjection(name, "field name")
      multipart.addFormDataPart(name, value)
    }

    val context = appContext.reactContext ?: throw IllegalStateException("Context is null")
    val resolver = context.contentResolver
    val uri = Uri.parse(fileUri)
    if (uri.scheme != ContentResolver.SCHEME_FILE && uri.scheme != ContentResolver.SCHEME_CONTENT) {
      throw IllegalArgumentException("upload file URI must use file/content")
    }
    val mediaType = mimeType.toMediaTypeOrNull()
    val body = UriRequestBody(resolver, uri, mediaType, contentLength(resolver, uri))
    multipart.addFormDataPart("file", fileName, body)
    val requestBody = OneShotRequestBody(multipart.build())

    val request = Request.Builder()
      .url(parsedUrl)
      .headers(headerBuilder.build())
      .post(requestBody)
      .build()
    val call = client.newCall(request)
    val registered = synchronized(registryLock) {
      if (destroyed || requestStates[requestId] != RequestState.ACTIVE) {
        false
      } else {
        activeCalls[requestId] = call
        true
      }
    }
    if (!registered) {
      call.cancel()
      throw java.util.concurrent.CancellationException("upload request was cancelled")
    }
    try {
      call.execute().use { response ->
        val responseBody = response.body?.byteStream()?.use(::readBounded) ?: ""
        return JSONObject()
          .put("status", response.code)
          .put("body", responseBody)
          .toString()
      }
    } finally {
      synchronized(registryLock) {
        if (activeCalls[requestId] === call) activeCalls.remove(requestId)
      }
    }
  }

  private fun contentLength(resolver: ContentResolver, uri: Uri): Long {
    if (uri.scheme == ContentResolver.SCHEME_FILE) {
      return uri.path?.let(::File)?.length() ?: -1L
    }
    resolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { cursor ->
      val index = cursor.getColumnIndex(OpenableColumns.SIZE)
      if (index >= 0 && cursor.moveToFirst() && !cursor.isNull(index)) {
        return cursor.getLong(index)
      }
    }
    return -1L
  }

  private fun readBounded(input: InputStream): String {
    val output = ByteArrayOutputStream()
    val buffer = ByteArray(COPY_BUFFER_BYTES)
    var total = 0
    while (true) {
      val count = input.read(buffer)
      if (count < 0) break
      total += count
      if (total > RESPONSE_LIMIT_BYTES) throw IllegalStateException("upload response is too large")
      output.write(buffer, 0, count)
    }
    return output.toString(Charsets.UTF_8.name())
  }

  private fun rejectHeaderInjection(value: String, label: String) {
    if (value.contains('\r') || value.contains('\n')) {
      throw IllegalArgumentException("$label contains invalid characters")
    }
  }
}
