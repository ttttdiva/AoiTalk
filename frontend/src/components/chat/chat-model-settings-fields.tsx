"use client";

import { useCallback, type ReactNode, type RefObject } from "react";
import { AppSelect, type AppSelectChangeEvent, type AppSelectOpenChangeDetails } from "@/components/ui/app-select";
import {
  AGENT_TEAM_VALUE_AUTO,
  AGENT_TEAM_VALUE_FREE_TEAM,
} from "@/hooks/use-chat-session-route";
import type {
  AgentTeamExecutionProfileOption,
  AgentTeamOption,
} from "@/lib/chat-llm-settings";
import type { RuntimeContextValue } from "@/contexts/runtime-context";
import type { UserSettings } from "@/lib/user-settings";
import { cn } from "@/lib/utils";

const runtimeSelectClassName =
  "h-8 min-h-8 border-input bg-background text-xs text-foreground shadow-none";

export type ChatModelSettingsFieldId =
  | "agentTeam"
  | "provider"
  | "model"
  | "executionProfile";

type ChatModelSettingsRouteProps = {
  agentTeamOptions: AgentTeamOption[];
  agentTeamSelectorValue: string;
  agentTeamDisabled: boolean;
  executionProfileId: string;
  executionProfileOptions: AgentTeamExecutionProfileOption[];
  executionProfileDisabled: boolean;
  catalogProviders: Array<{
    id: string;
    label?: string;
    models?: Array<{ id: string; label?: string }>;
  }>;
  effectiveProvider: string;
  effectiveModel: string;
  modelOptions: Array<{ id: string; label?: string }>;
  providerDisabled: boolean;
  modelDisabled: boolean;
  settingsLoading: boolean;
  updateAgentTeamValue: (value: string) => void;
  updateExecutionProfile: (value: string) => void;
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
    agentTeamOptions,
    agentTeamSelectorValue,
    agentTeamDisabled,
    executionProfileId,
    executionProfileOptions,
    executionProfileDisabled,
    catalogProviders,
    effectiveProvider,
    effectiveModel,
    modelOptions,
    providerDisabled,
    modelDisabled,
    settingsLoading,
    updateAgentTeamValue,
    updateExecutionProfile,
    updateProvider,
    updateModel,
  } = route;

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

  const handleAgentTeamChange = useCallback(
    (event: AppSelectChangeEvent) => {
      updateAgentTeamValue(event.target.value);
    },
    [updateAgentTeamValue],
  );

  const handleExecutionProfileChange = useCallback(
    (event: AppSelectChangeEvent) => {
      updateExecutionProfile(event.target.value);
    },
    [updateExecutionProfile],
  );

  const providerSelectDisabled = modelBusy || providerDisabled || settingsLoading;
  const modelSelectDisabled = modelBusy || modelDisabled || settingsLoading;

  return (
    <div className="space-y-3">
      {fixedDeployment ? null : catalogProviders.length > 0 ? (
        <>
          <label className="grid gap-1 text-xs text-muted-foreground">
            <span>Provider</span>
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
                <option key={provider.id} value={provider.id}>
                  {provider.label || provider.id}
                </option>
              ))}
            </AppSelect>
          </label>
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
                  {model.label || model.id}
                </option>
              ))}
            </AppSelect>
          </label>
        </>
      ) : null}

      {effortSlot}

      <label className="grid gap-1 text-xs text-muted-foreground">
        <span>Agent Team</span>
        <AppSelect
          aria-label="Agent Team"
          data-chat-settings-item="agentTeam"
          value={agentTeamSelectorValue}
          onChange={handleAgentTeamChange}
          disabled={agentTeamDisabled}
          className={cn(runtimeSelectClassName, "w-full")}
          {...settingsItemProps("agentTeam")}
          open={innerOpen === "agentTeam"}
        >
          <option value={AGENT_TEAM_VALUE_AUTO}>Auto</option>
          <option value={AGENT_TEAM_VALUE_FREE_TEAM}>Free Team</option>
          {agentTeamOptions.map((team) => (
            <option key={team.team_id} value={team.team_id}>
              {team.name || team.team_id}
            </option>
          ))}
        </AppSelect>
      </label>

      <label className="grid gap-1 text-xs text-muted-foreground">
        <span>Execution Profile</span>
        <AppSelect
          aria-label="Execution Profile"
          data-chat-settings-item="executionProfile"
          value={executionProfileId}
          onChange={handleExecutionProfileChange}
          disabled={executionProfileDisabled}
          className={cn(runtimeSelectClassName, "w-full")}
          {...settingsItemProps("executionProfile")}
          open={innerOpen === "executionProfile"}
        >
          <option value="">None</option>
          {executionProfileOptions.map((profile) => (
            <option key={profile.profile_id} value={profile.profile_id}>
              {profile.name || profile.profile_id}
            </option>
          ))}
        </AppSelect>
      </label>
    </div>
  );
}
