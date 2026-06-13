import React, { useCallback, useEffect, useMemo, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import {
  Button,
  Dialog,
  Divider,
  List,
  Portal,
  RadioButton,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import { useRouter } from "expo-router";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import {
  DEFAULT_MOBILE_LLM_SETTINGS,
  getMobileLlmFallbackProvider,
  getMobileLlmSettings,
  getMobileLlmProfile,
  isDirectProvider,
  saveMobileLlmFallbackProvider,
  saveMobileLlmProfile,
  saveMobileLlmSettings,
  type DirectMobileLlmProvider,
  type MobileLlmFallbackProvider,
  type MobileLlmProvider,
} from "../../../lib/mobile-llm";
import {
  llmEngineApi,
  type LlmEngineOption,
  type LlmEngineState,
} from "../../../lib/llm-engine-api";
import { taskApi } from "../../../lib/task-api";
import { getCurrentVersion } from "../../../lib/update-service";
import {
  DEFAULT_AUDIO_PLAYER_SETTINGS,
  getAudioPlayerSettings,
  serializeAudioPlayerSettings,
  type AudioPlayerSettings,
} from "../../../lib/audio-player-settings";
import { useNetworkStore } from "../../../stores/network";
import type { UserSettings } from "../../../types/api";

type ServerFallbackMode = `server_fallback_${DirectMobileLlmProvider}`;
type ChatDeliveryMode = MobileLlmProvider | ServerFallbackMode;

const DIRECT_PROVIDER_LABELS: Record<DirectMobileLlmProvider, string> = {
  openai: "OpenAI",
  gemini: "Gemini",
  openai_compatible: "OpenAI互換API",
};

const DIRECT_PROVIDERS: DirectMobileLlmProvider[] = [
  "openai",
  "gemini",
  "openai_compatible",
];

const CHAT_DELIVERY_OPTIONS: Array<{
  value: ChatDeliveryMode;
  provider: MobileLlmProvider;
  fallbackProvider: MobileLlmFallbackProvider;
  label: string;
  description: string;
}> = [
  {
    value: "server",
    provider: "server",
    fallbackProvider: "off",
    label: "Serverだけ使う",
    description: "つながらない時はローカル保存。Directへ勝手に切り替えません。",
  },
  ...DIRECT_PROVIDERS.map((provider) => ({
    value: `server_fallback_${provider}` as ChatDeliveryMode,
    provider: "server" as MobileLlmProvider,
    fallbackProvider: provider as MobileLlmFallbackProvider,
    label: `Server優先 / 失敗時だけ${DIRECT_PROVIDER_LABELS[provider]}`,
    description: "普段はServer。Server送信が失敗した時だけ端末から直接送ります。",
  })),
  ...DIRECT_PROVIDERS.map((provider) => ({
    value: provider as ChatDeliveryMode,
    provider: provider as MobileLlmProvider,
    fallbackProvider: "off" as MobileLlmFallbackProvider,
    label: `${DIRECT_PROVIDER_LABELS[provider]}へ直接送る`,
    description: "Serverを使わず、この端末から直接送ります。",
  })),
];

function getChatDeliveryMode(
  provider: MobileLlmProvider,
  fallbackProvider: MobileLlmFallbackProvider,
): ChatDeliveryMode {
  if (provider === "server" && fallbackProvider !== "off") {
    return `server_fallback_${fallbackProvider}` as ChatDeliveryMode;
  }
  return provider;
}

function getChatDeliveryOption(mode: ChatDeliveryMode) {
  return (
    CHAT_DELIVERY_OPTIONS.find((option) => option.value === mode) ??
    CHAT_DELIVERY_OPTIONS[0]
  );
}

export default function SettingsScreen() {
  const router = useRouter();
  const { user, logout, isAuthenticated, isAnonymous } = useAuth();
  const {
    spaces,
    projects,
    selectedSpaceId,
    selectedProjectId,
    setSelectedSpaceId,
    setSelectedProjectId,
  } = useProject();
  const network = useNetworkStore();
  const [userSettings, setUserSettings] = useState<UserSettings>({});
  const [customInstructions, setCustomInstructions] = useState("");
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [audioPlayerSettings, setAudioPlayerSettings] =
    useState<AudioPlayerSettings>(DEFAULT_AUDIO_PLAYER_SETTINGS);
  const [audioSettingsSaving, setAudioSettingsSaving] = useState(false);
  const [audioSettingsDialogVisible, setAudioSettingsDialogVisible] =
    useState(false);
  const [chatProvider, setChatProvider] =
    useState<MobileLlmProvider>("server");
  const [pendingChatMode, setPendingChatMode] =
    useState<ChatDeliveryMode>("server");
  const [chatFallbackProvider, setChatFallbackProvider] =
    useState<MobileLlmFallbackProvider>("off");
  const [chatApiKey, setChatApiKey] = useState("");
  const [chatModel, setChatModel] = useState(DEFAULT_MOBILE_LLM_SETTINGS.model);
  const [chatBaseUrl, setChatBaseUrl] = useState(
    DEFAULT_MOBILE_LLM_SETTINGS.baseUrl,
  );
  const [chatSettingsSaved, setChatSettingsSaved] = useState(false);
  const [serverEngine, setServerEngine] = useState<LlmEngineState | null>(null);
  const [serverEngineSaving, setServerEngineSaving] = useState(false);
  const [serverEngineDialogVisible, setServerEngineDialogVisible] =
    useState(false);
  const [chatModeDialogVisible, setChatModeDialogVisible] = useState(false);
  const [scopeDialogVisible, setScopeDialogVisible] = useState(false);
  const [chatCredentialsDialogVisible, setChatCredentialsDialogVisible] =
    useState(false);

  const credentialProvider: DirectMobileLlmProvider | null = isDirectProvider(
    chatProvider,
  )
    ? chatProvider
    : chatFallbackProvider !== "off"
      ? chatFallbackProvider
      : null;

  useEffect(() => {
    void (async () => {
      const llmSettings = await getMobileLlmSettings();
      const fallbackProvider = await getMobileLlmFallbackProvider();
      setChatProvider(llmSettings.provider);
      setPendingChatMode(
        getChatDeliveryMode(llmSettings.provider, fallbackProvider),
      );
      setChatFallbackProvider(fallbackProvider);
      const profileProvider = isDirectProvider(llmSettings.provider)
        ? llmSettings.provider
        : fallbackProvider !== "off"
          ? fallbackProvider
          : null;
      const profile = profileProvider
        ? await getMobileLlmProfile(profileProvider)
        : await getMobileLlmProfile("server");
      setChatApiKey(profile.apiKey);
      setChatModel(profile.model);
      setChatBaseUrl(profile.baseUrl);
      if (!isAuthenticated) {
        return;
      }
      try {
        setServerEngine(await llmEngineApi.get());
        const settings = await taskApi.getUserSettings();
        setUserSettings(settings);
        setAudioPlayerSettings(getAudioPlayerSettings(settings));
        setCustomInstructions(settings.custom_instructions ?? "");
      } catch {
        // ignore
      }
    })();
  }, [isAuthenticated]);

  const handleLogout = async () => {
    await logout();
    router.replace("/(auth)/login");
  };

  const handleSaveUserSettings = async () => {
    setSettingsSaving(true);
    try {
      const next = await taskApi.updateUserSettings({
        ...userSettings,
        custom_instructions: customInstructions.trim(),
      });
      setUserSettings(next);
      setCustomInstructions(next.custom_instructions ?? "");
    } finally {
      setSettingsSaving(false);
    }
  };

  const updateAudioSettings = async (patch: Partial<AudioPlayerSettings>) => {
    const nextAudioSettings = { ...audioPlayerSettings, ...patch };
    setAudioPlayerSettings(nextAudioSettings);
    setAudioSettingsSaving(true);
    try {
      const next = await taskApi.updateUserSettings({
        ...userSettings,
        audio_player: serializeAudioPlayerSettings(nextAudioSettings),
      });
      setUserSettings(next);
      setAudioPlayerSettings(getAudioPlayerSettings(next));
    } finally {
      setAudioSettingsSaving(false);
    }
  };

  const handleSelectChatMode = useCallback(async (mode: ChatDeliveryMode) => {
    const option = getChatDeliveryOption(mode);
    const profileProvider = isDirectProvider(option.provider)
      ? option.provider
      : option.fallbackProvider !== "off"
        ? option.fallbackProvider
        : "server";
    const profile = await getMobileLlmProfile(profileProvider);
    await saveMobileLlmSettings({ provider: option.provider, ...profile });
    await saveMobileLlmFallbackProvider(option.fallbackProvider);
    setChatProvider(option.provider);
    setChatFallbackProvider(option.fallbackProvider);
    setChatApiKey(profile.apiKey);
    setChatModel(profile.model);
    setChatBaseUrl(profile.baseUrl);
  }, []);

  const openChatModeDialog = useCallback(() => {
    setPendingChatMode(getChatDeliveryMode(chatProvider, chatFallbackProvider));
    setChatModeDialogVisible(true);
  }, [chatFallbackProvider, chatProvider]);

  const applyPendingChatMode = useCallback(async () => {
    await handleSelectChatMode(pendingChatMode);
    setChatModeDialogVisible(false);
  }, [handleSelectChatMode, pendingChatMode]);

  const handleSaveChatLlmSettings = async () => {
    const profileProvider = credentialProvider ?? chatProvider;
    const profile = await getMobileLlmProfile(profileProvider);
    const model =
      chatModel.trim() || profile.model || DEFAULT_MOBILE_LLM_SETTINGS.model;
    const baseUrl =
      chatBaseUrl.trim() ||
      profile.baseUrl ||
      DEFAULT_MOBILE_LLM_SETTINGS.baseUrl;
    await saveMobileLlmSettings({
      provider: chatProvider,
      apiKey: chatApiKey,
      model,
      baseUrl,
    });
    await saveMobileLlmFallbackProvider(chatFallbackProvider);
    if (credentialProvider) {
      await saveMobileLlmProfile(credentialProvider, {
        apiKey: chatApiKey,
        model,
        baseUrl,
      });
    }
    setChatModel(model);
    setChatBaseUrl(baseUrl);
    setChatSettingsSaved(true);
    setTimeout(() => setChatSettingsSaved(false), 2000);
  };

  const handleSelectServerEngine = async (option: LlmEngineOption) => {
    setServerEngineSaving(true);
    try {
      const next = await llmEngineApi.set(option.provider, option.model);
      setServerEngine(next);
      setServerEngineDialogVisible(false);
    } finally {
      setServerEngineSaving(false);
    }
  };

  const currentServerEngineLabel = useMemo(() => {
    if (!serverEngine) {
      return isAuthenticated ? "取得中" : "サーバーログイン後に選択できます";
    }
    const selected = serverEngine.available?.find(
      (option) =>
        option.provider === serverEngine.provider &&
        option.model === serverEngine.model,
    );
    return selected?.label ?? `${serverEngine.model} (${serverEngine.provider})`;
  }, [isAuthenticated, serverEngine]);

  const audioSettingsSummary = useMemo(() => {
    const scope =
      audioPlayerSettings.playbackScope === "global_next"
        ? "フォルダ跨ぎ"
        : "同一フォルダ";
    const options = [
      audioPlayerSettings.shuffle ? "シャッフル" : null,
      audioPlayerSettings.repeatOne ? "1曲リピート" : null,
    ].filter(Boolean);
    return options.length > 0 ? `${scope} / ${options.join(" / ")}` : scope;
  }, [audioPlayerSettings]);

  const selectedScopeValue = selectedSpaceId
    ? `space:${selectedSpaceId}`
    : selectedProjectId
      ? `project:${selectedProjectId}`
      : "";

  const selectedScopeLabel = useMemo(() => {
    if (selectedSpaceId) {
      const space = spaces.find((item) => item.id === selectedSpaceId);
      return space ? `Space: ${space.name}` : "Selected space";
    }
    if (selectedProjectId) {
      const project = projects.find((item) => item.id === selectedProjectId);
      return project ? `Project: ${project.name}` : "Selected project";
    }
    return "All projects";
  }, [projects, selectedProjectId, selectedSpaceId, spaces]);

  const currentChatModeOption = useMemo(
    () =>
      getChatDeliveryOption(
        getChatDeliveryMode(chatProvider, chatFallbackProvider),
      ),
    [chatFallbackProvider, chatProvider],
  );

  const pendingChatModeOption = useMemo(
    () => getChatDeliveryOption(pendingChatMode),
    [pendingChatMode],
  );

  const credentialProviderLabel = credentialProvider
    ? DIRECT_PROVIDER_LABELS[credentialProvider]
    : null;
  const credentialsSummary = credentialProvider
    ? `${credentialProviderLabel} / ${chatApiKey.trim() ? "APIキーあり" : "APIキー未設定"}`
    : "Direct送信は未使用";

  const handleSelectScope = (value: string) => {
    if (value.startsWith("space:")) {
      setSelectedSpaceId(value.slice("space:".length));
    } else if (value.startsWith("project:")) {
      setSelectedProjectId(value.slice("project:".length));
    } else {
      setSelectedProjectId(null);
    }
    setScopeDialogVisible(false);
  };


  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Surface style={styles.header} elevation={1}>
        <Text variant="titleLarge" style={styles.headerTitle}>
          Settings
        </Text>
        <Text style={styles.headerSubtitle}>
          普段触る設定を上に、管理・接続・アプリ情報を下に分けています。
        </Text>
      </Surface>

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>アカウント</Text>
        <Surface style={styles.accountCard} elevation={0}>
          <View style={styles.accountHeaderRow}>
            <View style={styles.accountAvatar}>
              <Text style={styles.accountAvatarText}>
                {(user?.username || "A").slice(0, 1).toUpperCase()}
              </Text>
            </View>
            <View style={styles.accountMainText}>
              <Text style={styles.settingValue}>
                {user?.username || "Anonymous"}
              </Text>
              <Text style={styles.settingHint}>
                {isAuthenticated
                  ? `Server login${user?.role ? ` / ${user.role}` : ""}`
                  : isAnonymous
                    ? "Anonymous / local data only"
                    : "Signed out"}
              </Text>
            </View>
          </View>
          <View style={styles.accountActions}>
            <Button
              mode="outlined"
              compact
              style={styles.compactButton}
              onPress={() => router.push("/(tabs)/settings/profile")}
            >
              詳細を見る
            </Button>
            {isAuthenticated ? (
              <Button
                mode="text"
                compact
                textColor="#f38ba8"
                onPress={handleLogout}
              >
                ログアウト
              </Button>
            ) : (
              <Button
                mode="text"
                compact
                textColor="#89b4fa"
                onPress={() => router.push("/(auth)/login")}
              >
                ログイン
              </Button>
            )}
          </View>
        </Surface>
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Chat / AI</Text>
        <Text style={styles.helperText}>
          送信方法は1つだけ選びます。ServerとDirectを別々に操作する必要はありません。
        </Text>
        <Surface style={styles.chatRouteBox} elevation={0}>
          <View style={styles.chatRouteRow}>
            <View style={styles.chatRouteText}>
              <Text style={styles.settingLabel}>送信モード</Text>
              <Text style={styles.settingValue}>{currentChatModeOption.label}</Text>
              <Text style={styles.settingHint}>
                {currentChatModeOption.description}
              </Text>
            </View>
            <Button
              mode="outlined"
              compact
              style={styles.compactButton}
              onPress={openChatModeDialog}
            >
              変更
            </Button>
          </View>
        </Surface>
        {chatProvider === "server" ? (
          <Surface style={styles.serverEngineBox} elevation={0}>
            <Text style={styles.settingLabel}>Server model</Text>
            <Text style={styles.settingValue} numberOfLines={2}>
              {currentServerEngineLabel}
            </Text>
            <Button
              mode="outlined"
              compact
              style={[styles.compactButton, styles.engineChangeButton]}
              disabled={!isAuthenticated || !serverEngine?.available?.length}
              loading={serverEngineSaving}
              onPress={() => setServerEngineDialogVisible(true)}
            >
              変更
            </Button>
          </Surface>
        ) : null}
        <Surface style={styles.credentialsCard} elevation={0}>
          <View style={styles.chatRouteRow}>
            <View style={styles.chatRouteText}>
              <Text style={styles.settingLabel}>Direct送信用の資格情報</Text>
              <Text style={styles.settingValue}>{credentialsSummary}</Text>
            </View>
            <Button
              mode="outlined"
              compact
              style={styles.compactButton}
              disabled={!credentialProvider}
              onPress={() => setChatCredentialsDialogVisible(true)}
            >
              編集
            </Button>
          </View>
        </Surface>
        <Divider style={styles.innerDivider} />
        {isAuthenticated ? (
          <>
            <TextInput
              label="Custom Instructions"
              value={customInstructions}
              onChangeText={setCustomInstructions}
              mode="outlined"
              style={styles.input}
              multiline
              numberOfLines={5}
            />
            <Button
              mode="outlined"
              onPress={handleSaveUserSettings}
              loading={settingsSaving}
              style={styles.saveButton}
            >
              指示を保存
            </Button>
          </>
        ) : (
          <Text style={styles.helperText}>
            カスタム指示やユーザー設定の同期は、サーバーログイン中のみ利用できます。
          </Text>
        )}
        <Divider style={styles.innerDivider} />
        <List.Item
          title="Characters"
          titleStyle={styles.navTitle}
          description="会話キャラクターとデフォルトキャラクター。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="account-star-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/character")}
        />
        <List.Item
          title="User Memory"
          titleStyle={styles.navTitle}
          description="会話で使うユーザーメモリ。"
          descriptionStyle={styles.navDescription}
          left={(props) => <List.Icon {...props} icon="brain" color="#89b4fa" />}
          onPress={() => router.push("/(tabs)/settings/memory")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Confirmations and Permissions</Text>
        <Text style={styles.helperText}>
          ツール実行、外部送信、危険操作など、会話中に確認が必要になる操作をまとめます。
        </Text>
        <Surface style={styles.statusGrid} elevation={0}>
          <View style={styles.statusCell}>
            <Text style={styles.settingLabel}>Tool confirmations</Text>
            <Text style={styles.settingValue}>Timeline card</Text>
            <Text style={styles.settingHint}>
              チャット画面で Allow / Deny を追跡します。
            </Text>
          </View>
          <View style={styles.statusCell}>
            <Text style={styles.settingLabel}>Default policy</Text>
            <Text style={styles.settingValue}>Confirm</Text>
            <Text style={styles.settingHint}>
              セッション単位の常時許可は今後ここに集約します。
            </Text>
          </View>
        </Surface>
        <List.Item
          title="MCP & Agents"
          titleStyle={styles.navTitle}
          description="外部ツール、エージェント、連携先の接続状態。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="robot-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/mcp")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Jobs and Automation</Text>
        <Text style={styles.helperText}>
          Deep Research などの長時間ジョブは、通常メッセージではなく復帰可能な実行状態として扱います。
        </Text>
        <Surface style={styles.statusGrid} elevation={0}>
          <View style={styles.statusCell}>
            <Text style={styles.settingLabel}>Deep Research</Text>
            <Text style={styles.settingValue}>Report mode</Text>
            <Text style={styles.settingHint}>
              進捗カード、再入場後の復元、結果メッセージ化を前提にします。
            </Text>
          </View>
          <View style={styles.statusCell}>
            <Text style={styles.settingLabel}>Background behavior</Text>
            <Text style={styles.settingValue}>Restore on open</Text>
            <Text style={styles.settingHint}>
              active job は user/session から再取得する設計です。
            </Text>
          </View>
        </Surface>
        <List.Item
          title="Task notifications / Calendar"
          titleStyle={styles.navTitle}
          description="通知、カレンダー、作業予定に関する自動化。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="bell-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/notifications")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Audio Player</Text>
        <List.Item
          title="再生設定"
          titleStyle={styles.navTitle}
          description={audioSettingsSummary}
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="music-note-outline" color="#89b4fa" />
          )}
          onPress={() => setAudioSettingsDialogVisible(true)}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Data / Sync</Text>
        <Surface style={styles.statusGrid} elevation={0}>
          <View style={styles.statusCell}>
            <Text style={styles.settingLabel}>Internet</Text>
            <Text style={styles.settingValue}>
              {network.online ? "Online" : "Offline"}
            </Text>
            <Text style={styles.settingHint}>
              端末のネットワーク到達性です。
            </Text>
          </View>
          <View style={styles.statusCell}>
            <Text style={styles.settingLabel}>AoiTalk API</Text>
            <Text style={styles.settingValue}>
              {network.serverReachable ? "Reachable" : "Not verified"}
            </Text>
            <Text style={styles.settingHint}>
              未送信キューと履歴再取得の判断に使います。
            </Text>
          </View>
        </Surface>
        <List.Item
          title="User Memory"
          titleStyle={styles.navTitle}
          description="会話で使うユーザーメモリとローカル同期対象。"
          descriptionStyle={styles.navDescription}
          left={(props) => <List.Icon {...props} icon="brain" color="#89b4fa" />}
          onPress={() => router.push("/(tabs)/settings/memory")}
        />
        <List.Item
          title="アプリ情報 / 診断"
          titleStyle={styles.navTitle}
          description="バージョン、更新確認、診断情報。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="information-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/about")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Runtime</Text>
        <Text style={styles.helperText}>
          端末側の音声・通知と、サーバー側の実行環境を分けて確認します。
        </Text>
        <Surface style={styles.statusGrid} elevation={0}>
          <View style={styles.statusCell}>
            <Text style={styles.settingLabel}>Server model</Text>
            <Text style={styles.settingValue} numberOfLines={2}>
              {currentServerEngineLabel}
            </Text>
            <Text style={styles.settingHint}>Chat / AI で変更します。</Text>
          </View>
          <View style={styles.statusCell}>
            <Text style={styles.settingLabel}>Local voice / TTS</Text>
            <Text style={styles.settingValue}>Device feature</Text>
            <Text style={styles.settingHint}>
              ローカル音声機能は Runtime 配下に集約します。
            </Text>
          </View>
        </Surface>
        <List.Item
          title="Server / Network"
          titleStyle={styles.navTitle}
          description="API URLとWi-Fi別の接続先。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="lan-connect" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/connection")}
        />
        <List.Item
          title="外部サーバー接続"
          titleStyle={styles.navTitle}
          description="他のAoiTalkサーバーのタスクを表示・操作。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="server-network" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/remote-servers")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>作業範囲</Text>
        <Surface style={styles.scopeSummary} elevation={0}>
          <View style={styles.scopeSummaryText}>
            <Text style={styles.settingLabel}>現在の範囲</Text>
            <Text style={styles.settingValue} numberOfLines={2}>
              {selectedScopeLabel}
            </Text>
          </View>
          <Button
            mode="outlined"
            compact
            style={styles.compactButton}
            onPress={() => setScopeDialogVisible(true)}
          >
            変更
          </Button>
        </Surface>
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Tasks</Text>
        <Text style={styles.helperText}>
          タスクの通知タイミングとカレンダー連携です。
        </Text>
        <List.Item
          title="Task notifications / Calendar"
          titleStyle={styles.navTitle}
          description="開始前通知、Google Calendar、プロジェクト別タスク既定値。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="bell-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/notifications")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Projects</Text>
        <List.Item
          title="Projects"
          titleStyle={styles.navTitle}
          description="プロジェクトの作成・編集・メタデータ確認。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="folder-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/projects")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Time Tracking</Text>
        <List.Item
          title="作業時間レポート"
          titleStyle={styles.navTitle}
          description="記録した作業時間の集計。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="chart-bar" color="#89b4fa" />
          )}
          onPress={() => router.push("/reports")}
        />
        <List.Item
          title="時間順の記録"
          titleStyle={styles.navTitle}
          description="作業時間の記録と予定枠を時系列で確認。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="timeline-clock-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/report/timeline")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Other Features</Text>
        <List.Item
          title="Scenarios"
          titleStyle={styles.navTitle}
          description="シナリオ管理と執筆セッション。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="book-open-variant" color="#89b4fa" />
          )}
          onPress={() => router.push("/scenarios")}
        />
        <List.Item
          title="TRPG"
          titleStyle={styles.navTitle}
          description="TRPGルームの作成・参加。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon
              {...props}
              icon="dice-multiple-outline"
              color="#89b4fa"
            />
          )}
          onPress={() => router.push("/trpg")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>Connection</Text>
        <List.Item
          title="Server / Network"
          titleStyle={styles.navTitle}
          description="API URLとWi-Fi別の接続先。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="lan-connect" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/connection")}
        />
        <List.Item
          title="MCP & Agents"
          titleStyle={styles.navTitle}
          description="外部ツール連携とエージェント接続状態。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="robot-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/mcp")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>アプリ</Text>
        <List.Item
          title="アプリ情報"
          titleStyle={styles.navTitle}
          description="バージョン、更新確認、診断情報。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="information-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/about")}
        />
      </Surface>

      <Text style={styles.version}>AoiTalk Mobile v{getCurrentVersion()}</Text>
      <Portal>
        <Dialog
          visible={audioSettingsDialogVisible}
          onDismiss={() => setAudioSettingsDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Audio Player</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.settingHint}>
              曲終了時は常に次の曲を再生します。ここでは次曲の選び方だけ変更します。
            </Text>
            <RadioButton.Group
              value={audioPlayerSettings.playbackScope}
              onValueChange={(value) =>
                void updateAudioSettings({
                  playbackScope:
                    value === "global_next" ? "global_next" : "folder_loop",
                })
              }
            >
              <RadioButton.Item
                label="同じフォルダでループ"
                value="folder_loop"
                labelStyle={styles.radioLabel}
                color="#7c3aed"
                uncheckedColor="#585b70"
                disabled={audioSettingsSaving}
              />
              <RadioButton.Item
                label="フォルダを跨いで続ける"
                value="global_next"
                labelStyle={styles.radioLabel}
                color="#7c3aed"
                uncheckedColor="#585b70"
                disabled={audioSettingsSaving}
              />
            </RadioButton.Group>
            <Divider style={styles.innerDivider} />
            <View style={styles.switchRow}>
              <View style={styles.switchText}>
                <Text style={styles.settingLabel}>シャッフル</Text>
                <Text style={styles.settingHint}>
                  再生範囲内からランダムに選びます。
                </Text>
              </View>
              <Switch
                value={audioPlayerSettings.shuffle}
                disabled={audioSettingsSaving}
                onValueChange={(value) =>
                  void updateAudioSettings({ shuffle: value })
                }
              />
            </View>
            <Divider style={styles.innerDivider} />
            <View style={styles.switchRow}>
              <View style={styles.switchText}>
                <Text style={styles.settingLabel}>1曲リピート</Text>
                <Text style={styles.settingHint}>
                  同じ曲を繰り返し再生します。
                </Text>
              </View>
              <Switch
                value={audioPlayerSettings.repeatOne}
                disabled={audioSettingsSaving}
                onValueChange={(value) =>
                  void updateAudioSettings({ repeatOne: value })
                }
              />
            </View>
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setAudioSettingsDialogVisible(false)}>
              閉じる
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={chatModeDialogVisible}
          onDismiss={() => setChatModeDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>送信モード</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.settingValue}>{pendingChatModeOption.label}</Text>
            <Text style={styles.settingHint}>
              {pendingChatModeOption.description}
            </Text>
          </Dialog.Content>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              <RadioButton.Group
                value={pendingChatMode}
                onValueChange={(value) =>
                  setPendingChatMode(value as ChatDeliveryMode)
                }
              >
                {CHAT_DELIVERY_OPTIONS.map((option) => (
                  <RadioButton.Item
                    key={option.value}
                    label={option.label}
                    value={option.value}
                    labelStyle={styles.radioLabel}
                    color="#7c3aed"
                    uncheckedColor="#585b70"
                  />
                ))}
              </RadioButton.Group>
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button onPress={() => setChatModeDialogVisible(false)}>
              キャンセル
            </Button>
            <Button onPress={() => void applyPendingChatMode()}>適用</Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={chatCredentialsDialogVisible}
          onDismiss={() => setChatCredentialsDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Direct資格情報</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.settingHint}>
              Directを含む送信モードでだけ使います。Serverだけ使う場合は不要です。
            </Text>
            <TextInput
              label="APIキー"
              value={chatApiKey}
              onChangeText={setChatApiKey}
              mode="outlined"
              style={styles.dialogInput}
              secureTextEntry
              autoCapitalize="none"
              disabled={!credentialProvider}
            />
            <TextInput
              label="Model"
              value={chatModel}
              onChangeText={setChatModel}
              mode="outlined"
              style={styles.dialogInput}
              autoCapitalize="none"
              disabled={!credentialProvider}
            />
            <TextInput
              label="Base URL"
              value={chatBaseUrl}
              onChangeText={setChatBaseUrl}
              mode="outlined"
              style={styles.dialogInput}
              autoCapitalize="none"
              disabled={!credentialProvider}
            />
            {chatSettingsSaved ? (
              <Text style={styles.savedText}>Saved</Text>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setChatCredentialsDialogVisible(false)}>
              閉じる
            </Button>
            <Button
              disabled={!credentialProvider}
              onPress={() => void handleSaveChatLlmSettings()}
            >
              保存
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={serverEngineDialogVisible}
          onDismiss={() => setServerEngineDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Server model</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              <RadioButton.Group
                value={
                  serverEngine
                    ? `${serverEngine.provider}:${serverEngine.model}`
                    : ""
                }
                onValueChange={(value) => {
                  const option = serverEngine?.available?.find(
                    (item) => `${item.provider}:${item.model}` === value,
                  );
                  if (option) {
                    void handleSelectServerEngine(option);
                  }
                }}
              >
                {serverEngine?.available?.map((option) => (
                  <RadioButton.Item
                    key={`${option.provider}:${option.model}`}
                    label={option.label}
                    value={`${option.provider}:${option.model}`}
                    labelStyle={styles.radioLabel}
                    color="#7c3aed"
                    uncheckedColor="#585b70"
                    disabled={serverEngineSaving}
                  />
                ))}
              </RadioButton.Group>
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button onPress={() => setServerEngineDialogVisible(false)}>
              閉じる
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={scopeDialogVisible}
          onDismiss={() => setScopeDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Project / Space</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              <RadioButton.Group
                value={selectedScopeValue}
                onValueChange={handleSelectScope}
              >
                <RadioButton.Item
                  label="All projects"
                  value=""
                  labelStyle={styles.radioLabel}
                  color="#7c3aed"
                  uncheckedColor="#585b70"
                />
                {spaces.map((space) => (
                  <RadioButton.Item
                    key={space.id}
                    label={`Space: ${space.name}`}
                    value={`space:${space.id}`}
                    labelStyle={styles.radioLabel}
                    color="#7c3aed"
                    uncheckedColor="#585b70"
                  />
                ))}
                {projects.map((project) => (
                  <RadioButton.Item
                    key={project.id}
                    label={`Project: ${project.name}`}
                    value={`project:${project.id}`}
                    labelStyle={styles.radioLabel}
                    color="#7c3aed"
                    uncheckedColor="#585b70"
                  />
                ))}
              </RadioButton.Group>
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button onPress={() => setScopeDialogVisible(false)}>Close</Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { paddingBottom: 40 },
  header: { padding: 16, paddingTop: 56, backgroundColor: "#1e1e2e" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtitle: { color: "#a6adc8", fontSize: 13, lineHeight: 19, marginTop: 6 },
  section: { backgroundColor: "#11111b", padding: 16 },
  sectionTitle: {
    color: "#7c3aed",
    fontSize: 14,
    fontWeight: "bold",
    marginBottom: 12,
  },
  helperText: { color: "#a6adc8", fontSize: 13, lineHeight: 19 },
  accountCard: {
    backgroundColor: "#1e1e2e",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 12,
    padding: 14,
  },
  accountHeaderRow: { flexDirection: "row", alignItems: "center", gap: 12 },
  accountAvatar: {
    width: 42,
    height: 42,
    borderRadius: 21,
    backgroundColor: "#7c3aed",
    alignItems: "center",
    justifyContent: "center",
  },
  accountAvatarText: { color: "#f5f3ff", fontSize: 18, fontWeight: "bold" },
  accountMainText: { flex: 1 },
  accountActions: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginTop: 12,
  },
  radioLabel: { color: "#cdd6f4" },
  navTitle: { color: "#cdd6f4" },
  navDescription: { color: "#a6adc8" },
  divider: { backgroundColor: "#313244" },
  innerDivider: { backgroundColor: "#313244", marginVertical: 16 },
  accordion: { backgroundColor: "transparent", padding: 0 },
  loader: { paddingVertical: 16 },
  input: { marginBottom: 8 },
  saveButton: { borderColor: "#7c3aed" },
  serverEngineBox: {
    backgroundColor: "#1e1e2e",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
    marginBottom: 12,
  },
  settingLabel: { color: "#a6adc8", fontSize: 12, marginBottom: 4 },
  settingValue: { color: "#cdd6f4", fontSize: 15, fontWeight: "600" },
  settingHint: { color: "#a6adc8", fontSize: 12, marginTop: 3 },
  chatRouteBox: {
    backgroundColor: "#1e1e2e",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
    marginBottom: 12,
  },
  credentialsCard: {
    backgroundColor: "#181825",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
    marginBottom: 12,
  },
  chatRouteRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  chatRouteText: { flex: 1 },
  scopeSummary: {
    backgroundColor: "#1e1e2e",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  scopeSummaryText: { flex: 1 },
  statusGrid: {
    backgroundColor: "transparent",
    flexDirection: "row",
    gap: 10,
    marginTop: 12,
    marginBottom: 8,
  },
  statusCell: {
    flex: 1,
    backgroundColor: "#1e1e2e",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
  },
  switchRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
  },
  switchText: { flex: 1 },
  compactButton: { borderColor: "#7c3aed", alignSelf: "flex-start" },
  engineChangeButton: { marginTop: 10 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogInput: { marginTop: 12 },
  savedText: { color: "#a6e3a1", marginTop: 10, fontSize: 13 },
  dialogScrollArea: { maxHeight: 420, borderColor: "#313244" },
  dialogScrollContent: { paddingVertical: 4 },
  inlineButton: { marginTop: 8, borderColor: "#7c3aed" },
  version: {
    color: "#585b70",
    textAlign: "center",
    marginTop: 24,
    fontSize: 12,
  },
});
