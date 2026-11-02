"use client";

import { useCallback, useState, type ReactNode, type RefObject } from "react";
import { AppSelect, type AppSelectChangeEvent, type AppSelectOpenChangeDetails } from "@/components/ui/app-select";
import { HelpPopover } from "@/components/ui/help-popover";
import { toast } from "sonner";
import type { RuntimeContextValue } from "@/contexts/runtime-context";
import type { UserSettings } from "@/lib/user-settings";
import { cn } from "@/lib/utils";
import { displayModelOptionLabel } from "@/lib/local-runtime-status";
import { chatApi } from "@/lib/chat-api";

const runtimeSelectClassName =
  "h-8 min-h-8 border-input bg-background text-xs text-foreground shadow-none";

export type ChatModelSettingsFieldId =
  | "provider"
  | "model";

type ChatModelSettingsRouteProps = {
  catalogProviders: Array<{
    id: string;
    label?: string;
    models?: Array<{ id: string; label?: string }>;
    chat_status?: {
      label?: string;
      selectable?: boolean;
      message?: string;
      required_configuration?: string;
      latest_request_id?: string;
      latest_request_state?: string;
    };
  }>;
  effectiveProvider: string;
  effectiveModel: string;
  modelOptions: Array<{
    id: string;
    label?: string;
    installed?: boolean;
    source_label?: string;
  }>;
  providerDisabled: boolean;
  modelDisabled: boolean;
  settingsLoading: boolean;
  updateProvider: (provider: string) => void;
  updateModel: (model: string) => void;
};

type ChatModelSettingsFieldsProps = {
  runtime: RuntimeContextValue;
  userSettings: UserSettings | null | undefined;
  fixedDeployment: boolean;
  modelBusy: boolean;
  route: ChatModelSettingsRouteProps;
  settingsItemProps: (
    id: ChatModelSettingsFieldId,
  ) => {
    onFocus: () => void;
    onKeyDownCapture: (event: React.KeyboardEvent<HTMLButtonElement>) => void;
    onContentKeyDownCapture: (event: React.KeyboardEvent<HTMLDivElement>) => void;
    open: boolean;
    onOpenChange: (open: boolean, details?: AppSelectOpenChangeDetails) => void;
    container?: RefObject<HTMLElement | null> | HTMLElement | null;
  };
  innerOpen: string | null;
  effortSlot?: ReactNode;
};

export function ChatModelSettingsFields({
  fixedDeployment,
  modelBusy,
  route,
  settingsItemProps,
  innerOpen,
  effortSlot,
}: ChatModelSettingsFieldsProps) {
  const {
    catalogProviders,
    effectiveProvider,
    effectiveModel,
    modelOptions,
    providerDisabled,
    modelDisabled,
    settingsLoading,
    updateProvider,
    updateModel,
  } = route;
  const chatStatusProviders = catalogProviders.filter(
    (provider) => provider.chat_status?.selectable === false,
  );
  const selectedChatStatus = catalogProviders.find(
    (provider) => provider.id === effectiveProvider,
  )?.chat_status;
  const dedicatedDotsRoute = effectiveProvider.trim().toLowerCase() === "gpt-dots";
  const [cancelingDotsRequestId, setCancelingDotsRequestId] = useState<string | null>(null);
  const [cancelledDotsRequestId, setCancelledDotsRequestId] = useState<string | null>(null);
  const dotsRequestId = selectedChatStatus?.latest_request_id ?? null;
  const dotsRequestState = selectedChatStatus?.latest_request_state?.toLowerCase() ?? "";
  const dotsRequestCanCancel = Boolean(
    dedicatedDotsRoute &&
      dotsRequestId &&
      ["pending", "delivery_accepted", "read"].includes(dotsRequestState) &&
      cancelledDotsRequestId !== dotsRequestId,
  );
  const dotsRequestCancelPending = Boolean(
    cancelledDotsRequestId === dotsRequestId &&
      ["pending", "delivery_accepted", "read"].includes(dotsRequestState),
  );

  const handleProviderChange = useCallback(
    (event: AppSelectChangeEvent) => {
      updateProvider(event.target.value);
    },
    [updateProvider],
  );

  const handleModelChange = useCallback(
    (event: AppSelectChangeEvent) => {
      updateModel(event.target.value);
    },
    [updateModel],
  );

  const handleCancelDotsRequest = async () => {
    if (!dotsRequestId || !dotsRequestCanCancel || cancelingDotsRequestId) return;
    setCancelingDotsRequestId(dotsRequestId);
    try {
      await chatApi.cancelDotsDialogueRequest(dotsRequestId);
      setCancelledDotsRequestId(dotsRequestId);
    } catch {
      toast.error("GPT dots依頼をキャンセルできませんでした。状態を再確認してください。");
    } finally {
      setCancelingDotsRequestId(null);
    }
  };

  const providerSelectDisabled = modelBusy || providerDisabled || settingsLoading;
  const modelSelectDisabled = modelBusy || modelDisabled || settingsLoading;

  return (
    <div className="space-y-3">
      {fixedDeployment ? null : catalogProviders.length > 0 ? (
        <>
          <div className="grid gap-1 text-xs text-muted-foreground">
            <div className="flex items-center gap-1">
              <span>Provider</span>
              {chatStatusProviders.length > 0 ? (
                <HelpPopover label="Chat Provider">
                  <div className="space-y-2">
                    <p>
                      Chatに表示される外部プロバイダーの接続状態です。未接続の項目は選択・送信できません。
                    </p>
                    {chatStatusProviders.map((provider) => (
                      <p key={provider.id}>
                        <strong>{provider.label || provider.id}</strong>: {" "}
                        {provider.chat_status?.message} 必要な設定: {" "}
                        {provider.chat_status?.required_configuration}
                      </p>
                    ))}
                  </div>
                </HelpPopover>
              ) : null}
            </div>
            <AppSelect
              aria-label="Provider"
              data-chat-settings-item="provider"
              value={effectiveProvider}
              onChange={handleProviderChange}
              disabled={providerSelectDisabled}
              className={cn(runtimeSelectClassName, "w-full")}
              {...settingsItemProps("provider")}
              open={innerOpen === "provider"}
            >
              {catalogProviders.map((provider) => (
                <option
                  key={provider.id}
                  value={provider.id}
                  disabled={provider.chat_status?.selectable === false}
                  title={provider.chat_status?.message}
                >
                  {provider.label || provider.id}
                  {provider.chat_status?.selectable === false
                    ? `（${provider.chat_status.label || "利用不可"}）`
                    : ""}
                </option>
              ))}
            </AppSelect>
            {selectedChatStatus?.message ? (
              <p
                className="text-[11px] text-amber-700 dark:text-amber-300"
                role="status"
              >
                {dotsRequestCancelPending
                  ? "GPT dots依頼のキャンセルを受け付けました。状態を確認しています。"
                  : selectedChatStatus.message}
              </p>
            ) : null}
            {dotsRequestCanCancel ? (
              <button
                type="button"
                className="text-left text-[11px] text-muted-foreground underline underline-offset-2 disabled:opacity-60"
                aria-label="GPT dots依頼をキャンセル"
                onClick={() => void handleCancelDotsRequest()}
                disabled={cancelingDotsRequestId === dotsRequestId}
              >
                {cancelingDotsRequestId === dotsRequestId
                  ? "キャンセル中…"
                  : "このGPT dots依頼をキャンセル"}
              </button>
            ) : null}
          </div>
          {dedicatedDotsRoute ? null : (
            <label className="grid gap-1 text-xs text-muted-foreground">
              <span>Model</span>
              <AppSelect
                aria-label="Model"
                data-chat-settings-item="model"
                value={effectiveModel}
                onChange={handleModelChange}
                disabled={modelSelectDisabled || !effectiveProvider}
                className={cn(runtimeSelectClassName, "w-full")}
                {...settingsItemProps("model")}
                open={innerOpen === "model"}
              >
                {modelOptions.map((model) => (
                  <option key={model.id} value={model.id}>
                    {displayModelOptionLabel(model)}
                  </option>
                ))}
              </AppSelect>
            </label>
          )}
        </>
      ) : null}

      {dedicatedDotsRoute ? null : effortSlot}

    </div>
  );
}
