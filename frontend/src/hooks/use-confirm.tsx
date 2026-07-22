"use client";

import {
  createContext,
  useCallback,
  useContext,
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

type ConfirmState = ConfirmOptions & { open: boolean };

/**
 * useConfirm を利用するためのプロバイダ。
 * アプリのレイアウト上位にマウントする。
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<ConfirmState>({ open: false });
  const resolverRef = useRef<((value: boolean) => void) | null>(null);

  const confirm = useCallback<ConfirmFn>((options) => {
    return new Promise<boolean>((resolve) => {
      resolverRef.current = resolve;
      setState({ ...options, open: true });
    });
  }, []);

  const settle = useCallback((result: boolean) => {
    const resolve = resolverRef.current;
    resolverRef.current = null;
    setState((prev) => ({ ...prev, open: false }));
    resolve?.(result);
  }, []);

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <AlertDialog
        open={state.open}
        title={state.title}
        description={state.description}
        confirmLabel={state.confirmLabel}
        cancelLabel={state.cancelLabel}
        destructive={state.destructive}
        onConfirm={() => settle(true)}
        onCancel={() => settle(false)}
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
