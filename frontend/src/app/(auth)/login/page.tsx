"use client";

import { useMemo, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { AppSelect } from "@/components/ui/app-select";
import { BlurFade } from "@/components/magicui/blur-fade";
import { BorderBeam } from "@/components/magicui/border-beam";

type CredentialSource = "local" | "active_directory";

function LoginForm() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [credentialSource, setCredentialSource] =
    useState<CredentialSource>("local");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const searchParams = useSearchParams();
  const next = searchParams.get("next") ?? "";

  const searchError = useMemo(() => {
    const err = searchParams.get("error");
    if (err === "auth_failed") return "Authentication failed";
    if (err === "auth_unavailable") {
      return "認証サービスを利用できません。管理者にお問い合わせください。";
    }
    if (err === "auth_conflict") {
      return "アカウントを安全に作成できませんでした。管理者にお問い合わせください。";
    }
    if (err === "inactive") return "Account is inactive";
    if (err === "missing") return "Username and password are required";
    if (err === "audit_unavailable") {
      return "認証監査サービスを利用できません。管理者にお問い合わせください。";
    }
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

    const source = document.createElement("input");
    source.type = "hidden";
    source.name = "credential_source";
    source.value = credentialSource;
    form.appendChild(source);

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
          autoComplete="username"
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="password">パスワード</Label>
        <Input
          id="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="credential-source">認証方式</Label>
        <AppSelect
          id="credential-source"
          value={credentialSource}
          onChange={(event) =>
            setCredentialSource(event.target.value as CredentialSource)
          }
          className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50"
        >
          <option value="local">ローカル</option>
          <option value="active_directory">Active Directory (AD自動生成)</option>
        </AppSelect>
        <p className="text-xs text-muted-foreground">
          Active Directory を選ぶと、初回成功時にADの識別子へローカル利用者を安全に紐付けます。
        </p>
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
    <BlurFade className="w-full" duration={0.3} blur="4px" offset={8}>
      <Card className="relative w-full overflow-hidden border border-border bg-card shadow-sm">
        <BorderBeam
          className="pointer-events-none"
          duration={10}
          size={80}
          borderWidth={1}
          colorFrom="var(--primary)"
          colorTo="var(--chart-2)"
        />
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
    </BlurFade>
  );
}
