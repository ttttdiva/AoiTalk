"use client";

import type { Dispatch, SetStateAction } from "react";
import { Cloud, ShieldCheck, ShieldOff, Sparkles } from "lucide-react";

import { AppSelect } from "@/components/ui/app-select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type {
  CloudAdvisorProviderOption,
  CloudAdvisorSettings,
  ExternalModelPrivacySettings,
} from "./llm-model-section-types";

type Props = {
  value: CloudAdvisorSettings;
  onChange: Dispatch<SetStateAction<CloudAdvisorSettings>>;
  onSave: () => void | Promise<void>;
  saving?: boolean;
  privacy?: ExternalModelPrivacySettings | null;
  providerOptions?: CloudAdvisorProviderOption[];
  chatgptWeb?: {
    profile_dir?: string;
    response_timeout_seconds?: number;
    max_rounds_per_turn?: number;
  } | null;
};

const REASONING_EFFORTS = [
  ["none", "None"],
  ["minimal", "Minimal"],
  ["low", "Low"],
  ["medium", "Medium"],
  ["high", "High"],
  ["xhigh", "XHigh"],
  ["max", "Max"],
] as const;

function privacyModeLabel(
  mode: ExternalModelPrivacySettings["mode"] | undefined,
) {
  switch (mode) {
    case "local_only":
      return "ローカル限定";
    case "protected":
      return "保護クラウド";
    default:
      return "通常";
  }
}

function reviewPolicyLabel(
  policy: ExternalModelPrivacySettings["review_policy"] | undefined,
) {
  switch (policy) {
    case "never":
      return "確認しない";
    case "always":
      return "常に確認";
    default:
      return "高リスク時のみ";
  }
}

/**
 * Cloud Advisor is intentionally a first-class settings surface rather than
 * another Agent Team route.  This component only edits non-secret routing
 * fields and exposes privacy diagnostics; it never displays payloads or
 * credentials returned by the backend.
 */
export function LlmCloudAdvisorSettings({
  value,
  onChange,
  onSave,
  saving = false,
  privacy,
  providerOptions = [],
  chatgptWeb,
}: Props) {
  const mode = value.mode ?? "disabled";
  const provider = value.provider ?? providerOptions[0]?.id ?? "openai";
  const effort = value.reasoning_effort ?? "high";
  const privacyMode = privacy?.mode ?? "direct";
  const semanticMasking = privacy?.semantic_redaction_enabled !== false;
  const failClosed = privacyMode === "local_only";
  const isWebChatGPT = provider === "chatgpt-web" || provider === "chatgpt_web";
  const semanticConfigIncomplete = semanticMasking && !privacy?.local_model?.trim();
  const update = <K extends keyof CloudAdvisorSettings>(
    key: K,
    next: CloudAdvisorSettings[K],
  ) => onChange((current) => ({ ...current, [key]: next }));

  return (
    <Card size="sm" data-testid="cloud-advisor-settings">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Cloud className="size-4" />
          Cloud Advisor
          <Badge variant={mode === "disabled" ? "secondary" : "default"}>
            {mode === "disabled" ? "無効" : mode === "manual" ? "手動" : "自動"}
          </Badge>
        </CardTitle>
        <p className="text-[11px] text-muted-foreground">
          Main Agent専用の読み取り専用相談先です。Agent Team
          v3の構成や通常のモデル経路は変更しません。
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-2">
          <label className="space-y-1 text-xs">
            <Label>Cloud Advisorモード</Label>
            <AppSelect
              aria-label="Cloud Advisorモード"
              value={mode}
              disabled={saving}
              onChange={(event) =>
                update(
                  "mode",
                  event.target.value as CloudAdvisorSettings["mode"],
                )
              }
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
            >
              <option value="disabled">無効（送信しない）</option>
              <option value="manual">手動（明示操作のみ）</option>
              <option value="automatic">自動（親Agentの判定時のみ）</option>
            </AppSelect>
          </label>
          <label className="space-y-1 text-xs">
            <Label>Provider</Label>
            <AppSelect
              aria-label="Cloud Advisor provider"
              value={provider}
              disabled={saving || providerOptions.length === 0}
              onChange={(event) => update("provider", event.target.value)}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
            >
              {providerOptions.length > 0 ? (
                providerOptions.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))
              ) : (
                <option value={provider}>{provider}</option>
              )}
            </AppSelect>
            <p className="text-[10px] text-muted-foreground">
              利用可能な候補はbackendの設定schemaに従います。
            </p>
          </label>
          <label className="space-y-1 text-xs">
            <Label>モデル（空欄で既定）</Label>
            <Input
              aria-label="Cloud Advisor model"
              value={value.model ?? ""}
              disabled={saving}
              onChange={(event) => update("model", event.target.value)}
              placeholder="Providerの既定モデルを使用"
              className="h-8"
            />
          </label>
          <label className="space-y-1 text-xs">
            <Label>Reasoning effort</Label>
            <AppSelect
              aria-label="Cloud Advisor reasoning effort"
              value={effort}
              disabled={saving}
              onChange={(event) =>
                update("reasoning_effort", event.target.value)
              }
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
            >
              {REASONING_EFFORTS.map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </AppSelect>
          </label>
        </div>

        {isWebChatGPT && (
          <div
            className="space-y-2 rounded-md border border-border bg-muted/35 p-3"
            data-testid="cloud-advisor-web-settings"
          >
            <div className="text-xs font-medium">Web ChatGPT接続設定</div>
            <p className="text-[11px] text-muted-foreground">
              Cloud Advisorではテキスト相談だけを、既存の親専用ブラウザプロファイルから送信します。
              ログイン確認とプロファイル変更は Agent Team の Director設定と共有されます。
            </p>
            <dl className="grid gap-2 text-[11px] sm:grid-cols-3">
              <div className="rounded border border-border/70 bg-background/60 p-2">
                <dt className="text-muted-foreground">ブラウザプロファイル</dt>
                <dd className="truncate font-medium" title={chatgptWeb?.profile_dir || undefined}>
                  {chatgptWeb?.profile_dir || "未設定"}
                </dd>
              </div>
              <div className="rounded border border-border/70 bg-background/60 p-2">
                <dt className="text-muted-foreground">応答待ち時間</dt>
                <dd className="font-medium">
                  {chatgptWeb?.response_timeout_seconds ?? 900}秒
                </dd>
              </div>
              <div className="rounded border border-border/70 bg-background/60 p-2">
                <dt className="text-muted-foreground">ログイン/設定</dt>
                <dd className="font-medium">Director設定で確認</dd>
              </div>
            </dl>
          </div>
        )}

        <div
          className="space-y-2 rounded-md border border-border bg-muted/35 p-3"
          data-testid="cloud-advisor-privacy-diagnostics"
        >
          <div className="flex items-center gap-2 text-xs font-medium">
            {failClosed ? (
              <ShieldOff className="size-4 text-amber-600" />
            ) : (
              <ShieldCheck className="size-4 text-emerald-600" />
            )}
            送信プライバシー診断
          </div>
          <div className="grid gap-2 text-[11px] sm:grid-cols-3">
            <div className="rounded border border-border/70 bg-background/60 p-2">
              <span className="block text-muted-foreground">
                外部送信モード
              </span>
              <span className="font-medium">
                {privacyModeLabel(privacy?.mode)}
              </span>
            </div>
            <div className="rounded border border-border/70 bg-background/60 p-2">
              <span className="block text-muted-foreground">送信前確認</span>
              <span className="font-medium">
                {reviewPolicyLabel(privacy?.review_policy)}
              </span>
            </div>
            <div className="rounded border border-border/70 bg-background/60 p-2">
              <span className="block text-muted-foreground">
                意味ベースのマスキング
              </span>
              <span className="font-medium">
                {semanticMasking ? "有効" : "無効"}
              </span>
            </div>
          </div>
          {failClosed ? (
            <p
              role="alert"
              className="flex items-start gap-2 text-[11px] text-amber-700 dark:text-amber-400"
            >
              <ShieldOff className="mt-0.5 size-3.5 shrink-0" />
              ローカル限定のためCloud
              Advisorへの外部送信はfail-closedで拒否されます。モードを変更して保存するまで相談は実行されません。
            </p>
          ) : (
            <p className="flex items-start gap-2 text-[11px] text-muted-foreground">
              <Sparkles className="mt-0.5 size-3.5 shrink-0" />
              raw
              payloadや資格情報はこの画面に表示しません。送信内容は共通のOutbound
              Privacy Gatewayで確認・マスキングされます。
            </p>
          )}
          {semanticConfigIncomplete && !failClosed && (
            <p
              role="alert"
              className="text-[11px] text-amber-700 dark:text-amber-400"
            >
              専用のローカル意味マスキングモデルが未設定です。Protected送信は
              fail-closedになります。Privacy &amp; Advancedでlocal provider/modelと
              非推論プロファイルを設定してください。
            </p>
          )}
        </div>

        <Button
          type="button"
          size="sm"
          onClick={() => void onSave()}
          disabled={saving}
        >
          {saving ? "保存中..." : "Cloud Advisor設定を保存"}
        </Button>
      </CardContent>
    </Card>
  );
}
