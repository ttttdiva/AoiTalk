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
  type MobileLlmProfile,
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
import type { UserSettings } from "../../../types/api";

type ModelSlot = "main" | "fallback";

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

const EMPTY_DIRECT_PROFILES: Record<DirectMobileLlmProvider, MobileLlmProfile> = {
  openai: { apiKey: "", model: "gpt-4o-mini", baseUrl: "https://api.openai.com/v1" },
  gemini: {
    apiKey: "",
    model: "gemini-1.5-flash",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta",
  },
  openai_compatible: {
    apiKey: "",
    model: "gpt-4o-mini",
    baseUrl: "https://api.openai.com/v1",
  },
};

function isFallbackDirectProvider(
  provider: MobileLlmFallbackProvider,
): provider is DirectMobileLlmProvider {
  return provider !== "off";
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
  const [userSettings, setUserSettings] = useState<UserSettings>({});
  const [customInstructions, setCustomInstructions] = useState("");
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [customInstructionsDialogVisible, setCustomInstructionsDialogVisible] =
    useState(false);
  const [audioPlayerSettings, setAudioPlayerSettings] =
    useState<AudioPlayerSettings>(DEFAULT_AUDIO_PLAYER_SETTINGS);
  const [audioSettingsSaving, setAudioSettingsSaving] = useState(false);
  const [audioSettingsDialogVisible, setAudioSettingsDialogVisible] =
    useState(false);
  const [chatProvider, setChatProvider] =
    useState<MobileLlmProvider>("server");
  const [chatFallbackProvider, setChatFallbackProvider] =
    useState<MobileLlmFallbackProvider>("off");
  const [modelSlotDialogVisible, setModelSlotDialogVisible] =
    useState<ModelSlot | null>(null);
  const [editingCredentialProvider, setEditingCredentialProvider] =
    useState<DirectMobileLlmProvider | null>(null);
  const [directProfiles, setDirectProfiles] =
    useState<Record<DirectMobileLlmProvider, MobileLlmProfile>>(
      EMPTY_DIRECT_PROFILES,
    );
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
  const [scopeDialogVisible, setScopeDialogVisible] = useState(false);
  const [chatCredentialsDialogVisible, setChatCredentialsDialogVisible] =
    useState(false);

  const loadDirectProfiles = useCallback(async () => {
    const entries = await Promise.all(
      DIRECT_PROVIDERS.map(async (provider) => {
        const profile = await getMobileLlmProfile(provider);
        return [provider, profile] as const;
      }),
    );
    const next = { ...EMPTY_DIRECT_PROFILES };
    for (const [provider, profile] of entries) {
      next[provider] = profile;
    }
    setDirectProfiles(next);
    return next;
  }, []);

  useEffect(() => {
    void (async () => {
      const llmSettings = await getMobileLlmSettings();
      const fallbackProvider = await getMobileLlmFallbackProvider();
      setChatProvider(llmSettings.provider);
      setChatFallbackProvider(fallbackProvider);
      await loadDirectProfiles();
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
  }, [isAuthenticated, loadDirectProfiles]);

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
      setCustomInstructionsDialogVisible(false);
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

  const handleSelectMainProvider = useCallback(
    async (provider: MobileLlmProvider) => {
      const profile = await getMobileLlmProfile(provider);
      await saveMobileLlmSettings({ provider, ...profile });
      setChatProvider(provider);

      if (isDirectProvider(provider) && chatFallbackProvider === provider) {
        await saveMobileLlmFallbackProvider("off");
        setChatFallbackProvider("off");
      }

      if (isDirectProvider(provider)) {
        setDirectProfiles((prev) => ({ ...prev, [provider]: profile }));
      }
      setModelSlotDialogVisible(null);
    },
    [chatFallbackProvider],
  );

  const handleSelectFallbackProvider = useCallback(
    async (provider: MobileLlmFallbackProvider) => {
      const next =
        isFallbackDirectProvider(provider) && provider === chatProvider
          ? "off"
          : provider;
      await saveMobileLlmFallbackProvider(next);
      setChatFallbackProvider(next);
      if (isFallbackDirectProvider(next)) {
        const profile = await getMobileLlmProfile(next);
        setDirectProfiles((prev) => ({ ...prev, [next]: profile }));
      }
      setModelSlotDialogVisible(null);
    },
    [chatProvider],
  );

  const openCredentialsDialog = useCallback(
    async (provider: DirectMobileLlmProvider) => {
      const profile = await getMobileLlmProfile(provider);
      setDirectProfiles((prev) => ({ ...prev, [provider]: profile }));
      setEditingCredentialProvider(provider);
      setChatApiKey(profile.apiKey);
      setChatModel(profile.model);
      setChatBaseUrl(profile.baseUrl);
      setChatSettingsSaved(false);
      setChatCredentialsDialogVisible(true);
    },
    [],
  );

  const handleSaveChatLlmSettings = async () => {
    const provider = editingCredentialProvider;
    if (!provider) return;
    const profile = await getMobileLlmProfile(provider);
    const model =
      chatModel.trim() || profile.model || DEFAULT_MOBILE_LLM_SETTINGS.model;
    const baseUrl =
      chatBaseUrl.trim() ||
      profile.baseUrl ||
      DEFAULT_MOBILE_LLM_SETTINGS.baseUrl;
    await saveMobileLlmProfile(provider, {
      apiKey: chatApiKey,
      model,
      baseUrl,
    });
    if (chatProvider === provider) {
      await saveMobileLlmSettings({
        provider: chatProvider,
        apiKey: chatApiKey,
        model,
        baseUrl,
      });
    }
    setDirectProfiles((prev) => ({
      ...prev,
      [provider]: {
        apiKey: chatApiKey.trim(),
        model,
        baseUrl,
      },
    }));
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

  const modelSlotSummary = useCallback(
    (provider: MobileLlmProvider | MobileLlmFallbackProvider) => {
      if (provider === "off") return "なし";
      if (provider === "server") return `Server / ${currentServerEngineLabel}`;
      const profile = directProfiles[provider];
      const model = profile?.model?.trim() || "モデル未設定";
      const keyStatus = profile?.apiKey?.trim() ? "APIキーあり" : "APIキー未設定";
      return `${DIRECT_PROVIDER_LABELS[provider]} / ${model} / ${keyStatus}`;
    },
    [currentServerEngineLabel, directProfiles],
  );

  const customInstructionsSummary = customInstructions.trim()
    ? "設定済み"
    : "未設定";

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
        <Text style={styles.sectionTitle}>会話・AI応答</Text>
        <Text style={styles.helperText}>
          通常使うメインモデルと、失敗時だけ使うフォールバックモデルを指定します。
        </Text>
        <View style={styles.modelSlotList}>
          <Surface style={styles.modelSlotCard} elevation={0}>
            <View style={styles.modelSlotHeader}>
              <View style={styles.modelSlotText}>
                <Text style={styles.settingLabel}>メイン</Text>
                <Text style={styles.settingValue} numberOfLines={2}>
                  {modelSlotSummary(chatProvider)}
                </Text>
                <Text style={styles.settingHint}>通常の会話応答で使います。</Text>
              </View>
              <Button
                mode="outlined"
                compact
                style={styles.compactButton}
                onPress={() => setModelSlotDialogVisible("main")}
              >
                選択
              </Button>
            </View>
            <View style={styles.slotActions}>
              {chatProvider === "server" ? (
                <Button
                  mode="text"
                  compact
                  textColor="#89b4fa"
                  disabled={!isAuthenticated || !serverEngine?.available?.length}
                  loading={serverEngineSaving}
                  onPress={() => setServerEngineDialogVisible(true)}
                >
                  Serverモデル
                </Button>
              ) : null}
              {isDirectProvider(chatProvider) ? (
                <Button
                  mode="text"
                  compact
                  textColor="#89b4fa"
                  onPress={() => void openCredentialsDialog(chatProvider)}
                >
                  資格情報
                </Button>
              ) : null}
            </View>
          </Surface>

          <Surface style={styles.modelSlotCard} elevation={0}>
            <View style={styles.modelSlotHeader}>
              <View style={styles.modelSlotText}>
                <Text style={styles.settingLabel}>フォールバック</Text>
                <Text style={styles.settingValue} numberOfLines={2}>
                  {modelSlotSummary(chatFallbackProvider)}
                </Text>
                <Text style={styles.settingHint}>
                  メインが通信失敗・認証失敗・モデルエラーになった時だけ使います。
                </Text>
              </View>
              <Button
                mode="outlined"
                compact
                style={styles.compactButton}
                onPress={() => setModelSlotDialogVisible("fallback")}
              >
                選択
              </Button>
            </View>
            {isFallbackDirectProvider(chatFallbackProvider) ? (
              <View style={styles.slotActions}>
                <Button
                  mode="text"
                  compact
                  textColor="#89b4fa"
                  onPress={() => void openCredentialsDialog(chatFallbackProvider)}
                >
                  資格情報
                </Button>
              </View>
            ) : null}
          </Surface>
        </View>
        <Divider style={styles.innerDivider} />
        <List.Item
          title="会話カスタム指示"
          titleStyle={styles.navTitle}
          description={
            isAuthenticated
              ? customInstructionsSummary
              : "サーバーログイン中のみ利用できます。"
          }
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="message-text-outline" color="#89b4fa" />
          )}
          disabled={!isAuthenticated}
          onPress={() => setCustomInstructionsDialogVisible(true)}
        />
        <List.Item
          title="キャラクター"
          titleStyle={styles.navTitle}
          description="会話キャラクターとデフォルトキャラクター。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="account-star-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/character")}
        />
        <List.Item
          title="ユーザーメモリ"
          titleStyle={styles.navTitle}
          description="会話で使うユーザーメモリ。"
          descriptionStyle={styles.navDescription}
          left={(props) => <List.Icon {...props} icon="brain" color="#89b4fa" />}
          onPress={() => router.push("/(tabs)/settings/memory")}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>入力・操作</Text>
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
        <List.Item
          title="作業範囲"
          titleStyle={styles.navTitle}
          description={selectedScopeLabel}
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="target" color="#89b4fa" />
          )}
          onPress={() => setScopeDialogVisible(true)}
        />
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>通知・予定</Text>
        <List.Item
          title="タスク通知 / カレンダー"
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
        <Text style={styles.sectionTitle}>プロジェクト</Text>
        <List.Item
          title="プロジェクト管理"
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
        <Text style={styles.sectionTitle}>作業時間</Text>
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
        <Text style={styles.sectionTitle}>その他の機能</Text>
        <List.Item
          title="シナリオ"
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
        <Text style={styles.sectionTitle}>接続・権限</Text>
        <List.Item
          title="サーバー / ネットワーク"
          titleStyle={styles.navTitle}
          description="API URLとWi-Fi別の接続先。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="lan-connect" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/connection")}
        />
        <List.Item
          title="MCP / エージェント"
          titleStyle={styles.navTitle}
          description="外部ツール連携とエージェント接続状態。"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="robot-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/mcp")}
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
          visible={customInstructionsDialogVisible}
          onDismiss={() => setCustomInstructionsDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>会話カスタム指示</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.settingHint}>
              すべての会話に追加するユーザー別指示です。
            </Text>
            <TextInput
              label="カスタム指示"
              value={customInstructions}
              onChangeText={setCustomInstructions}
              mode="outlined"
              style={styles.dialogInput}
              multiline
              numberOfLines={6}
            />
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setCustomInstructionsDialogVisible(false)}>
              閉じる
            </Button>
            <Button
              loading={settingsSaving}
              onPress={() => void handleSaveUserSettings()}
            >
              保存
            </Button>
          </Dialog.Actions>
        </Dialog>

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
          visible={modelSlotDialogVisible !== null}
          onDismiss={() => setModelSlotDialogVisible(null)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            {modelSlotDialogVisible === "fallback" ? "フォールバック" : "メイン"}
            モデル
          </Dialog.Title>
          <Dialog.Content>
            <Text style={styles.settingHint}>
              {modelSlotDialogVisible === "fallback"
                ? "メインが使えなかった時だけ使うDirectモデルを選びます。"
                : "通常の会話応答に使うモデルを1つ選びます。"}
            </Text>
          </Dialog.Content>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              {modelSlotDialogVisible === "fallback" ? (
                <RadioButton.Group
                  value={chatFallbackProvider}
                  onValueChange={(value) =>
                    void handleSelectFallbackProvider(
                      value as MobileLlmFallbackProvider,
                    )
                  }
                >
                  <RadioButton.Item
                    label="なし"
                    value="off"
                    labelStyle={styles.radioLabel}
                    color="#7c3aed"
                    uncheckedColor="#585b70"
                  />
                  {DIRECT_PROVIDERS.map((provider) => (
                    <RadioButton.Item
                      key={provider}
                      label={`${DIRECT_PROVIDER_LABELS[provider]} / ${directProfiles[provider].model || "モデル未設定"}`}
                      value={provider}
                      labelStyle={styles.radioLabel}
                      color="#7c3aed"
                      uncheckedColor="#585b70"
                      disabled={chatProvider === provider}
                    />
                  ))}
                </RadioButton.Group>
              ) : (
                <RadioButton.Group
                  value={chatProvider}
                  onValueChange={(value) =>
                    void handleSelectMainProvider(value as MobileLlmProvider)
                  }
                >
                  <RadioButton.Item
                    label={`Server / ${currentServerEngineLabel}`}
                    value="server"
                    labelStyle={styles.radioLabel}
                    color="#7c3aed"
                    uncheckedColor="#585b70"
                  />
                  {DIRECT_PROVIDERS.map((provider) => (
                    <RadioButton.Item
                      key={provider}
                      label={`${DIRECT_PROVIDER_LABELS[provider]} / ${directProfiles[provider].model || "モデル未設定"}`}
                      value={provider}
                      labelStyle={styles.radioLabel}
                      color="#7c3aed"
                      uncheckedColor="#585b70"
                    />
                  ))}
                </RadioButton.Group>
              )}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button onPress={() => setModelSlotDialogVisible(null)}>閉じる</Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={chatCredentialsDialogVisible}
          onDismiss={() => setChatCredentialsDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            {editingCredentialProvider
              ? `${DIRECT_PROVIDER_LABELS[editingCredentialProvider]} 資格情報`
              : "Direct資格情報"}
          </Dialog.Title>
          <Dialog.Content>
            <Text style={styles.settingHint}>
              メインまたはフォールバックでこのプロバイダーを選んだ時に使います。
            </Text>
            <TextInput
              label="APIキー"
              value={chatApiKey}
              onChangeText={setChatApiKey}
              mode="outlined"
              style={styles.dialogInput}
              secureTextEntry
              autoCapitalize="none"
              disabled={!editingCredentialProvider}
            />
            <TextInput
              label="Model"
              value={chatModel}
              onChangeText={setChatModel}
              mode="outlined"
              style={styles.dialogInput}
              autoCapitalize="none"
              disabled={!editingCredentialProvider}
            />
            <TextInput
              label="Base URL"
              value={chatBaseUrl}
              onChangeText={setChatBaseUrl}
              mode="outlined"
              style={styles.dialogInput}
              autoCapitalize="none"
              disabled={!editingCredentialProvider}
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
              disabled={!editingCredentialProvider}
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
  modelSlotList: { gap: 10, marginTop: 12 },
  modelSlotCard: {
    backgroundColor: "#1e1e2e",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
  },
  modelSlotHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  modelSlotText: { flex: 1 },
  slotActions: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
    marginTop: 8,
  },
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
