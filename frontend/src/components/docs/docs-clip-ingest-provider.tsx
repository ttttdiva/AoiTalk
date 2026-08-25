"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { usePathname, useRouter } from "next/navigation";

import { OPEN_DOCS_CLIP_INGEST_EVENT } from "@/lib/clip-ingest-shortcut";
import { apiFetch } from "./docs-utils";
import {
  DocsClipIngestDialog,
  type DocsClipIngestSubmission,
} from "./docs-clip-ingest-dialog";
import {
  DocsClipIngestPanel,
  useDocsClipIngestJobs,
  type DocsClipIngestJob,
} from "./docs-clip-ingest-panel";
import {
  DocsClipIngestDetailDialog,
  docsClipIngestReceiptDetailPath,
  normalizeDocsClipIngestReceiptDetail,
  sortDocsClipIngestReceiptSummaries,
  type DocsClipIngestReceiptDetail,
  type DocsClipIngestReceiptSummary,
} from "./docs-clip-ingest-detail-dialog";
import { isDocsWorkspaceUnmountedError } from "./docs-workspace-shared";

type ReceiptSelection =
  | { source: "history"; jobId: string; receiptId: string }
  | { source: "provenance"; receiptId: string };

type DocsClipIngestContextValue = {
  dialogOpen: boolean;
  historyOpen: boolean;
  ingestEnabled: boolean;
  jobs: DocsClipIngestJob[];
  connectionError: boolean;
  requestError: string | null;
  openDialog: () => void;
  setIngestEnabled: (enabled: boolean) => void;
  setDialogOpen: (open: boolean) => void;
  setHistoryOpen: (open: boolean) => void;
  setOpenNodeHandler: (
    handler: ((nodeId: string) => void | Promise<void>) | null,
  ) => void;
  enqueue: (submission: DocsClipIngestSubmission | string) => void;
  dismiss: (id: string) => void;
  retry: (id: string) => void;
  /** Open one canonical server-side receipt in the shared detail Dialog. */
  openReceipt: (receipt: DocsClipIngestReceiptSummary | string) => void;
  /** Open the shared picker for two or more server-side receipt summaries. */
  openReceiptPicker: (receipts: DocsClipIngestReceiptSummary[]) => void;
  closeReceiptDetail: () => void;
};

const DocsClipIngestContext = createContext<DocsClipIngestContextValue | null>(null);

export function useDocsClipIngest() {
  const context = useContext(DocsClipIngestContext);
  if (!context) {
    throw new Error("useDocsClipIngest must be used within DocsClipIngestProvider");
  }
  return context;
}

function errorStatus(value: unknown): number | null {
  if (!value || typeof value !== "object") return null;
  const status = (value as { status?: unknown }).status;
  return typeof status === "number" ? status : null;
}

function receiptErrorMessage(value: unknown): string {
  if (errorStatus(value) === 404) {
    return "このDocsノードの取り込み記録を表示する権限がありません。ノードの共有範囲と現在のアカウントを確認してください。";
  }
  return value instanceof Error
    ? value.message
    : "取り込み記録を読み込めませんでした。時間をおいて再試行してください。";
}

export function DocsClipIngestProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [ingestEnabled, setIngestEnabledState] = useState(true);
  const [openNodeHandler, setOpenNodeHandlerState] = useState<
    ((nodeId: string) => void | Promise<void>) | null
  >(null);
  const openNodeHandlerRef = useRef(openNodeHandler);
  const {
    jobs,
    connectionError,
    requestError,
    enqueue: enqueueJob,
    dismiss,
    retry: retryJob,
  } = useDocsClipIngestJobs();

  const [receiptSelection, setReceiptSelection] = useState<ReceiptSelection | null>(null);
  const [receiptPicker, setReceiptPicker] = useState<DocsClipIngestReceiptSummary[] | null>(null);
  const [receiptDetail, setReceiptDetail] = useState<DocsClipIngestReceiptDetail | null>(null);
  const [receiptLoading, setReceiptLoading] = useState(false);
  const [receiptError, setReceiptError] = useState<string | null>(null);
  const receiptGenerationRef = useRef(0);
  const receiptAbortRef = useRef<AbortController | null>(null);

  const setIngestEnabled = useCallback((enabled: boolean) => {
    setIngestEnabledState(enabled);
    if (!enabled) {
      // 読み取り専用Docsでは新規取り込みだけを止める。既存履歴は閲覧できる。
      setDialogOpen(false);
    }
  }, []);

  const setOpenNodeHandler = useCallback(
    (handler: ((nodeId: string) => void | Promise<void>) | null) => {
      // setState に関数を直接渡すと updater として実行されるため、
      // handler 自体を state 値として保持するための二重関数にする。
      openNodeHandlerRef.current = handler;
      setOpenNodeHandlerState(() => handler);
    },
    [],
  );

  const openDialog = useCallback(() => {
    if (ingestEnabled) setDialogOpen(true);
  }, [ingestEnabled]);

  const closeReceiptDetail = useCallback(() => {
    receiptGenerationRef.current += 1;
    receiptAbortRef.current?.abort();
    receiptAbortRef.current = null;
    setReceiptSelection(null);
    setReceiptPicker(null);
    setReceiptDetail(null);
    setReceiptLoading(false);
    setReceiptError(null);
  }, []);

  const loadReceipt = useCallback((
    receiptId: string,
    selection: ReceiptSelection,
  ) => {
    const generation = receiptGenerationRef.current + 1;
    receiptGenerationRef.current = generation;
    receiptAbortRef.current?.abort();
    const controller = new AbortController();
    receiptAbortRef.current = controller;
    setReceiptSelection(selection);
    setReceiptPicker(null);
    setReceiptDetail(null);
    setReceiptError(null);
    setReceiptLoading(true);
    void apiFetch<unknown>(docsClipIngestReceiptDetailPath(receiptId), {
      signal: controller.signal,
    })
      .then((response) => {
        if (controller.signal.aborted || receiptGenerationRef.current !== generation) return;
        const normalized = normalizeDocsClipIngestReceiptDetail(response);
        if (!normalized) throw new Error("取り込み記録の形式が不正です");
        setReceiptDetail(normalized);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || receiptGenerationRef.current !== generation) return;
        setReceiptError(receiptErrorMessage(error));
      })
      .finally(() => {
        if (controller.signal.aborted || receiptGenerationRef.current !== generation) return;
        setReceiptLoading(false);
      });
  }, []);

  const openReceipt = useCallback((receipt: DocsClipIngestReceiptSummary | string) => {
    const receiptId = typeof receipt === "string" ? receipt : receipt.id;
    if (!receiptId.trim()) return;
    loadReceipt(receiptId, { source: "provenance", receiptId });
  }, [loadReceipt]);

  const openReceiptPicker = useCallback((receipts: DocsClipIngestReceiptSummary[]) => {
    const unique = Array.from(new Map(
      receipts.filter((item) => item.id.trim()).map((item) => [item.id, item]),
    ).values());
    const newestFirst = sortDocsClipIngestReceiptSummaries(unique);
    if (newestFirst.length === 0) return;
    if (newestFirst.length === 1) {
      openReceipt(newestFirst[0]);
      return;
    }
    receiptGenerationRef.current += 1;
    receiptAbortRef.current?.abort();
    receiptAbortRef.current = null;
    setReceiptSelection({ source: "provenance", receiptId: "" });
    setReceiptPicker(newestFirst);
    setReceiptDetail(null);
    setReceiptLoading(false);
    setReceiptError(null);
  }, [openReceipt]);

  const openHistoryDetail = useCallback((job: DocsClipIngestJob) => {
    const receiptId = job.receipt_id?.trim();
    if (!receiptId) {
      receiptGenerationRef.current += 1;
      receiptAbortRef.current?.abort();
      receiptAbortRef.current = null;
      setReceiptSelection(null);
      setReceiptPicker(null);
      setReceiptDetail(null);
      setReceiptLoading(false);
      setReceiptError(null);
      // A legacy cache entry without a receipt ID remains viewable through
      // the local job result.  It is never used to synthesize a new receipt.
      setReceiptSelection({ source: "history", jobId: job.id, receiptId: "" });
      return;
    }
    loadReceipt(receiptId, { source: "history", jobId: job.id, receiptId });
  }, [loadReceipt]);

  const retry = useCallback((id: string) => {
    if (receiptSelection?.source === "history" && receiptSelection.jobId === id) {
      setReceiptDetail(null);
      setReceiptError(null);
      setReceiptLoading(false);
    }
    retryJob(id);
  }, [receiptSelection, retryJob]);

  useEffect(() => {
    if (receiptSelection?.source !== "history") return;
    const job = jobs.find((item) => item.id === receiptSelection.jobId);
    if (!job || job.status !== "success" || !job.receipt_id) return;
    if (job.receipt_id === receiptSelection.receiptId) return;
    // This effect bridges a completed retry to the newly-created durable
    // receipt.  The fetch itself is generation-guarded in loadReceipt.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadReceipt(job.receipt_id, {
      source: "history",
      jobId: job.id,
      receiptId: job.receipt_id,
    });
  }, [jobs, loadReceipt, receiptSelection]);

  useEffect(() => {
    // Closing the history Sheet should not close a provenance detail opened
    // from the current document, but it must release a history-owned detail.
    if (!historyOpen && receiptSelection?.source === "history") {
      // Closing the Sheet releases history-owned detail state; provenance
      // detail opened from the document is intentionally left alone.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      closeReceiptDetail();
    }
  }, [closeReceiptDetail, historyOpen, receiptSelection]);

  useEffect(() => () => {
    receiptGenerationRef.current += 1;
    receiptAbortRef.current?.abort();
  }, []);

  const enqueue = useCallback(
    (submission: DocsClipIngestSubmission | string) => {
      if (!ingestEnabled) return;
      setHistoryOpen(true);
      enqueueJob(submission);
    },
    [enqueueJob, ingestEnabled],
  );

  const openResultNode = useCallback(async (nodeId: string) => {
    setHistoryOpen(false);
    closeReceiptDetail();
    // Docs内は Workspace の opener。Docs外と Workspace 不在だけ route を入口にする。
    const isDocsRoute = pathname === "/docs" || pathname?.startsWith("/docs/");
    if (isDocsRoute) {
      const handler = openNodeHandler;
      if (handler) {
        try {
          await handler(nodeId);
          // Workspaceがunmount/re-registerされた境界を跨いだ場合、旧handlerの
          // 成功を現在画面の成功扱いにせず canonical routeへ引き渡す。
          if (handler === openNodeHandler && openNodeHandlerRef.current !== handler) {
            router.push(`/docs/${encodeURIComponent(nodeId)}`);
          }
          return;
        } catch (error) {
          if (!isDocsWorkspaceUnmountedError(error)) return;
        }
      }
    }
    router.push(`/docs/${encodeURIComponent(nodeId)}`);
  }, [closeReceiptDetail, openNodeHandler, pathname, router]);

  useEffect(() => {
    const handleOpen = () => openDialog();
    window.addEventListener(OPEN_DOCS_CLIP_INGEST_EVENT, handleOpen);
    return () => {
      window.removeEventListener(OPEN_DOCS_CLIP_INGEST_EVENT, handleOpen);
    };
  }, [openDialog]);

  const selectedHistoryJob = receiptSelection?.source === "history"
    ? jobs.find((item) => item.id === receiptSelection.jobId) ?? null
    : null;
  const historyDetailOpen = receiptSelection?.source === "history";
  const detailOpen = receiptSelection !== null;

  const value = useMemo<DocsClipIngestContextValue>(
    () => ({
      dialogOpen,
      historyOpen,
      ingestEnabled,
      jobs,
      connectionError,
      requestError,
      openDialog,
      setIngestEnabled,
      setDialogOpen,
      setHistoryOpen,
      setOpenNodeHandler,
      enqueue,
      dismiss,
      retry,
      openReceipt,
      openReceiptPicker,
      closeReceiptDetail,
    }),
    [
      closeReceiptDetail,
      connectionError,
      dialogOpen,
      dismiss,
      enqueue,
      historyOpen,
      ingestEnabled,
      jobs,
      openDialog,
      openReceipt,
      openReceiptPicker,
      requestError,
      retry,
      setIngestEnabled,
      setOpenNodeHandler,
    ],
  );

  return (
    <DocsClipIngestContext.Provider value={value}>
      {children}
      <DocsClipIngestDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onSubmit={enqueue}
      />
      <DocsClipIngestPanel
        jobs={jobs}
        connectionError={connectionError}
        requestError={requestError}
        open={historyOpen}
        detailOpen={historyDetailOpen}
        onOpenChange={setHistoryOpen}
        onDismiss={dismiss}
        onRetry={(jobId) => {
          setHistoryOpen(true);
          retry(jobId);
        }}
        onOpenDetail={openHistoryDetail}
        onCloseDetail={() => {
          if (receiptSelection?.source === "history") closeReceiptDetail();
        }}
        onOpenNode={openResultNode}
      />
      <DocsClipIngestDetailDialog
        open={detailOpen}
        // Once a receipt id exists, the server detail is authoritative.  Do
        // not let a cached success result appear beside or instead of a
        // receipt loading/error state.
        legacyJob={receiptSelection?.source === "history" && receiptSelection.receiptId
          ? null
          : selectedHistoryJob}
        receipt={receiptDetail}
        receiptLoading={receiptLoading}
        receiptError={receiptError}
        receiptPicker={receiptPicker}
        onSelectReceipt={(receipt) => openReceipt(receipt)}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) closeReceiptDetail();
        }}
        onRetry={(jobId) => {
          setHistoryOpen(true);
          retry(jobId);
        }}
        onOpenNode={openResultNode}
      />
    </DocsClipIngestContext.Provider>
  );
}
