"use client";

import type { Dispatch, SetStateAction } from "react";
import { ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { AppSelect } from "@/components/ui/app-select";
import type { ExternalModelPrivacySettings } from "./llm-model-section-types";

type Props = {
  value: ExternalModelPrivacySettings;
  onChange: Dispatch<SetStateAction<ExternalModelPrivacySettings>>;
  onSave: () => void | Promise<void>;
  saving?: boolean;
  localModelOptions?: Array<{ id: string; label: string; provider: string }>;
};

/** 外部送信ポリシーを Agent Team のモデル分担から分離して編集する。 */
export function LlmExternalPrivacySettings({ value, onChange, onSave, saving = false, localModelOptions = [] }: Props) {
  const update = <K extends keyof ExternalModelPrivacySettings>(key: K, next: ExternalModelPrivacySettings[K]) => {
    onChange((current) => ({ ...current, [key]: next }));
  };
  return (
    <Card size="sm" data-testid="external-model-privacy-settings">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <ShieldCheck className="size-4" />
          外部送信・機密保護
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 md:grid-cols-3">
          <label className="space-y-1 text-xs">
            <Label>外部送信モード</Label>
            <AppSelect
              aria-label="外部送信モード"
              value={value.mode ?? "direct"}
              disabled={saving}
              onChange={(event) => update("mode", event.target.value as ExternalModelPrivacySettings["mode"])}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
            >
              <option value="direct">通常</option>
              <option value="protected">保護クラウド</option>
              <option value="local_only">ローカル限定</option>
            </AppSelect>
          </label>
          <label className="space-y-1 text-xs">
            <Label>送信前確認</Label>
            <AppSelect
              aria-label="送信前確認"
              value={value.review_policy ?? "high_risk"}
              disabled={saving}
              onChange={(event) => update("review_policy", event.target.value as ExternalModelPrivacySettings["review_policy"])}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
            >
              <option value="never">しない</option>
              <option value="high_risk">リスクが高い時だけ</option>
              <option value="always">常に確認</option>
            </AppSelect>
          </label>
          <label className="space-y-1 text-xs">
            <Label>raw media</Label>
            <AppSelect
              aria-label="raw mediaの扱い"
              value={value.raw_media_policy ?? "block"}
              disabled={saving}
              onChange={(event) => update("raw_media_policy", event.target.value as ExternalModelPrivacySettings["raw_media_policy"])}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
            >
              <option value="block">外部送信しない</option>
              <option value="confirm">確認して送信</option>
            </AppSelect>
          </label>
        </div>
        <label className="flex items-center gap-2 text-xs">
          <Checkbox checked={value.semantic_redaction_enabled !== false} disabled={saving} onCheckedChange={(checked) => update("semantic_redaction_enabled", checked === true)} />
          意味ベースのマスキングを使う（trusted local sidecarのみ）
        </label>
        <label className="flex items-center gap-2 text-xs">
          <Checkbox checked={value.notify !== false} disabled={saving} onCheckedChange={(checked) => update("notify", checked === true)} />
          確認が必要な時に通知する
        </label>
        <div className="grid gap-3 md:grid-cols-2">
          <label className="space-y-1 text-xs">
            <Label>ローカルマスキングprovider</Label>
            <AppSelect
              aria-label="ローカルマスキングprovider"
              value={value.local_provider ?? "openai_compatible_local"}
              disabled={saving}
              onChange={(event) => update("local_provider", event.target.value)}
              className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
            >
              <option value="ollama">Ollama</option>
              <option value="sglang">SGLang</option>
              <option value="openai_compatible_local">OpenAI-compatible local</option>
            </AppSelect>
          </label>
          <label className="space-y-1 text-xs">
            <Label>ローカルマスキングモデル</Label>
            {localModelOptions.length > 0 ? (
              <AppSelect
                aria-label="ローカルマスキングモデル"
                value={value.local_model ?? ""}
                disabled={saving}
                onChange={(event) => update("local_model", event.target.value)}
                className="h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm"
              >
                <option value="">自動選択（provider設定）</option>
                {localModelOptions.map((option) => (
                  <option key={`${option.provider}:${option.id}`} value={option.id}>
                    {option.label || option.id} ({option.provider})
                  </option>
                ))}
              </AppSelect>
            ) : (
              <Input value={value.local_model ?? ""} disabled={saving} onChange={(event) => update("local_model", event.target.value)} placeholder="Ollama / SGLang のモデル名" className="h-8" />
            )}
          </label>
          <label className="space-y-1 text-xs">
            <Label>追加でマスクする語句（カンマ区切り）</Label>
            <Input value={(value.redaction_terms ?? []).join(", ")} disabled={saving} onChange={(event) => update("redaction_terms", event.target.value.split(",").map((item) => item.trim()).filter(Boolean))} className="h-8" />
          </label>
        </div>
        <label className="space-y-1 text-xs">
          <Label>信頼するローカルホスト（カンマ区切り）</Label>
          <Input value={(value.trusted_local_hosts ?? []).join(", ")} disabled={saving} onChange={(event) => update("trusted_local_hosts", event.target.value.split(",").map((item) => item.trim()).filter(Boolean))} placeholder="localhost, 192.168.0.10" className="h-8" />
        </label>
        <Button type="button" size="sm" onClick={() => void onSave()} disabled={saving}>外部送信設定を保存</Button>
      </CardContent>
    </Card>
  );
}
