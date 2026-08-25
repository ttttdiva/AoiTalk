"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getProjectManagementConfig,
  getProjectWbsScan,
  listProjectManagementFiles,
  updateProjectManagementConfig,
  uploadProjectManagementFile,
  type ManagementConfig,
  type ManagementConfigPatch,
  type ManagementDocumentKind,
  type ManagementFileKind,
  type ProjectFilerListResponse,
  type WbsScanResponse,
} from "@/lib/projects-workspace-api";

export type ManagementFilePicker = {
  kind: ManagementDocumentKind;
  title: string;
  accept: string;
};

export type ManagementUploadResult = {
  kind: ManagementFileKind;
  total: number;
  succeeded: Array<{ name: string; path?: string }>;
  failed: Array<{ name: string; error: string }>;
};

type UseProjectManagementFilesOptions = {
  projectId: string | null;
  enabled: boolean;
};

const EMPTY_CONFIG: ManagementConfig = {
  wbsFile: null,
  issueFile: null,
  riskFile: null,
  requestFiles: [],
};

function normalizeConfig(config: ManagementConfig): ManagementConfig {
  return {
    wbsFile: config.wbsFile || null,
    issueFile: config.issueFile || null,
    riskFile: config.riskFile || null,
    requestFiles: config.requestFiles || [],
  };
}

function isAbortError(error: unknown) {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}

function uploadResponseFailure(value: unknown): string | null {
  if (
    typeof value === "object" &&
    value !== null &&
    "success" in value &&
    value.success === false
  ) {
    if (
      "detail" in value &&
      typeof value.detail === "string" &&
      value.detail.trim()
    ) {
      return value.detail;
    }
    return "アップロードに失敗しました";
  }
  return null;
}

function uploadErrorMessage(file: File, error: unknown): string {
  const message = errorMessage(error, "アップロードに失敗しました");
  const prefix = `${file.name}:`;
  return message.startsWith(prefix) ? message.slice(prefix.length).trim() : message;
}

function fallbackDigest(bytes: Uint8Array): string {
  // SubtleCrypto is available in supported browsers, but keep a deterministic
  // non-cryptographic fallback for older WebViews so distinct file contents do
  // not collapse to the same metadata-only key.
  let hash = 2166136261;
  for (const byte of bytes) {
    hash ^= byte;
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash.toString(16).padStart(8, "0").repeat(8);
}

async function digestBytes(bytes: Uint8Array): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (subtle) {
    try {
      const hash = await subtle.digest(
        "SHA-256",
        bytes.slice().buffer as ArrayBuffer,
      );
      return Array.from(new Uint8Array(hash))
        .map((value) => value.toString(16).padStart(2, "0"))
        .join("");
    } catch {
      // Fall through to the deterministic fallback below.
    }
  }
  return fallbackDigest(bytes);
}

async function fileBytes(file: File): Promise<Uint8Array> {
  if (typeof file.arrayBuffer === "function") {
    return new Uint8Array(await file.arrayBuffer());
  }
  if (typeof FileReader !== "undefined") {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error ?? new Error("file read failed"));
      reader.onload = () => {
        const value = reader.result;
        if (value instanceof ArrayBuffer) resolve(new Uint8Array(value));
        else reject(new Error("file read returned non-binary data"));
      };
      reader.readAsArrayBuffer(file);
    });
  }
  // Last-resort deterministic fallback for non-browser test doubles.
  return new TextEncoder().encode(`${file.name}\u0000${file.size}`);
}

type UploadIdentity = { signature: string; idempotencyKey: string };

async function uploadIdentity(
  projectId: string,
  kind: ManagementFileKind,
  file: File,
): Promise<UploadIdentity> {
  const contentDigest = await digestBytes(await fileBytes(file));
  const material = [
    projectId,
    kind,
    file.name,
    String(file.size),
    String(file.lastModified),
    contentDigest,
  ].join("\u0000");
  const keyDigest = await digestBytes(
    new TextEncoder().encode(material),
  );
  return {
    signature: `${material}\u0000${contentDigest}`,
    // Hash the entire identity material so Unicode/long filenames cannot
    // exceed the server's bounded header length.
    idempotencyKey: `v1:${keyDigest}`,
  };
}

type ManagementLoadResult =
  | { ok: true }
  | { ok: false; error: string };

export function useProjectManagementFiles({
  projectId,
  enabled,
}: UseProjectManagementFilesOptions) {
  const activeProjectId = enabled ? projectId : null;
  const [stateProjectId, setStateProjectId] = useState<string | null>(null);
  const [config, setConfig] = useState<ManagementConfig>(EMPTY_CONFIG);
  const [wbsScan, setWbsScan] = useState<WbsScanResponse | null>(null);
  const [managementLoading, setManagementLoading] = useState(false);
  const [managementSaving, setManagementSaving] = useState(false);
  const [managementUploading, setManagementUploading] =
    useState<ManagementFileKind | null>(null);
  const [managementError, setManagementError] = useState("");
  const [syncResult, setSyncResult] = useState<string | null>(null);
  const [uploadResult, setUploadResult] =
    useState<ManagementUploadResult | null>(null);
  const [filePicker, setFilePicker] = useState<ManagementFilePicker | null>(
    null,
  );
  const [filePickerPath, setFilePickerPathState] = useState("");
  const [filePickerData, setFilePickerData] =
    useState<ProjectFilerListResponse | null>(null);
  const [filePickerLoading, setFilePickerLoading] = useState(false);
  const [filePickerError, setFilePickerError] = useState("");
  const generationRef = useRef(0);
  const projectAbortRef = useRef<AbortController | null>(null);
  const filePickerRequestRef = useRef(0);
  const uploadRequestRef = useRef(new Set<string>());
  const successfulUploadRef = useRef(new Set<string>());
  const managementSaveRequestRef = useRef<number | null>(null);

  const isCurrentProject = useCallback(
    (targetProjectId: string, generation: number) =>
      activeProjectId === targetProjectId &&
      generationRef.current === generation,
    [activeProjectId],
  );

  const loadManagement = useCallback(
    async (
      targetProjectId: string,
      generation: number,
      signal: AbortSignal,
    ): Promise<ManagementLoadResult> => {
      if (isCurrentProject(targetProjectId, generation)) {
        setManagementLoading(true);
        setManagementError("");
      }
      try {
        const [configData, scanData] = await Promise.all([
          getProjectManagementConfig(targetProjectId, signal),
          getProjectWbsScan(targetProjectId, signal),
        ]);
        if (isCurrentProject(targetProjectId, generation)) {
          setConfig(normalizeConfig(configData.config));
          setWbsScan(scanData);
        }
        return { ok: true };
      } catch (error) {
        if (isAbortError(error)) {
          return { ok: false, error: "管理資料設定の取得を中断しました" };
        }
        const message = errorMessage(
          error,
          "管理資料設定の取得に失敗しました",
        );
        if (isCurrentProject(targetProjectId, generation)) {
          setManagementError(message);
        }
        return { ok: false, error: message };
      } finally {
        if (isCurrentProject(targetProjectId, generation)) {
          setManagementLoading(false);
        }
      }
    },
    [isCurrentProject],
  );

  useEffect(() => {
    projectAbortRef.current?.abort();
    filePickerRequestRef.current += 1;
    const controller = new AbortController();
    projectAbortRef.current = controller;
    const generation = ++generationRef.current;

    setStateProjectId(activeProjectId);
    setConfig(EMPTY_CONFIG);
    setWbsScan(null);
    setManagementLoading(Boolean(activeProjectId));
    setManagementSaving(false);
    setManagementUploading(null);
    setManagementError("");
    setSyncResult(null);
    setUploadResult(null);
    setFilePicker(null);
    setFilePickerPathState("");
    setFilePickerData(null);
    setFilePickerLoading(false);
    setFilePickerError("");
    uploadRequestRef.current.clear();
    successfulUploadRef.current.clear();
    managementSaveRequestRef.current = null;

    if (activeProjectId) {
      void loadManagement(activeProjectId, generation, controller.signal);
    }

    return () => {
      controller.abort();
      if (projectAbortRef.current === controller)
        projectAbortRef.current = null;
      if (generationRef.current === generation) generationRef.current += 1;
    };
  }, [activeProjectId, loadManagement]);

  const current = stateProjectId === activeProjectId;

  const refreshManagement = useCallback(async () => {
    const targetProjectId = activeProjectId;
    const generation = generationRef.current;
    const controller = projectAbortRef.current;
    if (!targetProjectId || !controller) return;
    await loadManagement(targetProjectId, generation, controller.signal);
  }, [activeProjectId, loadManagement]);

  const saveManagementConfigPatch = useCallback(
    async (patch: ManagementConfigPatch) => {
      const targetProjectId = activeProjectId;
      const generation = generationRef.current;
      const controller = projectAbortRef.current;
      if (
        !targetProjectId ||
        !controller ||
        managementSaveRequestRef.current !== null
      ) {
        return null;
      }
      managementSaveRequestRef.current = generation;
      setManagementSaving(true);
      setManagementError("");
      setSyncResult(null);
      try {
        const data = await updateProjectManagementConfig(
          targetProjectId,
          patch,
          controller.signal,
        );
        if (!isCurrentProject(targetProjectId, generation)) return null;
        setConfig(normalizeConfig(data.config));
        await loadManagement(targetProjectId, generation, controller.signal);
        return data.config;
      } catch (error) {
        if (
          !isAbortError(error) &&
          isCurrentProject(targetProjectId, generation)
        ) {
          setManagementError(
              errorMessage(error, "案件資料の登録に失敗しました"),
          );
        }
        return null;
      } finally {
        if (managementSaveRequestRef.current === generation) {
          managementSaveRequestRef.current = null;
        }
        if (isCurrentProject(targetProjectId, generation)) {
          setManagementSaving(false);
        }
      }
    },
    [activeProjectId, isCurrentProject, loadManagement],
  );

  const registerExistingManagementFile = useCallback(
    async (kind: ManagementDocumentKind, filePath: string) => {
      const normalizedPath = filePath.trim();
      if (!normalizedPath) return false;
      const nextRequestFiles =
        kind === "request"
          ? [...new Set([...config.requestFiles, normalizedPath])]
          : config.requestFiles;
      const nextConfig = await saveManagementConfigPatch({
        wbs_file: kind === "wbs" ? normalizedPath : config.wbsFile || null,
        issue_file:
          kind === "issue" ? normalizedPath : config.issueFile || null,
        risk_file: kind === "risk" ? normalizedPath : config.riskFile || null,
        request_files: nextRequestFiles,
      });
      if (nextConfig) {
        setSyncResult(
          kind === "wbs"
            ? "WBSをProject Filesから登録しました。"
            : "資料をProject Filesから登録しました。",
        );
      }
      return Boolean(nextConfig);
    },
    [config, saveManagementConfigPatch],
  );

  const clearManagementFile = useCallback(
    async (kind: ManagementDocumentKind, filePath?: string) => {
      const nextRequestFiles =
        kind === "request" && filePath
          ? config.requestFiles.filter((item) => item !== filePath)
          : config.requestFiles;
      await saveManagementConfigPatch({
        wbs_file: kind === "wbs" ? null : config.wbsFile || null,
        issue_file: kind === "issue" ? null : config.issueFile || null,
        risk_file: kind === "risk" ? null : config.riskFile || null,
        request_files:
          kind === "request" ? nextRequestFiles : config.requestFiles,
      });
    },
    [config, saveManagementConfigPatch],
  );

  const openFilePicker = useCallback(
    (kind: ManagementDocumentKind, title: string, accept: string) => {
      setFilePicker({ kind, title, accept });
      setFilePickerPathState("");
      setFilePickerData(null);
      setFilePickerError("");
    },
    [],
  );

  const closeFilePicker = useCallback(() => {
    filePickerRequestRef.current += 1;
    setFilePicker(null);
    setFilePickerData(null);
    setFilePickerLoading(false);
    setFilePickerError("");
  }, []);

  const setFilePickerPath = useCallback((path: string) => {
    setFilePickerPathState(path);
  }, []);

  useEffect(() => {
    const targetProjectId = activeProjectId;
    const controller = projectAbortRef.current;
    if (!filePicker || !targetProjectId || !controller) return;
    const generation = generationRef.current;
    const request = ++filePickerRequestRef.current;
    setFilePickerLoading(true);
    setFilePickerError("");
    void listProjectManagementFiles(
      targetProjectId,
      filePickerPath,
      controller.signal,
    )
      .then((data) => {
        if (
          request === filePickerRequestRef.current &&
          isCurrentProject(targetProjectId, generation)
        ) {
          setFilePickerData(data);
        }
      })
      .catch((error) => {
        if (
          request === filePickerRequestRef.current &&
          !isAbortError(error) &&
          isCurrentProject(targetProjectId, generation)
        ) {
          setFilePickerError(
            error instanceof Error
              ? error.message
              : "Project Filesの読み込みに失敗しました",
          );
        }
      })
      .finally(() => {
        if (
          request === filePickerRequestRef.current &&
          isCurrentProject(targetProjectId, generation)
        ) {
          setFilePickerLoading(false);
        }
      });
  }, [activeProjectId, filePicker, filePickerPath, isCurrentProject]);

  const selectExistingManagementFile = useCallback(
    async (kind: ManagementDocumentKind, path: string) => {
      const targetProjectId = activeProjectId;
      const generation = generationRef.current;
      const registered = await registerExistingManagementFile(kind, path);
      if (
        registered &&
        targetProjectId &&
        isCurrentProject(targetProjectId, generation)
      ) {
        closeFilePicker();
      }
    },
    [
      activeProjectId,
      closeFilePicker,
      isCurrentProject,
      registerExistingManagementFile,
    ],
  );

  const uploadManagementFiles = useCallback(
    async (kind: ManagementFileKind, files: File[]) => {
      const targetProjectId = activeProjectId;
      const generation = generationRef.current;
      const controller = projectAbortRef.current;
      if (!targetProjectId || !controller || files.length === 0) return;

      const requestKey = `${targetProjectId}:${generation}:${kind}`;
      // Keep the single managementUploading indicator truthful and avoid
      // racing config snapshots when two cards are submitted together.
      if (uploadRequestRef.current.size > 0) return;
      uploadRequestRef.current.add(requestKey);

      setManagementUploading(kind);
      setManagementError("");
      setSyncResult(null);
      setUploadResult(null);
      try {
        const identities = await Promise.all(
          files.map((file) => uploadIdentity(targetProjectId, kind, file)),
        );
        const identityFor = (file: File) =>
          identities[files.indexOf(file)] ?? identities[0];
        const uniqueFiles = files.filter(
          (file, index) =>
            index ===
            identities.findIndex(
              (identity) => identity.signature === identityFor(file).signature,
            ),
        );
        const pendingFiles = uniqueFiles.filter(
          (file) =>
            !successfulUploadRef.current.has(
              `${requestKey}:${identityFor(file).signature}`,
            ),
        );
        const alreadyUploaded = uniqueFiles
          .filter((file) => !pendingFiles.includes(file))
          .map((file) => ({ name: file.name }));
        const settled = await Promise.allSettled(
          pendingFiles.map(async (file) =>
            Promise.resolve().then(() =>
              uploadProjectManagementFile(
                targetProjectId,
                kind,
                file,
                controller.signal,
                identityFor(file).idempotencyKey,
              ),
            ),
          ),
        );
        if (!isCurrentProject(targetProjectId, generation)) return;

        const succeeded = [
          ...alreadyUploaded,
          ...pendingFiles.flatMap((file, index) => {
            const result = settled[index];
            if (
              !result ||
              result.status !== "fulfilled" ||
              uploadResponseFailure(result.value)
            ) {
              return [];
            }
            successfulUploadRef.current.add(
              `${requestKey}:${identityFor(file).signature}`,
            );
            const path =
              typeof result.value === "object" &&
              result.value !== null &&
              "path" in result.value &&
              typeof result.value.path === "string"
                ? result.value.path
                : undefined;
            return [{ name: file.name, ...(path ? { path } : {}) }];
          }),
        ];
        const failed = pendingFiles.flatMap((file, index) => {
          const result = settled[index];
          if (!result) {
            return [{ name: file.name, error: "アップロードに失敗しました" }];
          }
          if (result.status === "fulfilled") {
            const failure = uploadResponseFailure(result.value);
            return failure ? [{ name: file.name, error: failure }] : [];
          }
          return [
            {
              name: file.name,
              error: uploadErrorMessage(file, result.reason),
            },
          ];
        });
        const result = {
          kind,
          total: uniqueFiles.length,
          succeeded,
          failed,
        };
        const detail = failed
          .map((item) => `${item.name}: ${item.error}`)
          .join(" / ");
        const batchError =
          failed.length > 0
            ? `${failed.length}件のアップロードに失敗しました${detail ? `: ${detail}` : ""}`
            : "";
        const batchSyncResult =
          failed.length === 0
            ? succeeded.length === 1
              ? kind === "wbs"
                ? "WBSをProject Filesへ登録しました。"
                : "資料をProject Filesへ登録しました。"
              : kind === "wbs"
                ? `${succeeded.length}件のWBSをProject Filesへ登録しました。`
                : `${succeeded.length}件の資料をProject Filesへ登録しました。`
            : `${succeeded.length}件成功、${failed.length}件失敗しました。`;
        setUploadResult(result);
        setManagementError(batchError);
        setSyncResult(batchSyncResult);

        // Re-read after every batch, including an all-failed or partial batch.
        // This reconciles successful writes committed before a later failure.
        const reconciliation = await loadManagement(
          targetProjectId,
          generation,
          controller.signal,
        );
        if (isCurrentProject(targetProjectId, generation)) {
          // loadManagement clears the previous request error while it starts.
          // Preserve the batch outcome after a successful reconciliation, but
          // do not mask a failed re-read as if the server state were current.
          setUploadResult(result);
          if (reconciliation.ok) {
            setManagementError(batchError);
            setSyncResult(batchSyncResult);
          } else {
            const reconciliationError = reconciliation.error;
            const combinedError = [
              batchError,
              `最新状態の再取得に失敗しました: ${reconciliationError}`,
            ]
              .filter(Boolean)
              .join(" / ");
            setManagementError(combinedError);
            setSyncResult(
              `${batchSyncResult}（最新状態を確認できないため、再同期を再試行してください）`,
            );
          }
        }
      } catch (error) {
        if (
          !isAbortError(error) &&
          isCurrentProject(targetProjectId, generation)
        ) {
          setManagementError(errorMessage(error, "アップロードに失敗しました"));
        }
      } finally {
        uploadRequestRef.current.delete(requestKey);
        if (isCurrentProject(targetProjectId, generation)) {
          setManagementUploading(null);
        }
      }
    },
    [activeProjectId, isCurrentProject, loadManagement],
  );

  const visibleConfig = current ? config : EMPTY_CONFIG;
  return {
    wbsFile: visibleConfig.wbsFile || "",
    issueFile: visibleConfig.issueFile || "",
    riskFile: visibleConfig.riskFile || "",
    requestFiles: visibleConfig.requestFiles,
    wbsScan: current ? wbsScan : null,
    managementLoading: current && managementLoading,
    managementSaving: current && managementSaving,
    managementUploading: current ? managementUploading : null,
    managementError: current ? managementError : "",
    syncResult: current ? syncResult : null,
    uploadResult: current ? uploadResult : null,
    filePicker: current ? filePicker : null,
    filePickerPath: current ? filePickerPath : "",
    filePickerData: current ? filePickerData : null,
    filePickerLoading: current && filePickerLoading,
    filePickerError: current ? filePickerError : "",
    refreshManagement,
    clearManagementFile,
    openFilePicker,
    closeFilePicker,
    setFilePickerPath,
    selectExistingManagementFile,
    uploadManagementFiles,
  };
}

type ProjectManagementFilesHookReturn = ReturnType<
  typeof useProjectManagementFiles
>;

// Keep the new structured result optional for presentational controller stubs
// that only exercise the existing management fields.
export type ProjectManagementFilesController = Omit<
  ProjectManagementFilesHookReturn,
  "uploadResult"
> & {
  uploadResult?: ManagementUploadResult | null;
};
