"use client";

import { Suspense, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const DEFAULT_DESTINATION = "/chat";

/**
 * ログイン後の遷移先は同一オリジンのアプリ内パスだけを許可する。
 * 初期パスワード変更ページは通常のAppLayoutを通らないため、変更成功
 * 後のフルナビゲーションで新しいJWTを確実に読み込ませる。
 */
function safeDestination(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) {
    return DEFAULT_DESTINATION;
  }

  try {
    // 相対パスだけを受け付けるため、SSRでも利用できるダミーoriginで検証する。
    const url = new URL(raw, "http://aotalk.local");
    if (
      url.origin !== "http://aotalk.local" ||
      !url.pathname.startsWith("/") ||
      url.pathname.startsWith("//") ||
      url.pathname === "/login" ||
      url.pathname.startsWith("/api/") ||
      url.pathname.startsWith("/change-password")
    ) {
      return DEFAULT_DESTINATION;
    }
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return DEFAULT_DESTINATION;
  }
}

async function changePassword(newPassword: string): Promise<void> {
  const response = await fetch("/api/auth/change-password", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_password: newPassword }),
  });

  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    const message =
      detail && typeof detail === "object" && "detail" in detail
        ? String((detail as { detail?: unknown }).detail)
        : response.statusText;
    throw new Error(message || "パスワードの更新に失敗しました");
  }
}

function ChangePasswordForm() {
  const searchParams = useSearchParams();
  const destination = useMemo(
    () => safeDestination(searchParams.get("next")),
    [searchParams],
  );
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    setError("");
    if (password.length < 6) {
      setError("新しいパスワードは6文字以上必要です");
      return;
    }
    if (password !== confirmPassword) {
      setError("新しいパスワードが一致しません");
      return;
    }

    setLoading(true);
    try {
      await changePassword(password);
      // APIがSet-Cookieで再発行したJWTを使って通常画面を再取得する。
      window.location.assign(destination);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "パスワードの更新に失敗しました",
      );
      setLoading(false);
    }
  };

  return (
    <div className="space-y-4" onKeyDown={(event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void submit();
      }
    }}>
      <p className="text-sm text-muted-foreground">
        初回ログインのため、新しいパスワードを設定してください。
      </p>
      <div className="space-y-2">
        <Label htmlFor="new-password">新しいパスワード</Label>
        <Input
          id="new-password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoFocus
          autoComplete="new-password"
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="confirm-password">新しいパスワード（確認）</Label>
        <Input
          id="confirm-password"
          type="password"
          value={confirmPassword}
          onChange={(event) => setConfirmPassword(event.target.value)}
          autoComplete="new-password"
        />
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <Button className="w-full" disabled={loading} onClick={() => void submit()}>
        {loading ? "更新中..." : "パスワードを設定"}
      </Button>
    </div>
  );
}

export default function ChangePasswordPage() {
  return (
    <Card className="w-full border border-border bg-card shadow-sm">
      <CardHeader>
        <CardTitle className="text-center text-2xl">パスワード設定</CardTitle>
      </CardHeader>
      <CardContent>
        <Suspense>
          <ChangePasswordForm />
        </Suspense>
      </CardContent>
    </Card>
  );
}
