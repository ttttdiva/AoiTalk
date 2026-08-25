import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import {
  Button,
  Dialog,
  Divider,
  List,
  Portal,
  ProgressBar,
  RadioButton,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import { useRouter } from "expo-router";
import { ScreenHeader } from "../../../components/screen-header";
import { ScreenShell } from "../../../components/screen-primitives";
import { useAuth } from "../../../contexts/AuthContext";
import {
  getClipIngestConfig,
  getDirectReasoningEffortOptions,
  getFallbackConfig,
  getMainSlot,
  getProviderProfile,
  isDirectProvider,
  saveClipIngestConfig,
  saveFallbackConfig,
  saveMainSlot,
  saveProviderProfile,
  testMobileLlmConnection,
  type MobileLlmClipIngestConfig,
  type MobileLlmFallbackConfig,
  type MobileLlmProviderProfile,
  type MobileLlmSlotSelection,
} from "../../../lib/mobile-llm";
import {
  DIRECT_PROVIDER_ORDER,
  fetchCloudModels,
  getProviderDefinition,
  getProviderLabel,
  getSeedModelIds,
  mergeModelIds,
  readCachedModels,
  writeCachedModels,
  type DirectMobileLlmProvider,
  type MobileLlmProvider,
} from "../../../lib/cloud-model-catalog";
import {
  llmEngineApi,
  type LlmEngineOption,
  type LlmEngineState,
} from "../../../lib/llm-engine-api";
import { taskApi } from "../../../lib/task-api";
import { getCurrentVersion } from "../../../lib/update-service";
import {
  forceDocsResync,
  type DocsResyncProgress,
} from "../../../sync/engine";
import {
  DEFAULT_AUDIO_PLAYER_SETTINGS,
  getAudioPlayerSettings,
  serializeAudioPlayerSettings,
  type AudioPlayerSettings,
} from "../../../lib/audio-player-settings";
import type { UserSettings } from "../../../types/api";

type ModelSlot = "main" | "fallback" | "clipIngest";

const CUSTOM_MODEL_VALUE = "__custom__";

function SelectorRow({
  label,
  value,
  onPress,
  disabled = false,
}: {
  label: string;
  value: string;
  onPress: () => void;
  disabled?: boolean;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${label}を選択。現在は${value}`}
      disabled={disabled}
      onPress={onPress}
      style={[styles.selectorRow, disabled ? styles.selectorRowDisabled : null]}
    >
      <View style={styles.selectorRowText}>
        <Text style={styles.settingLabel}>{label}</Text>
        <Text style={styles.settingValue} numberOfLines={1}>
          {value}
        </Text>
      </View>
      <Text style={styles.selectorChevron}>›</Text>
    </Pressable>
  );
}

const EMPTY_PROVIDER_PROFILES: Record<
  DirectMobileLlmProvider,
  MobileLlmProviderProfile
> = DIRECT_PROVIDER_ORDER.reduce(
  (acc, provider) => {
    acc[provider] = {
      apiKey: "",
      baseUrl: getProviderDefinition(provider).defaultBaseUrl,
    };
    return acc;
  },
  {} as Record<DirectMobileLlmProvider, MobileLlmProviderProfile>,
);

function formatDocsResyncError(error: unknown): string {
  if (
    error instanceof Error &&
    error.message.includes("Docs sync snapshot changed")
  ) {
    return "サーバー側でDocsが更新されたため、再同期を完了できませんでした。少し待ってからもう一度実行してください。";
  }
  return error instanceof Error ? error.message : "Docsの再取得に失敗しました。";
}

export default function SettingsScreen() {
  const router = useRouter();
  const { user, logout, isAuthenticated, isAnonymous } = useAuth();
  const [userSettings, setUserSettings] = useState<UserSettings>({});
  const [customInstructions, setCustomInstructions] = useState("");
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [customInstructionsDialogVisible, setCustomInstructionsDialogVisible] =
    useState(false);
  const [audioPlayerSettings, setAudioPlayerSettings] =
    useState<AudioPlayerSettings>(DEFAULT_AUDIO_PLAYER_SETTINGS);
  const [audioSettingsSaving, setAudioSettingsSaving] = useState(false);
  const [docsResyncing, setDocsResyncing] = useState(false);
  const [docsResyncProgress, setDocsResyncProgress] =
    useState<DocsResyncProgress | null>(null);
  const [audioSettingsDialogVisible, setAudioSettingsDialogVisible] =
    useState(false);
  const [mainSlot, setMainSlot] = useState<MobileLlmSlotSelection>({
    provider: "server",
    model: "",
    reasoningEffort: "",
  });
  const [fallback, setFallback] = useState<MobileLlmFallbackConfig>({
    enabled: false,
    provider: "openai",
    model: "",
    reasoningEffort: "",
  });
  const [clipIngest, setClipIngest] = useState<MobileLlmClipIngestConfig>({
    enabled: false,
    provider: "openai",
    model: "",
    reasoningEffort: "",
  });
  const [providerProfiles, setProviderProfiles] =
    useState<Record<DirectMobileLlmProvider, MobileLlmProviderProfile>>(
      EMPTY_PROVIDER_PROFILES,
    );
  // スロット編集ダイアログのドラフト状態。
  const [slotDialog, setSlotDialog] = useState<ModelSlot | null>(null);
  const [slotSelector, setSlotSelector] = useState<
    "provider" | "model" | "effort" | null
  >(null);
  const [draftProvider, setDraftProvider] =
    useState<MobileLlmProvider>("server");
  const [draftSelectedModel, setDraftSelectedModel] = useState("");
  const [draftCustomModel, setDraftCustomModel] = useState("");
  const [draftApiKey, setDraftApiKey] = useState("");
  const [draftBaseUrl, setDraftBaseUrl] = useState("");
  const [draftFallbackEnabled, setDraftFallbackEnabled] = useState(true);
  // クリップ取り込みスロットの「個別に指定する」トグルと reasoning effort。
  const [draftClipIngestEnabled, setDraftClipIngestEnabled] = useState(false);
  const [draftEffort, setDraftEffort] = useState("");
  const [slotSaving, setSlotSaving] = useState(false);
  const [slotSaveError, setSlotSaveError] = useState<string | null>(null);
  const [connTesting, setConnTesting] = useState(false);
  const [connResult, setConnResult] = useState<{
    ok: boolean;
    message?: string;
  } | null>(null);
  // モデル一覧（動的取得＋静的シードのマージ結果）とその取得状態。
  const [draftModelChoices, setDraftModelChoices] = useState<string[]>([]);
  const [modelListLoading, setModelListLoading] = useState(false);
  const [modelListError, setModelListError] = useState<string | null>(null);
  const [modelFilter, setModelFilter] = useState("");
  // 直近のモデル取得リクエストを識別し、古い結果の反映を防ぐ。
  const modelReqSeq = useRef(0);
  const [serverEngine, setServerEngine] = useState<LlmEngineState | null>(null);
  const [serverEngineSaving, setServerEngineSaving] = useState(false);
  const [serverEngineDialogVisible, setServerEngineDialogVisible] =
    useState(false);

  const visibleDirectProviders = DIRECT_PROVIDER_ORDER;
  const visibleStandardDirectProviders = useMemo(
    () =>
      visibleDirectProviders.filter(
        (provider) => !getProviderDefinition(provider).advanced,
      ),
    [visibleDirectProviders],
  );
  const visibleAdvancedDirectProviders = useMemo(
    () =>
      visibleDirectProviders.filter((provider) =>
        getProviderDefinition(provider).advanced,
      ),
    [visibleDirectProviders],
  );
  const visibleServerEngineOptions = serverEngine?.available ?? [];

  const loadProviderProfiles = useCallback(async () => {
    const entries = await Promise.all(
      DIRECT_PROVIDER_ORDER.map(async (provider) => {
        const profile = await getProviderProfile(provider);
        return [provider, profile] as const;
      }),
    );
    const next = { ...EMPTY_PROVIDER_PROFILES };
    for (const [provider, profile] of entries) {
      next[provider] = profile;
    }
    setProviderProfiles(next);
    return next;
  }, []);

  useEffect(() => {
    void (async () => {
      const [slot, fallbackConfig, clipIngestConfig] = await Promise.all([
        getMainSlot(),
        getFallbackConfig(),
        getClipIngestConfig(),
      ]);
      setMainSlot(slot);
      setFallback(fallbackConfig);
      setClipIngest(clipIngestConfig);
      await loadProviderProfiles();
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
  }, [isAuthenticated, loadProviderProfiles]);

  // ドラフトのモデル選択（候補ラジオ or カスタム）を指定プロバイダーへ初期化する。
  const initDraftModel = useCallback(
    (provider: MobileLlmProvider, model: string) => {
      if (!isDirectProvider(provider)) {
        setDraftSelectedModel("");
        setDraftCustomModel("");
        return;
      }
      const definition = getProviderDefinition(provider);
      const trimmed = model.trim();
      const inCandidates = definition.models.some(
        (candidate) => candidate.id === trimmed,
      );
      if (trimmed && inCandidates) {
        setDraftSelectedModel(trimmed);
        setDraftCustomModel("");
      } else if (trimmed) {
        setDraftSelectedModel(CUSTOM_MODEL_VALUE);
        setDraftCustomModel(trimmed);
      } else if (definition.models.length > 0) {
        setDraftSelectedModel(definition.defaultModel);
        setDraftCustomModel("");
      } else {
        setDraftSelectedModel(CUSTOM_MODEL_VALUE);
        setDraftCustomModel("");
      }
    },
    [],
  );

  // プロバイダーAPIからモデル一覧を取得する。
  // キャッシュ→即時表示、その後バックグラウンドで最新取得して差し替える。
  const loadModelChoices = useCallback(
    async (provider: MobileLlmProvider, apiKey: string, baseUrl: string) => {
      if (!isDirectProvider(provider)) {
        setDraftModelChoices([]);
        setModelListLoading(false);
        setModelListError(null);
        return;
      }
      const seq = ++modelReqSeq.current;
      const seeds = getSeedModelIds(provider);
      setModelListError(null);
      setModelFilter("");
      // 前プロバイダーの一覧が残像表示されないよう、まずシードへ同期的に差し替える。
      setDraftModelChoices(seeds.slice());
      // 1. キャッシュ（あれば）＋静的シードを即時表示。
      const cached = await readCachedModels(provider).catch(() => []);
      if (seq !== modelReqSeq.current) return;
      setDraftModelChoices(mergeModelIds(cached, seeds));
      // 2. バックグラウンドで最新を取得。
      setModelListLoading(true);
      try {
        const fetched = await fetchCloudModels(provider, {
          apiKey: apiKey.trim(),
          baseUrl: baseUrl.trim(),
        });
        if (seq !== modelReqSeq.current) return;
        if (fetched.length > 0) {
          setDraftModelChoices(mergeModelIds(fetched, seeds));
          void writeCachedModels(provider, fetched);
        } else if (cached.length === 0) {
          // 取得できず、キャッシュも無い（＝APIキー未入力など）。シードのみ表示。
          setModelListError(
            "モデル一覧を取得できませんでした（手入力可）",
          );
        }
      } catch {
        if (seq !== modelReqSeq.current) return;
        setModelListError("モデル一覧を取得できませんでした（手入力可）");
      } finally {
        if (seq === modelReqSeq.current) setModelListLoading(false);
      }
    },
    [],
  );

  const openSlotDialog = useCallback(
    (slot: ModelSlot) => {
      setSlotSelector(null);
      setSlotSaveError(null);
      setConnResult(null);
      setDraftEffort("");
      if (slot === "main") {
        const provider = mainSlot.provider;
        setDraftProvider(provider);
        initDraftModel(provider, mainSlot.model);
        setDraftEffort(mainSlot.reasoningEffort ?? "");
        setDraftFallbackEnabled(true);
        setDraftClipIngestEnabled(false);
        if (isDirectProvider(provider)) {
          const profile = providerProfiles[provider];
          const apiKey = profile?.apiKey ?? "";
          const baseUrl =
            profile?.baseUrl ?? getProviderDefinition(provider).defaultBaseUrl;
          setDraftApiKey(apiKey);
          setDraftBaseUrl(baseUrl);
          void loadModelChoices(provider, apiKey, baseUrl);
        } else {
          setDraftApiKey("");
          setDraftBaseUrl("");
          setDraftModelChoices([]);
        }
      } else {
        const config = slot === "clipIngest" ? clipIngest : fallback;
        const provider = config.provider;
        setDraftProvider(provider);
        initDraftModel(provider, config.model);
        setDraftFallbackEnabled(fallback.enabled);
        setDraftClipIngestEnabled(clipIngest.enabled);
        setDraftEffort(
          slot === "clipIngest"
            ? clipIngest.reasoningEffort
            : fallback.reasoningEffort ?? "",
        );
        const profile = providerProfiles[provider];
        const apiKey = profile?.apiKey ?? "";
        const baseUrl =
          profile?.baseUrl ?? getProviderDefinition(provider).defaultBaseUrl;
        setDraftApiKey(apiKey);
        setDraftBaseUrl(baseUrl);
        void loadModelChoices(provider, apiKey, baseUrl);
      }
      setSlotDialog(slot);
    },
    [
      clipIngest,
      fallback,
      initDraftModel,
      loadModelChoices,
      mainSlot,
      providerProfiles,
    ],
  );

  // ダイアログ内でプロバイダーを切り替えたら、そのプロバイダーの
  // 共有プロファイルと既定モデルへドラフトを差し替える。
  const applyDraftProvider = useCallback(
    (provider: MobileLlmProvider) => {
      setDraftProvider(provider);
      setConnResult(null);
      setSlotSaveError(null);
      // プロバイダーが変わると effort の選択肢も変わるので既定へ戻す。
      setDraftEffort("");
      if (!isDirectProvider(provider)) {
        setDraftApiKey("");
        setDraftBaseUrl("");
        setDraftModelChoices([]);
        initDraftModel(provider, "");
        return;
      }
      const definition = getProviderDefinition(provider);
      const profile = providerProfiles[provider];
      const apiKey = profile?.apiKey ?? "";
      const baseUrl = profile?.baseUrl ?? definition.defaultBaseUrl;
      setDraftApiKey(apiKey);
      setDraftBaseUrl(baseUrl);
      initDraftModel(provider, "");
      void loadModelChoices(provider, apiKey, baseUrl);
    },
    [initDraftModel, loadModelChoices, providerProfiles],
  );

  const draftEffectiveModel =
    draftSelectedModel === CUSTOM_MODEL_VALUE
      ? draftCustomModel.trim()
      : draftSelectedModel.trim();

  // 「メインと同じ」を選んでいる間は、プロバイダー・モデル欄を出さない。
  const slotFieldsVisible = slotDialog !== "clipIngest" || draftClipIngestEnabled;

  const draftEffortOptions = useMemo(
    () =>
      isDirectProvider(draftProvider)
        ? getDirectReasoningEffortOptions(draftProvider, draftEffectiveModel)
        : [],
    [draftEffectiveModel, draftProvider],
  );

  // 動的一覧が届いた後、カスタム入力中のモデルが一覧に含まれていれば
  // ラジオ選択へ昇格させる（一覧取得前は候補外＝カスタム扱いだったもの）。
  useEffect(() => {
    if (draftSelectedModel !== CUSTOM_MODEL_VALUE) return;
    const custom = draftCustomModel.trim();
    if (custom && draftModelChoices.includes(custom)) {
      setDraftSelectedModel(custom);
      setDraftCustomModel("");
    }
  }, [draftCustomModel, draftModelChoices, draftSelectedModel]);

  // 動的取得済みのモデルへ静的候補のラベルを当てる（無ければIDそのまま）。
  const modelLabelById = useMemo(() => {
    const map = new Map<string, string>();
    if (isDirectProvider(draftProvider)) {
      for (const candidate of getProviderDefinition(draftProvider).models) {
        map.set(candidate.id, candidate.label ?? candidate.id);
      }
    }
    return map;
  }, [draftProvider]);

  const filteredModelChoices = useMemo(() => {
    const query = modelFilter.trim().toLowerCase();
    if (!query) return draftModelChoices;
    return draftModelChoices.filter((id) =>
      id.toLowerCase().includes(query),
    );
  }, [draftModelChoices, modelFilter]);

  const orderedModelChoices = useMemo(() => {
    const selected = draftEffectiveModel;
    if (!selected || !filteredModelChoices.includes(selected)) {
      return filteredModelChoices;
    }
    return [
      selected,
      ...filteredModelChoices.filter((model) => model !== selected),
    ];
  }, [draftEffectiveModel, filteredModelChoices]);

  const handleRefreshModels = useCallback(() => {
    void loadModelChoices(draftProvider, draftApiKey, draftBaseUrl);
  }, [draftApiKey, draftBaseUrl, draftProvider, loadModelChoices]);

  const handleTestConnection = useCallback(async () => {
    if (!isDirectProvider(draftProvider)) return;
    const definition = getProviderDefinition(draftProvider);
    setConnTesting(true);
    setConnResult(null);
    try {
      const result = await testMobileLlmConnection({
        provider: draftProvider,
        apiKey: draftApiKey,
        model: draftEffectiveModel,
        baseUrl: draftBaseUrl.trim() || definition.defaultBaseUrl,
      });
      setConnResult(result);
    } finally {
      setConnTesting(false);
    }
  }, [draftApiKey, draftBaseUrl, draftEffectiveModel, draftProvider]);

  const handleSaveSlot = useCallback(async () => {
    const slot = slotDialog;
    if (!slot) return;
    setSlotSaveError(null);

    // クリップ取り込みで「メインと同じ」を選んだ場合はモデル指定不要。
    if (slot === "clipIngest" && !draftClipIngestEnabled) {
      setSlotSaving(true);
      try {
        const next: MobileLlmClipIngestConfig = {
          ...clipIngest,
          enabled: false,
        };
        await saveClipIngestConfig(next);
        setClipIngest(next);
        setSlotSelector(null);
        setSlotDialog(null);
      } catch (error) {
        setSlotSaveError(
          error instanceof Error ? error.message : "保存に失敗しました。",
        );
      } finally {
        setSlotSaving(false);
      }
      return;
    }

    // メインスロットで Server を選択した場合はモデル不要。
    if (slot === "main" && !isDirectProvider(draftProvider)) {
      setSlotSaving(true);
      try {
        await saveMainSlot("server", "", "");
        setMainSlot({ provider: "server", model: "", reasoningEffort: "" });
        setSlotSelector(null);
        setSlotDialog(null);
      } catch (error) {
        setSlotSaveError(
          error instanceof Error ? error.message : "保存に失敗しました。",
        );
      } finally {
        setSlotSaving(false);
      }
      return;
    }

    if (!isDirectProvider(draftProvider)) return;
    const definition = getProviderDefinition(draftProvider);
    const model = draftEffectiveModel;
    if (!model) {
      setSlotSaveError("モデルIDを指定してください。");
      return;
    }
    const baseUrl = draftBaseUrl.trim() || definition.defaultBaseUrl;
    if (definition.baseUrlRequired && !baseUrl) {
      setSlotSaveError("Base URL を入力してください。");
      return;
    }
    const efforts = getDirectReasoningEffortOptions(draftProvider, model);
    const reasoningEffort = efforts.includes(draftEffort)
      ? draftEffort
      : efforts[0] ?? "";

    setSlotSaving(true);
    try {
      await saveProviderProfile(draftProvider, {
        apiKey: draftApiKey,
        baseUrl,
      });
      const nextProfile: MobileLlmProviderProfile = {
        apiKey: draftApiKey.trim(),
        baseUrl,
      };
      if (slot === "main") {
        await saveMainSlot(draftProvider, model, reasoningEffort);
        setMainSlot({ provider: draftProvider, model, reasoningEffort });
      } else if (slot === "clipIngest") {
        const nextClipIngest: MobileLlmClipIngestConfig = {
          enabled: true,
          provider: draftProvider,
          model,
          // 対応しないモデルへ effort を残すと無効な値を送ってしまうので捨てる。
          reasoningEffort: efforts.includes(draftEffort) ? draftEffort : "",
        };
        await saveClipIngestConfig(nextClipIngest);
        setClipIngest(nextClipIngest);
      } else {
        const nextFallback: MobileLlmFallbackConfig = {
          enabled: draftFallbackEnabled,
          provider: draftProvider,
          model,
          reasoningEffort,
        };
        await saveFallbackConfig(nextFallback);
        setFallback(nextFallback);
      }
      setProviderProfiles((prev) => ({
        ...prev,
        [draftProvider]: nextProfile,
      }));
      setSlotSelector(null);
      setSlotDialog(null);
    } catch (error) {
      setSlotSaveError(
        error instanceof Error ? error.message : "保存に失敗しました。",
      );
    } finally {
      setSlotSaving(false);
    }
  }, [
    clipIngest,
    draftApiKey,
    draftBaseUrl,
    draftClipIngestEnabled,
    draftEffectiveModel,
    draftEffort,
    draftFallbackEnabled,
    draftProvider,
    slotDialog,
  ]);

  const handleToggleFallbackEnabled = useCallback(async () => {
    const next: MobileLlmFallbackConfig = {
      ...fallback,
      enabled: !fallback.enabled,
    };
    // 有効化するのにモデル未設定ならダイアログを開いて設定を促す。
    if (next.enabled && !fallback.model.trim()) {
      openSlotDialog("fallback");
      return;
    }
    try {
      await saveFallbackConfig(next);
      setFallback(next);
    } catch {
      // モデル未設定などで失敗した場合はダイアログへ誘導。
      openSlotDialog("fallback");
    }
  }, [fallback, openSlotDialog]);

  const handleLogout = async () => {
    await logout();
    router.replace("/(auth)/login");
  };

  const handleForceDocsResync = () => {
    if (!isAuthenticated || docsResyncing) return;
    Alert.alert(
      "Docs再同期",
      "端末のDocsと未送信の編集を破棄して、サーバーから再取得します。サーバー上のデータは削除されません。",
      [
        { text: "キャンセル", style: "cancel" },
        {
          text: "再同期",
          style: "destructive",
          onPress: () => {
            void (async () => {
              setDocsResyncing(true);
              setDocsResyncProgress({
                phase: "preparing",
                completed: 0,
                total: null,
                page: 0,
              });
              try {
                await forceDocsResync(setDocsResyncProgress);
                Alert.alert("Docs再同期完了", "サーバーからDocsを再取得しました。");
              } catch (error) {
                Alert.alert(
                  "Docs再同期失敗",
                  formatDocsResyncError(error),
                );
              } finally {
                setDocsResyncing(false);
                setDocsResyncProgress(null);
              }
            })();
          },
        },
      ],
    );
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

  const directSlotSummary = useCallback(
    (provider: DirectMobileLlmProvider, model: string) => {
      const modelLabel = model.trim() || "モデル未設定";
      const keyStatus = providerProfiles[provider]?.apiKey?.trim()
        ? "APIキーあり"
        : "APIキー未設定";
      return `${getProviderLabel(provider)} / ${modelLabel} / ${keyStatus}`;
    },
    [providerProfiles],
  );

  const mainSlotSummary = useMemo(() => {
    if (!isDirectProvider(mainSlot.provider)) {
      return `Server / ${currentServerEngineLabel}`;
    }
    const base = directSlotSummary(mainSlot.provider, mainSlot.model);
    return mainSlot.reasoningEffort
      ? `${base} / effort: ${mainSlot.reasoningEffort}`
      : base;
  }, [currentServerEngineLabel, directSlotSummary, mainSlot]);

  const fallbackSlotSummary = useMemo(() => {
    if (!fallback.enabled) return "無効（メインのみ使用）";
    const base = directSlotSummary(fallback.provider, fallback.model);
    return fallback.reasoningEffort
      ? `${base} / effort: ${fallback.reasoningEffort}`
      : base;
  }, [directSlotSummary, fallback]);

  const clipIngestSlotSummary = useMemo(() => {
    if (!clipIngest.enabled) return `メインと同じ（${mainSlotSummary}）`;
    const base = directSlotSummary(clipIngest.provider, clipIngest.model);
    return clipIngest.reasoningEffort
      ? `${base} / effort: ${clipIngest.reasoningEffort}`
      : base;
  }, [clipIngest, directSlotSummary, mainSlotSummary]);

  const customInstructionsSummary = customInstructions.trim()
    ? "設定済み"
    : "未設定";


  return (
    <ScreenShell
      scroll
      style={styles.container}
      contentContainerStyle={styles.content}
      header={
        <ScreenHeader
          title="Settings"
          subtitle="普段使う設定を上に、管理・接続・アプリ情報を下にまとめています。"
          showSettings={false}
        />
      }
    >

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

      {isAuthenticated ? (
        <>
          <Divider style={styles.divider} />

          <Surface style={styles.section} elevation={0}>
            <Text style={styles.sectionTitle}>データ管理</Text>
            <List.Item
              title="Docs再同期"
              titleStyle={styles.navTitle}
              description={
                docsResyncProgress?.phase === "preparing"
                  ? "再同期の準備中…"
                  : docsResyncProgress?.phase === "finalizing"
                    ? "端末データを仕上げています…"
                    : docsResyncProgress?.phase === "downloading"
                      ? docsResyncProgress.total != null
                        ? `${docsResyncProgress.completed.toLocaleString()} / ${docsResyncProgress.total.toLocaleString()}件を取得済み`
                        : `${docsResyncProgress.completed.toLocaleString()}件を取得済み`
                      : "サーバーから再取得"
              }
              descriptionStyle={styles.navDescription}
              left={(props) => (
                <List.Icon
                  {...props}
                  icon="database-sync-outline"
                  color="#f38ba8"
                />
              )}
              right={() =>
                docsResyncing ? (
                  <ActivityIndicator color="#f38ba8" />
                ) : (
                  <List.Icon icon="chevron-right" color="#a6adc8" />
                )
              }
              accessibilityLabel="Docs再同期"
              disabled={docsResyncing}
              onPress={handleForceDocsResync}
            />
            {docsResyncing ? (
              <View style={styles.docsResyncProgress}>
                <ProgressBar
                  accessibilityLabel="Docs再同期の進捗"
                  progress={
                    docsResyncProgress?.total != null
                      ? docsResyncProgress.total === 0
                        ? 1
                        : Math.min(
                            1,
                            docsResyncProgress.completed
                              / docsResyncProgress.total,
                          )
                      : 0
                  }
                  indeterminate={docsResyncProgress?.total == null}
                  color="#f38ba8"
                  style={styles.docsResyncProgressBar}
                />
                <Text style={styles.docsResyncProgressText}>
                  {docsResyncProgress?.total != null
                    ? docsResyncProgress.total === 0
                      ? "100%"
                      : `${Math.floor(
                          (docsResyncProgress.completed
                            / docsResyncProgress.total)
                            * 100,
                        )}%`
                    : "件数を確認中"}
                </Text>
              </View>
            ) : null}
          </Surface>
        </>
      ) : null}

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>会話・AI応答</Text>
        <Text style={styles.helperText}>
          通常のモデルと、送信失敗時のフォールバックを設定します。
        </Text>
        <View style={styles.modelSlotList}>
          <Surface style={styles.modelSlotCard} elevation={0}>
            <View style={styles.modelSlotHeader}>
              <View style={styles.modelSlotText}>
                <Text style={styles.settingLabel}>メイン</Text>
                <Text style={styles.settingValue} numberOfLines={2}>
                  {mainSlotSummary}
                </Text>
                <Text style={styles.settingHint}>通常の会話応答で使います。</Text>
              </View>
              <Button
                accessibilityLabel="メイン設定を開く"
                mode="outlined"
                compact
                style={styles.compactButton}
                onPress={() => openSlotDialog("main")}
              >
                設定
              </Button>
            </View>
            {mainSlot.provider === "server" ? (
              <View style={styles.slotActions}>
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
              </View>
            ) : null}
          </Surface>

          <Surface style={styles.modelSlotCard} elevation={0}>
            <View style={styles.modelSlotHeader}>
              <View style={styles.modelSlotText}>
                <Text style={styles.settingLabel}>フォールバック</Text>
                <Text style={styles.settingValue} numberOfLines={2}>
                  {fallbackSlotSummary}
                </Text>
                <Text style={styles.settingHint}>
                  メインの送信失敗時に使用します。
                </Text>
              </View>
              <View style={styles.fallbackSlotControls}>
                <Switch
                  accessibilityLabel="フォールバック切替"
                  value={fallback.enabled}
                  onValueChange={() => void handleToggleFallbackEnabled()}
                />
                <Button
                  accessibilityLabel="フォールバック設定を開く"
                  mode="outlined"
                  compact
                  style={styles.compactButton}
                  onPress={() => openSlotDialog("fallback")}
                >
                  設定
                </Button>
              </View>
            </View>
          </Surface>

          <Surface style={styles.modelSlotCard} elevation={0}>
            <View style={styles.modelSlotHeader}>
              <View style={styles.modelSlotText}>
                <Text style={styles.settingLabel}>クリップ取り込み</Text>
                <Text style={styles.settingValue} numberOfLines={2}>
                  {clipIngestSlotSummary}
                </Text>
                <Text style={styles.settingHint}>
                  オフライン時のDocs取り込みに使います。
                </Text>
              </View>
              <Button
                accessibilityLabel="クリップ取り込み設定を開く"
                mode="outlined"
                compact
                style={styles.compactButton}
                onPress={() => openSlotDialog("clipIngest")}
              >
                設定
              </Button>
            </View>
          </Surface>
        </View>
        <Divider style={styles.innerDivider} />
        <List.Item
          title="会話カスタム指示"
          titleStyle={styles.navTitle}
          description={
            isAuthenticated
              ? customInstructionsSummary
              : "サーバーログイン中のみ利用可"
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
          description="会話キャラクターを設定"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="account-star-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/character")}
        />
        <List.Item
          title="ユーザーメモリ"
          titleStyle={styles.navTitle}
          description="会話で使うユーザーメモリ"
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
      </Surface>

      <Divider style={styles.divider} />

      <Surface style={styles.section} elevation={0}>
        <Text style={styles.sectionTitle}>通知・予定</Text>
        <List.Item
          title="タスク通知 / カレンダー"
          titleStyle={styles.navTitle}
          description="通知とGoogle Calendar"
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
          description="プロジェクトを管理"
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
          description="記録した作業時間の集計"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="chart-bar" color="#89b4fa" />
          )}
          onPress={() => router.push("/reports")}
        />
        <List.Item
          title="時間順の記録"
          titleStyle={styles.navTitle}
          description="記録と予定枠を時系列で確認"
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
          description="シナリオと執筆セッション"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="book-open-variant" color="#89b4fa" />
          )}
          onPress={() => router.push("/scenarios")}
        />
        <List.Item
          title="TRPG"
          titleStyle={styles.navTitle}
          description="TRPGルームの作成・参加"
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
          description="API URLと接続先"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="lan-connect" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/connection")}
        />
        <List.Item
          title="MCP / エージェント"
          titleStyle={styles.navTitle}
          description="外部ツール連携"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon {...props} icon="robot-outline" color="#89b4fa" />
          )}
          onPress={() => router.push("/(tabs)/settings/mcp")}
        />
        <List.Item
          title="外部サーバー接続"
          titleStyle={styles.navTitle}
          description="他のAoiTalkサーバーへ接続"
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
          title="Apps"
          titleStyle={styles.navTitle}
          description="App管理・実行状況"
          descriptionStyle={styles.navDescription}
          left={(props) => (
            <List.Icon
              {...props}
              icon="application-brackets-outline"
              color="#89b4fa"
            />
          )}
          onPress={() => router.push("/(tabs)/apps")}
        />
        <List.Item
          title="アプリ情報"
          titleStyle={styles.navTitle}
          description="バージョンと診断情報"
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
          visible={slotSelector !== null}
          onDismiss={() => setSlotSelector(null)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            {slotSelector === "provider"
              ? "プロバイダーを選択"
              : slotSelector === "model"
                ? "モデルを選択"
                : "Effortを選択"}
          </Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.selectorDialogContent}>
              {slotSelector === "provider" ? (
                <>
                  {slotDialog === "main" ? (
                    <Button
                      mode={draftProvider === "server" ? "contained" : "outlined"}
                      buttonColor={draftProvider === "server" ? "#7c3aed" : undefined}
                      textColor={draftProvider === "server" ? "#f5e9ff" : "#89b4fa"}
                      style={styles.selectorButton}
                      onPress={() => {
                        applyDraftProvider("server");
                        setSlotSelector(null);
                      }}
                    >
                      Server / {currentServerEngineLabel}
                    </Button>
                  ) : null}
                  {visibleStandardDirectProviders.map((provider) => (
                    <Button
                      key={provider}
                      mode={draftProvider === provider ? "contained" : "outlined"}
                      buttonColor={draftProvider === provider ? "#7c3aed" : undefined}
                      textColor={draftProvider === provider ? "#f5e9ff" : "#89b4fa"}
                      style={styles.selectorButton}
                      onPress={() => {
                        applyDraftProvider(provider);
                        setSlotSelector(null);
                      }}
                    >
                      {getProviderDefinition(provider).label}
                    </Button>
                  ))}
                  {visibleAdvancedDirectProviders.length > 0 ? (
                    <Text style={styles.advancedNote}>上級者向け</Text>
                  ) : null}
                  {visibleAdvancedDirectProviders.map((provider) => (
                    <Button
                      key={provider}
                      mode={draftProvider === provider ? "contained" : "outlined"}
                      buttonColor={draftProvider === provider ? "#7c3aed" : undefined}
                      textColor={draftProvider === provider ? "#f5e9ff" : "#89b4fa"}
                      style={styles.selectorButton}
                      onPress={() => {
                        applyDraftProvider(provider);
                        setSlotSelector(null);
                      }}
                    >
                      {getProviderDefinition(provider).label}
                    </Button>
                  ))}
                </>
              ) : null}
              {slotSelector === "model" ? (
                <>
                  <TextInput
                    label="モデルを絞り込み"
                    value={modelFilter}
                    onChangeText={setModelFilter}
                    mode="outlined"
                    style={styles.dialogInput}
                    autoCapitalize="none"
                    dense
                  />
                  {orderedModelChoices.map((id) => (
                    <Button
                      key={id}
                      mode={draftEffectiveModel === id ? "contained" : "outlined"}
                      buttonColor={draftEffectiveModel === id ? "#7c3aed" : undefined}
                      textColor={draftEffectiveModel === id ? "#f5e9ff" : "#89b4fa"}
                      style={styles.selectorButton}
                      onPress={() => {
                        setDraftSelectedModel(id);
                        setDraftCustomModel("");
                        setSlotSelector(null);
                      }}
                    >
                      {modelLabelById.get(id) ?? id}
                    </Button>
                  ))}
                  <Button
                    mode={draftSelectedModel === CUSTOM_MODEL_VALUE ? "contained" : "outlined"}
                    buttonColor={draftSelectedModel === CUSTOM_MODEL_VALUE ? "#7c3aed" : undefined}
                    textColor={draftSelectedModel === CUSTOM_MODEL_VALUE ? "#f5e9ff" : "#89b4fa"}
                    style={styles.selectorButton}
                    onPress={() => {
                      setDraftSelectedModel(CUSTOM_MODEL_VALUE);
                      setSlotSelector(null);
                    }}
                  >
                    カスタムモデルID
                  </Button>
                </>
              ) : null}
              {slotSelector === "effort" ? (
                <>
                  <Button
                    mode={!draftEffort ? "contained" : "outlined"}
                    buttonColor={!draftEffort ? "#7c3aed" : undefined}
                    textColor={!draftEffort ? "#f5e9ff" : "#89b4fa"}
                    style={styles.selectorButton}
                    onPress={() => {
                      setDraftEffort("");
                      setSlotSelector(null);
                    }}
                  >
                    モデル既定
                  </Button>
                  {draftEffortOptions.map((effort) => (
                    <Button
                      key={effort}
                      mode={draftEffort === effort ? "contained" : "outlined"}
                      buttonColor={draftEffort === effort ? "#7c3aed" : undefined}
                      textColor={draftEffort === effort ? "#f5e9ff" : "#89b4fa"}
                      style={styles.selectorButton}
                      onPress={() => {
                        setDraftEffort(effort);
                        setSlotSelector(null);
                      }}
                    >
                      {effort}
                    </Button>
                  ))}
                </>
              ) : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button onPress={() => setSlotSelector(null)}>閉じる</Button>
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
          visible={slotDialog !== null && slotSelector === null}
          onDismiss={() => {
            setSlotSelector(null);
            setSlotDialog(null);
          }}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            {slotDialog === "fallback"
              ? "フォールバック設定"
              : slotDialog === "clipIngest"
                ? "クリップ取り込み設定"
                : "メイン設定"}
          </Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              <Text style={styles.settingHint}>
                {slotDialog === "fallback"
                  ? "メインの送信失敗時のみ使うプロバイダーとモデルを設定します。"
                  : slotDialog === "clipIngest"
                    ? "サーバー未接続時のクリップ取り込みで使うプロバイダーとモデルを設定します。"
                    : "通常の会話応答に使うプロバイダーとモデルを設定します。"}
              </Text>

              {slotDialog === "clipIngest" ? (
                <View style={styles.switchRow}>
                  <View style={styles.switchText}>
                    <Text style={styles.settingLabel}>個別のモデルを指定する</Text>
                    <Text style={styles.settingHint}>
                      オフの間はメインと同じモデルを使います。
                    </Text>
                  </View>
                  <Switch
                    accessibilityLabel="クリップ取り込みの個別指定切替"
                    value={draftClipIngestEnabled}
                    onValueChange={setDraftClipIngestEnabled}
                  />
                </View>
              ) : null}

              {slotDialog === "fallback" ? (
                <View style={styles.switchRow}>
                  <View style={styles.switchText}>
                    <Text style={styles.settingLabel}>
                      フォールバックを有効にする
                    </Text>
                    <Text style={styles.settingHint}>
                      無効の間はメインが失敗しても切り替えません。
                    </Text>
                  </View>
                  <Switch
                    value={draftFallbackEnabled}
                    onValueChange={setDraftFallbackEnabled}
                  />
                </View>
              ) : null}

              {slotFieldsVisible ? (
                <>
                  <SelectorRow
                    label="プロバイダー"
                    value={
                      draftProvider === "server"
                        ? `Server / ${currentServerEngineLabel}`
                        : getProviderDefinition(draftProvider).label
                    }
                    onPress={() => setSlotSelector("provider")}
                  />

              {isDirectProvider(draftProvider) ? (
                <>
                  {getProviderDefinition(draftProvider).hint ? (
                    <Text style={styles.settingHint}>
                      {getProviderDefinition(draftProvider).hint}
                    </Text>
                  ) : null}

                  <View style={styles.modelHeaderRow}>
                    <SelectorRow
                      label="モデル"
                      value={
                        draftEffectiveModel ||
                        (draftSelectedModel === CUSTOM_MODEL_VALUE
                          ? "カスタムモデルID"
                          : "未選択")
                      }
                      onPress={() => setSlotSelector("model")}
                    />
                    <View style={styles.modelHeaderActions}>
                      {modelListLoading ? (
                        <ActivityIndicator size={16} color="#89b4fa" />
                      ) : null}
                      <Button
                        mode="text"
                        compact
                        textColor="#89b4fa"
                        disabled={modelListLoading}
                        onPress={handleRefreshModels}
                      >
                        {modelListLoading ? "取得中" : "再取得"}
                      </Button>
                    </View>
                  </View>
                  {modelListError ? (
                    <Text style={styles.settingHint}>{modelListError}</Text>
                  ) : null}
                  {draftSelectedModel === CUSTOM_MODEL_VALUE ? (
                    <TextInput
                      label="モデルID"
                      value={draftCustomModel}
                      onChangeText={setDraftCustomModel}
                      mode="outlined"
                      style={styles.dialogInput}
                      autoCapitalize="none"
                    />
                  ) : null}

                  <Text style={styles.dialogSubheading}>APIキー</Text>
                  <Text style={styles.settingHint}>
                    このプロバイダーのメイン／フォールバック共通で使います。
                  </Text>
                  <TextInput
                    label="APIキー"
                    value={draftApiKey}
                    onChangeText={setDraftApiKey}
                    onBlur={() => {
                      // キー入力後にフォーカスを外したら最新一覧を取り直す。
                      if (draftApiKey.trim()) handleRefreshModels();
                    }}
                    mode="outlined"
                    style={styles.dialogInput}
                    secureTextEntry
                    autoCapitalize="none"
                  />

                  {getProviderDefinition(draftProvider).baseUrlEditable ? (
                    <TextInput
                      label={
                        getProviderDefinition(draftProvider).baseUrlRequired
                          ? "Base URL（必須）"
                          : "Base URL"
                      }
                      value={draftBaseUrl}
                      onChangeText={setDraftBaseUrl}
                      onBlur={handleRefreshModels}
                      mode="outlined"
                      style={styles.dialogInput}
                      autoCapitalize="none"
                    />
                  ) : null}

                  <Button
                    mode="text"
                    compact
                    textColor="#89b4fa"
                    loading={connTesting}
                    disabled={connTesting}
                    onPress={() => void handleTestConnection()}
                    style={styles.inlineButton}
                  >
                    接続テスト
                  </Button>
                  {connResult ? (
                    <Text
                      style={connResult.ok ? styles.savedText : styles.errorText}
                    >
                      {connResult.message ??
                        (connResult.ok ? "接続に成功しました。" : "接続に失敗しました。")}
                    </Text>
                  ) : null}
                </>
              ) : null}

              {draftEffortOptions.length > 0 && isDirectProvider(draftProvider) ? (
                <>
                  <SelectorRow
                    label="Effort"
                    value={draftEffort || "モデル既定"}
                    onPress={() => setSlotSelector("effort")}
                  />
                </>
              ) : null}
                </>
              ) : null}

              {slotSaveError ? (
                <Text style={styles.errorText}>{slotSaveError}</Text>
              ) : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button onPress={() => setSlotDialog(null)}>閉じる</Button>
            <Button
              loading={slotSaving}
              disabled={slotSaving}
              onPress={() => void handleSaveSlot()}
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
                {visibleServerEngineOptions.map((option) => (
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

      </Portal>
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#10101b" },
  content: { paddingBottom: 32 },
  section: {
    marginHorizontal: 14,
    marginTop: 12,
    paddingHorizontal: 10,
    paddingTop: 14,
    paddingBottom: 6,
    backgroundColor: "#1b1a2b",
    borderColor: "#343149",
    borderWidth: 1,
    borderRadius: 14,
  },
  sectionTitle: {
    color: "#bd9aff",
    fontSize: 15,
    fontWeight: "600",
    marginHorizontal: 7,
    marginBottom: 8,
  },
  helperText: {
    color: "#a9a7c5",
    fontSize: 11,
    lineHeight: 16,
    marginHorizontal: 7,
    marginBottom: 4,
  },
  accountCard: {
    backgroundColor: "transparent",
    padding: 0,
  },
  accountHeaderRow: {
    minHeight: 52,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  accountAvatar: {
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: "#9558ff",
    alignItems: "center",
    justifyContent: "center",
  },
  accountAvatarText: { color: "#ffffff", fontSize: 16, fontWeight: "600" },
  accountMainText: { flex: 1 },
  accountActions: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginTop: 6,
    paddingBottom: 2,
  },
  radioLabel: { color: "#e5e3ff" },
  selectorRow: {
    minHeight: 54,
    flexDirection: "row",
    alignItems: "center",
    borderBottomWidth: 1,
    borderBottomColor: "#2b2940",
    paddingVertical: 8,
    gap: 8,
  },
  selectorRowDisabled: { opacity: 0.5 },
  selectorRowText: { flex: 1 },
  selectorChevron: { color: "#89b4fa", fontSize: 24, lineHeight: 24 },
  selectorDialogContent: { paddingVertical: 4, gap: 8 },
  selectorButton: { marginVertical: 2 },
  navTitle: { color: "#f0efff", fontSize: 13, fontWeight: "500" },
  navDescription: { color: "#a9a7c5", fontSize: 11, lineHeight: 15 },
  docsResyncProgress: {
    paddingHorizontal: 16,
    paddingBottom: 12,
    gap: 6,
  },
  docsResyncProgressBar: {
    height: 6,
    borderRadius: 3,
    backgroundColor: "#343149",
  },
  docsResyncProgressText: {
    color: "#a9a7c5",
    fontSize: 11,
    textAlign: "right",
  },
  divider: { height: 0, backgroundColor: "transparent" },
  innerDivider: { backgroundColor: "#2b2940", marginVertical: 4 },
  accordion: { backgroundColor: "transparent", padding: 0 },
  loader: { paddingVertical: 16 },
  input: { marginBottom: 8 },
  saveButton: { borderColor: "#7c3aed" },
  modelSlotList: { gap: 0, marginTop: 2 },
  modelSlotCard: {
    backgroundColor: "transparent",
    borderBottomColor: "#2b2940",
    borderBottomWidth: 1,
    paddingVertical: 6,
    paddingHorizontal: 0,
  },
  modelSlotHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  modelSlotText: { flex: 1 },
  slotActions: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
    marginTop: 2,
  },
  fallbackSlotControls: {
    alignItems: "center",
    gap: 4,
  },
  dialogSubheading: {
    color: "#cdd6f4",
    fontSize: 13,
    fontWeight: "600",
    marginTop: 16,
    marginBottom: 4,
  },
  advancedNote: {
    color: "#f9e2af",
    fontSize: 12,
    marginBottom: 4,
  },
  errorText: { color: "#f38ba8", marginTop: 10, fontSize: 13 },
  serverEngineBox: {
    backgroundColor: "#1e1e2e",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
    marginBottom: 12,
  },
  settingLabel: { color: "#a9a7c5", fontSize: 11, marginBottom: 2 },
  settingValue: { color: "#f0efff", fontSize: 13, fontWeight: "500" },
  settingHint: { color: "#a9a7c5", fontSize: 11, lineHeight: 15, marginTop: 2 },
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
  compactButton: { borderColor: "#9558ff", alignSelf: "flex-start" },
  engineChangeButton: { marginTop: 10 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogInput: { marginTop: 12 },
  savedText: { color: "#a6e3a1", marginTop: 10, fontSize: 13 },
  modelHeaderRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  modelHeaderActions: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  modelListBox: {
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 8,
    marginTop: 8,
    overflow: "hidden",
  },
  modelList: { maxHeight: 240 },
  dialogScrollArea: { maxHeight: 420, borderColor: "#313244" },
  dialogScrollContent: { paddingVertical: 4 },
  inlineButton: { marginTop: 8, borderColor: "#7c3aed" },
  version: {
    color: "#777294",
    textAlign: "center",
    marginTop: 18,
    fontSize: 11,
  },
});
