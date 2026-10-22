import { randomUUID } from "expo-crypto";
import { File } from "expo-file-system";
import { requireNativeModule } from "expo";
import { protectedFetch } from "./protected-fetch";
import { mobilePlatform } from "./platform";

export type NativeMultipartFile = {
  uri: string;
  name: string;
  mimeType?: string | null;
};

export type NativeMultipartRequest = {
  url: string;
  headers: Record<string, string>;
  fields?: Record<string, string>;
  file: NativeMultipartFile;
  signal?: AbortSignal;
};

type NativeMultipartModule = {
  prepare(requestId: string): void;
  upload(
    requestId: string,
    url: string,
    headersJson: string,
    fieldsJson: string,
    fileUri: string,
    fileName: string,
    mimeType: string,
  ): Promise<string>;
  cancel(requestId: string): void;
  release(requestId: string): void;
};

export type NativeMultipartResponse = {
  ok: boolean;
  status: number;
  text(): Promise<string>;
  json<T = unknown>(): Promise<T>;
};

function createAbortError(): Error {
  const error = new Error("ファイル送信を中止しました");
  error.name = "AbortError";
  return error;
}

function getNativeMultipartModule(): NativeMultipartModule {
  try {
    return requireNativeModule<NativeMultipartModule>("AoiTalkNativeMultipart");
  } catch (error) {
    throw new Error(
      "大容量ファイル送信に必要なNative multipart moduleがこのビルドに含まれていません。",
      { cause: error },
    );
  }
}

function assertNotAborted(signal?: AbortSignal): void {
  if (!signal?.aborted) return;
  throw createAbortError();
}

function rejectHeaderInjection(value: string, label: string): void {
  if (value.includes("\r") || value.includes("\n")) {
    throw new Error(`${label} contains invalid characters`);
  }
}

function assertMultipartUrl(url: string): void {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error("upload URL is invalid");
  }
  if (
    !["http:", "https:"].includes(parsed.protocol.toLowerCase()) ||
    parsed.username ||
    parsed.password ||
    parsed.hash
  ) {
    throw new Error("upload URL contains prohibited credentials");
  }
}

function assertLocalFileUri(uri: string): void {
  let parsed: URL;
  try {
    parsed = new URL(uri);
  } catch {
    throw new Error("upload file URI is invalid");
  }
  const allowedProtocols =
    mobilePlatform.OS === "android"
      ? ["file:", "content:"]
      : mobilePlatform.OS === "ios"
        ? ["file:"]
        : ["blob:", "data:"];
  if (!allowedProtocols.includes(parsed.protocol.toLowerCase())) {
    throw new Error("upload file URI uses a prohibited scheme");
  }
}

function assertMultipartBoundary(request: NativeMultipartRequest): void {
  assertMultipartUrl(request.url);
  assertLocalFileUri(request.file.uri);
  rejectHeaderInjection(request.file.name, "file name");
  rejectHeaderInjection(request.file.mimeType ?? "", "mime type");
  for (const [name, value] of Object.entries(request.headers)) {
    rejectHeaderInjection(name, "header name");
    rejectHeaderInjection(value, "header value");
    if (
      ["content-type", "content-length", "transfer-encoding", "cookie", "host"].includes(
        name.toLowerCase(),
      )
    ) {
      throw new Error(`multipart transport controls ${name.toLowerCase()}`);
    }
  }
  for (const [name, value] of Object.entries(request.fields ?? {})) {
    rejectHeaderInjection(name, "field name");
    rejectHeaderInjection(value, "field value");
  }
}

async function protectedMultipartUpload(
  request: NativeMultipartRequest,
): Promise<NativeMultipartResponse> {
  if (mobilePlatform.OS === "web") {
    return protectedWebMultipartUpload(request);
  }
  if (mobilePlatform.OS !== "ios") {
    throw new Error(
      "大容量ファイル送信に必要なNative multipart moduleがこのプラットフォームに含まれていません。",
    );
  }

  const file = new File(request.file.uri);
  const filePart =
    request.file.mimeType && file.type !== request.file.mimeType
      ? file.slice(0, file.size, request.file.mimeType)
      : file;
  const formData = new FormData();
  for (const [name, value] of Object.entries(request.fields ?? {})) {
    formData.append(name, value);
  }
  formData.append("file", filePart, request.file.name);
  return protectedFetch(request.url, {
    method: "POST",
    headers: request.headers,
    body: formData,
    signal: request.signal,
  });
}

function assertWebLocalUri(uri: string): void {
  let parsed: URL;
  try {
    parsed = new URL(uri);
  } catch {
    throw new Error("Web upload file URI is invalid");
  }
  if (!["blob:", "data:"].includes(parsed.protocol.toLowerCase())) {
    throw new Error("Web upload accepts only local blob/data URIs");
  }
}

async function protectedWebMultipartUpload(
  request: NativeMultipartRequest,
): Promise<NativeMultipartResponse> {
  assertWebLocalUri(request.file.uri);
  const localResponse = await protectedFetch(request.file.uri, {
    method: "GET",
    headers: {},
    signal: request.signal,
  });
  if (!localResponse.ok) {
    throw new Error(`Web upload file read failed (${localResponse.status})`);
  }
  const sourceBlob = await localResponse.blob();
  assertNotAborted(request.signal);
  const filePart =
    request.file.mimeType && sourceBlob.type !== request.file.mimeType
      ? sourceBlob.slice(0, sourceBlob.size, request.file.mimeType)
      : sourceBlob;
  const formData = new FormData();
  for (const [name, value] of Object.entries(request.fields ?? {})) {
    formData.append(name, value);
  }
  formData.append("file", filePart, request.file.name);
  return protectedFetch(request.url, {
    method: "POST",
    headers: request.headers,
    body: formData,
    signal: request.signal,
  });
}

function parseNativeResponse(raw: string): NativeMultipartResponse {
  const parsed = JSON.parse(raw) as { status?: unknown; body?: unknown };
  if (
    typeof parsed.status !== "number" ||
    !Number.isInteger(parsed.status) ||
    typeof parsed.body !== "string"
  ) {
    throw new Error("Native multipart response is invalid");
  }
  const body = parsed.body;
  return {
    ok: parsed.status >= 200 && parsed.status < 300,
    status: parsed.status,
    text: async () => body,
    json: async <T>() => JSON.parse(body) as T,
  };
}

/**
 * Send one local/content URI through the platform multipart boundary. Android
 * uses a native streaming bridge; iOS uses an Expo File part and Web uses a
 * local blob/data URI through the protected fetch boundary. Auth headers are
 * supplied by sendWithTransportAuth immediately before this helper is called;
 * this module never resolves or stores credentials itself.
 */
export async function nativeMultipartUpload(
  request: NativeMultipartRequest,
): Promise<NativeMultipartResponse> {
  assertNotAborted(request.signal);
  assertMultipartBoundary(request);
  if (mobilePlatform.OS !== "android") {
    return protectedMultipartUpload(request);
  }
  const native = getNativeMultipartModule();
  const requestId = randomUUID();
  let aborted = false;
  let prepared = false;
  const abort = () => {
    aborted = true;
    native.cancel(requestId);
  };
  request.signal?.addEventListener("abort", abort, { once: true });
  try {
    native.prepare(requestId);
    prepared = true;
    if (request.signal?.aborted) {
      abort();
      const error = new Error("ファイル送信を中止しました");
      error.name = "AbortError";
      throw error;
    }
    const raw = await native.upload(
      requestId,
      request.url,
      JSON.stringify(request.headers),
      JSON.stringify(request.fields ?? {}),
      request.file.uri,
      request.file.name,
      request.file.mimeType || "application/octet-stream",
    );
    if (aborted) {
      throw createAbortError();
    }
    return parseNativeResponse(raw);
  } catch (error) {
    if (aborted || request.signal?.aborted) {
      throw createAbortError();
    }
    throw error;
  } finally {
    request.signal?.removeEventListener("abort", abort);
    if (prepared) native.release(requestId);
  }
}
