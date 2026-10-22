"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { AlertDialog } from "@/components/ui/alert-dialog";

export type ConfirmOptions = {
  title?: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
};

type ConfirmFn = (options?: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

type ConfirmRequest = {
  id: number;
  options?: ConfirmOptions;
  returnFocus: HTMLElement | null;
  resolve: (value: boolean) => void;
  settled: boolean;
  focusRestoreResolved: boolean;
};

/**
 * useConfirm を利用するためのプロバイダ。
 * アプリのレイアウト上位にマウントする。
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [activeRequest, setActiveRequest] = useState<ConfirmRequest | null>(null);
  const nextIdRef = useRef(0);
  const activeRequestRef = useRef<ConfirmRequest | null>(null);
  const queueRef = useRef<ConfirmRequest[]>([]);
  const isLiveRef = useRef(true);
  const skipReturnFocusRequestsRef = useRef(new WeakSet<ConfirmRequest>());
  const lastSettledRequestRef = useRef<ConfirmRequest | null>(null);

  const activateNext = useCallback(() => {
    const next = queueRef.current.shift() ?? null;
    activeRequestRef.current = next;
    setActiveRequest(next);
  }, []);

  const confirm = useCallback<ConfirmFn>((options) => {
    return new Promise<boolean>((resolve) => {
      if (!isLiveRef.current) {
        resolve(false);
        return;
      }
      const currentRequest =
        activeRequestRef.current ??
        queueRef.current[0] ??
        (lastSettledRequestRef.current?.focusRestoreResolved
          ? null
          : lastSettledRequestRef.current);
      if (lastSettledRequestRef.current) {
        if (!lastSettledRequestRef.current.focusRestoreResolved) {
          skipReturnFocusRequestsRef.current.add(lastSettledRequestRef.current);
        }
        lastSettledRequestRef.current = null;
      }
      const returnFocus = currentRequest
        ? currentRequest.returnFocus
        : typeof document !== "undefined" && document.activeElement instanceof HTMLElement && document.activeElement !== document.body
          ? document.activeElement
          : null;
      const request: ConfirmRequest = {
        id: ++nextIdRef.current,
        options,
        returnFocus,
        resolve,
        settled: false,
        focusRestoreResolved: false,
      };
      queueRef.current.push(request);
      if (!activeRequestRef.current) activateNext();
    });
  }, [activateNext]);

  const settle = useCallback(
    (id: number, result: boolean) => {
      const request = activeRequestRef.current;
      if (!request || request.id !== id || request.settled) return;
      request.settled = true;
      lastSettledRequestRef.current = request;
      if (queueRef.current.length > 0) {
        skipReturnFocusRequestsRef.current.add(request);
      }
      request.resolve(result);
      activateNext();
    },
    [activateNext],
  );

  const finalFocus = useCallback(() => {
    if (activeRequest) {
      const request = activeRequest;
      queueMicrotask(() => {
        queueMicrotask(() => {
          request.focusRestoreResolved = true;
        });
      });
      if (skipReturnFocusRequestsRef.current.has(activeRequest)) {
        return false;
      }
      return activeRequest.returnFocus ?? true;
    }
    return true;
  }, [activeRequest]);

  useEffect(() => {
    isLiveRef.current = true;
    return () => {
      isLiveRef.current = false;
      const pending = [
        ...(activeRequestRef.current ? [activeRequestRef.current] : []),
        ...queueRef.current,
      ];
      activeRequestRef.current = null;
      queueRef.current = [];
      lastSettledRequestRef.current = null;
      for (const request of pending) {
        if (!request.settled) {
          request.settled = true;
          request.resolve(false);
        }
      }
    };
  }, []);

  const activeId = activeRequest?.id;

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <AlertDialog
        key={activeId ?? "closed"}
        open={activeRequest !== null}
        title={activeRequest?.options?.title}
        description={activeRequest?.options?.description}
        confirmLabel={activeRequest?.options?.confirmLabel}
        cancelLabel={activeRequest?.options?.cancelLabel}
        destructive={activeRequest?.options?.destructive}
        finalFocus={finalFocus}
        onConfirm={() => {
          if (activeId !== undefined) settle(activeId, true);
        }}
        onCancel={() => {
          if (activeId !== undefined) settle(activeId, false);
        }}
      />
    </ConfirmContext.Provider>
  );
}

/**
 * ConfirmProvider が無い環境（テストのスモーク描画など）向けのフォールバック。
 * ブラウザでは従来どおり window.confirm、それ以外では true を返す。
 */
const fallbackConfirm: ConfirmFn = (options) =>
  Promise.resolve(
    typeof window !== "undefined" && typeof window.confirm === "function"
      ? window.confirm(options?.description ?? options?.title ?? "")
      : true,
  );

/**
 * window.confirm の代替。`await confirm({ description })` で boolean を返す。
 * ConfirmProvider の外側では window.confirm へフォールバックする。
 */
export function useConfirm(): ConfirmFn {
  return useContext(ConfirmContext) ?? fallbackConfirm;
}
