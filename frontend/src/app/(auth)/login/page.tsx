"use client";

import { useMemo, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";

function LoginForm() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const searchParams = useSearchParams();
  const next = searchParams.get("next") ?? "";

  /*
  const searchError = useMemo(() => {
    const err = searchParams.get("error");
    if (err === "auth_failed") setError("認証に失敗しました");
    else if (err === "inactive") setError("アカウントが無効です");
    else if (err === "missing") setError("ユーザー名とパスワードが必要です");
  }, [searchParams]);
  */

  const searchError = useMemo(() => {
    const err = searchParams.get("error");
    if (err === "auth_failed") return "Authentication failed";
    if (err === "inactive") return "Account is inactive";
    if (err === "missing") return "Username and password are required";
    return "";
  }, [searchParams]);

  const handleLogin = () => {
    if (!username || !password) {
      setError("ユーザー名とパスワードを入力してください");
      return;
    }
    setError("");
    setLoading(true);

    // 通常のフォームPOSTで送信（fetchではなくブラウザのナビゲーションでCookieを確実に保存）
    const form = document.createElement("form");
    form.method = "POST";
    form.action = "/api/auth/login-form";

    const u = document.createElement("input");
    u.type = "hidden";
    u.name = "username";
    u.value = username;
    form.appendChild(u);

    const p = document.createElement("input");
    p.type = "hidden";
    p.name = "password";
    p.value = password;
    form.appendChild(p);

    if (next) {
      const n = document.createElement("input");
      n.type = "hidden";
      n.name = "next";
      n.value = next;
      form.appendChild(n);
    }

    document.body.appendChild(form);
    form.submit();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleLogin();
    }
  };

  return (
    <div className="space-y-4" onKeyDown={handleKeyDown}>
      <div className="space-y-2">
        <Label htmlFor="username">ユーザー名</Label>
        <Input
          id="username"
          type="text"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="password">パスワード</Label>
        <Input
          id="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      {(error || searchError) && (
        <p className="text-sm text-destructive">{error || searchError}</p>
      )}
      <Button className="w-full" disabled={loading} onClick={handleLogin}>
        {loading ? "ログイン中..." : "ログイン"}
      </Button>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Card className="w-full border border-border bg-card shadow-sm">
      <CardHeader className="items-center gap-3">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src="/images/ui/brand-orb.png"
          alt=""
          className="size-14 rounded-full object-cover ring-1 ring-border"
        />
        <CardTitle className="text-center text-2xl tracking-tight">
          AoiTalk
        </CardTitle>
      </CardHeader>
      <CardContent>
        <Suspense>
          <LoginForm />
        </Suspense>
      </CardContent>
    </Card>
  );
}
