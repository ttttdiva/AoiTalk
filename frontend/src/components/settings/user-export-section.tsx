"use client";

import { useState, useCallback, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Download,
  Upload,
  ChevronDown,
  ChevronUp,
  Loader2,
  FileJson,
} from "lucide-react";

interface UserInfo {
  id: string;
  username: string;
  email: string | null;
  display_name: string | null;
  role: string | null;
  is_active: boolean | null;
  created_at: string | null;
}

async function apiFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

export function UserExportSection() {
  const [expanded, setExpanded] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [defaultPassword, setDefaultPassword] = useState("changeme123");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleExport = useCallback(async () => {
    setExporting(true);
    try {
      const users = await apiFetch<UserInfo[]>("/api/users");
      const exportData = {
        version: 1,
        exported_at: new Date().toISOString(),
        users: users.map((u) => ({
          username: u.username,
          email: u.email,
          display_name: u.display_name,
          role: u.role,
          is_active: u.is_active,
        })),
      };
      const blob = new Blob([JSON.stringify(exportData, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `users_export_${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(err instanceof Error ? err.message : "エクスポートに失敗しました");
    } finally {
      setExporting(false);
    }
  }, []);

  const handleImportFile = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;

      setImporting(true);
      setImportResult(null);
      setImportError(null);

      try {
        const text = await file.text();
        const data = JSON.parse(text);

        if (!data.users || !Array.isArray(data.users)) {
          throw new Error("無効なファイル形式です。users配列が必要です");
        }

        let created = 0;
        let skipped = 0;
        const errors: string[] = [];

        for (const user of data.users) {
          if (!user.username) {
            errors.push("usernameが空のエントリをスキップ");
            skipped++;
            continue;
          }
          try {
            await apiFetch("/api/users", {
              method: "POST",
              body: JSON.stringify({
                username: user.username,
                password: defaultPassword,
                role: user.role || "member",
                email: user.email || undefined,
                display_name: user.display_name || undefined,
              }),
            });
            created++;
          } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            if (msg.includes("exists") || msg.includes("既に")) {
              skipped++;
            } else {
              errors.push(`${user.username}: ${msg}`);
            }
          }
        }

        let result = `作成: ${created}件、スキップ: ${skipped}件`;
        if (errors.length > 0) {
          result += `\nエラー: ${errors.join(", ")}`;
        }
        setImportResult(result);
      } catch (err) {
        setImportError(
          err instanceof Error ? err.message : "インポートに失敗しました"
        );
      } finally {
        setImporting(false);
        // ファイル入力をリセット
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    },
    [defaultPassword]
  );

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setExpanded((v) => !v)}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <FileJson className="size-4" />
            ユーザーインポート/エクスポート
          </span>
          {expanded ? (
            <ChevronUp className="size-4" />
          ) : (
            <ChevronDown className="size-4" />
          )}
        </CardTitle>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-4">
          {/* エクスポート */}
          <div className="space-y-2">
            <Label className="text-xs font-medium">エクスポート</Label>
            <p className="text-xs text-muted-foreground">
              全ユーザー情報をJSONファイルとしてダウンロードします（パスワードは含まれません）
            </p>
            <Button
              variant="outline"
              size="sm"
              onClick={handleExport}
              disabled={exporting}
            >
              {exporting ? (
                <Loader2 className="size-3 animate-spin mr-1" />
              ) : (
                <Download className="size-3 mr-1" />
              )}
              エクスポート
            </Button>
          </div>

          {/* インポート */}
          <div className="space-y-2">
            <Label className="text-xs font-medium">インポート</Label>
            <p className="text-xs text-muted-foreground">
              JSONファイルからユーザーを一括作成します。既存ユーザーはスキップされます。
            </p>
            <div className="flex items-end gap-2">
              <div className="space-y-1">
                <Label className="text-[10px] text-muted-foreground">
                  初期パスワード
                </Label>
                <Input
                  value={defaultPassword}
                  onChange={(e) => setDefaultPassword(e.target.value)}
                  className="h-8 w-40"
                  placeholder="初期パスワード"
                />
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => fileInputRef.current?.click()}
                disabled={importing}
              >
                {importing ? (
                  <Loader2 className="size-3 animate-spin mr-1" />
                ) : (
                  <Upload className="size-3 mr-1" />
                )}
                JSONファイルを選択
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                accept=".json"
                className="hidden"
                onChange={handleImportFile}
              />
            </div>
            {importResult && (
              <p className="text-xs text-green-600 dark:text-green-400 whitespace-pre-line">
                {importResult}
              </p>
            )}
            {importError && (
              <p className="text-xs text-destructive">{importError}</p>
            )}
          </div>
        </CardContent>
      )}
    </Card>
  );
}
