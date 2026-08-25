"use client";

import { useEffect } from "react";
import { RefreshCw, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";

// 画面ごとのレンダー例外をここで受け止める。
// これが無いと Next の既定エラー画面に落ちて、利用者からは
// 「タブが開けない」としか見えず、原因も再試行手段も分からない。
export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("画面の描画に失敗しました", error);
  }, [error]);

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-md rounded-xl border bg-card p-6 text-center shadow-sm">
        <span className="mx-auto flex size-11 items-center justify-center rounded-full bg-destructive/10 text-destructive">
          <TriangleAlert className="size-5" />
        </span>
        <p className="mt-3 text-sm font-medium">この画面を表示できませんでした</p>
        <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
          一時的な問題の可能性があります。再読み込みしても直らない場合は、
          サーバーが起動しているかを確認してください。
        </p>
        {error.digest && (
          <p className="mt-2 font-mono text-[11px] text-muted-foreground">
            エラーID: {error.digest}
          </p>
        )}
        <Button type="button" size="sm" className="mt-4" onClick={reset}>
          <RefreshCw className="size-3.5" /> 再読み込み
        </Button>
      </div>
    </div>
  );
}
