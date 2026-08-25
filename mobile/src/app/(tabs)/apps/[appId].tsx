import React, { useCallback, useMemo, useState } from "react";
import {
  Alert,
  RefreshControl,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Button,
  Chip,
  Dialog,
  Divider,
  Icon,
  IconButton,
  Menu,
  Portal,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import {
  useFocusEffect,
  useLocalSearchParams,
  useRouter,
} from "expo-router";
import { ScreenHeader } from "../../../components/screen-header";
import { useNetworkStore } from "../../../stores/network";
import { useProject } from "../../../contexts/ProjectContext";
import {
  appsRepo,
} from "../../../repositories/apps";
import {
  permissionAtLeast,
  type AppContext,
  type AppFile,
  type AppFileContent,
  type AppJob,
  type AppRelease,
  type AppSummary,
  type AppTarget,
  type ProjectAppBinding,
  type TaskAppLink,
} from "../../../lib/apps-api";
import { createCurrentCharacterSession } from "../../../features/characters/current-character";

const BACKGROUND = "#11111b";
const SURFACE = "#1e1e2e";
const TEXT = "#cdd6f4";
const MUTED = "#a6adc8";

type AppDetailData = {
  app: AppSummary | null;
  context: AppContext | null;
  targets: AppTarget[];
  files: AppFile[];
  releases: AppRelease[];
  jobs: AppJob[];
  projectBindings: ProjectAppBinding[];
  taskLinks: Array<TaskAppLink & { task?: Record<string, unknown> }>;
};

function getParam(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function formatJobStatus(job: AppJob): string {
  if (job.exit_code !== null && job.exit_code !== undefined) {
    return `${job.status} (exit ${job.exit_code})`;
  }
  return job.status;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Surface style={styles.section} elevation={1}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {children}
    </Surface>
  );
}

export default function AppDetailScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ appId: string; projectId?: string | string[] }>();
  const appId = getParam(params.appId);
  const projectId = getParam(params.projectId) || undefined;
  const { selectedProjectId } = useProject();
  const online = useNetworkStore((state) => state.online);
  const effectiveProjectId = projectId || selectedProjectId || undefined;
  const [data, setData] = useState<AppDetailData>({
    app: null,
    context: null,
    targets: [],
    files: [],
    releases: [],
    jobs: [],
    projectBindings: [],
    taskLinks: [],
  });
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedTarget, setSelectedTarget] = useState<string | null>(null);
  const [busyJob, setBusyJob] = useState(false);
  const [logs, setLogs] = useState<string | null>(null);
  const [linkBusy, setLinkBusy] = useState(false);
  const [metadataDialogVisible, setMetadataDialogVisible] = useState(false);
  const [archiveDialogVisible, setArchiveDialogVisible] = useState(false);
  const [metadataSaving, setMetadataSaving] = useState(false);
  const [metadataName, setMetadataName] = useState("");
  const [metadataDescription, setMetadataDescription] = useState("");
  const [metadataVisibility, setMetadataVisibility] = useState<
    "private" | "shared" | "public"
  >("private");
  const [metadataTargetKey, setMetadataTargetKey] = useState<string | null>(null);
  const [visibilityMenuVisible, setVisibilityMenuVisible] = useState(false);
  const [targetMenuVisible, setTargetMenuVisible] = useState(false);
  const [bindingDialogVisible, setBindingDialogVisible] = useState(false);
  const [bindingSaving, setBindingSaving] = useState(false);
  const [bindingMode, setBindingMode] = useState<"development" | "installed">(
    "development",
  );
  const [bindingEnabled, setBindingEnabled] = useState(true);
  const [bindingPinned, setBindingPinned] = useState(false);
  const [bindingReleaseId, setBindingReleaseId] = useState<string | null>(null);
  const [releaseMenuVisible, setReleaseMenuVisible] = useState(false);
  const [filePreview, setFilePreview] = useState<{
    path: string;
    content: AppFileContent | null;
    loading: boolean;
    error?: string;
  } | null>(null);

  const load = useCallback(async () => {
    if (!appId) return;
    setError(null);
    try {
      const [app, context, targets, files, releases, jobs, projectBindings, taskLinks] =
        await Promise.all([
          appsRepo.get(appId, { projectId: effectiveProjectId }),
          appsRepo.getContext(appId, { projectId: effectiveProjectId }),
          appsRepo.listTargets(appId, { projectId: effectiveProjectId }),
          appsRepo.listFiles(appId, { projectId: effectiveProjectId }),
          appsRepo.listReleases(appId, { projectId: effectiveProjectId }),
          appsRepo.listJobs(appId, { projectId: effectiveProjectId }),
          effectiveProjectId ? appsRepo.listProjectApps(effectiveProjectId) : Promise.resolve([]),
          effectiveProjectId
            ? appsRepo.listAppTasks(appId, effectiveProjectId)
            : Promise.resolve([] as TaskAppLink[]),
        ]);
      setData({
        app,
        context,
        targets,
        files,
        releases,
        jobs,
        projectBindings,
        taskLinks,
      });
      setSelectedTarget((current) =>
        current && targets.some((target) => target.target_key === current)
          ? current
          : context?.target_key || targets[0]?.target_key || null,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "App詳細を読み込めませんでした");
    } finally {
      setLoading(false);
    }
  }, [appId, effectiveProjectId]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const permission = data.context?.permission ?? data.app?.permission;
  const canRun = permissionAtLeast(permission, "runner");
  const canAdmin = permissionAtLeast(permission, "admin");
  const binding = useMemo(
    () => data.projectBindings.find((item) => item.app_id === appId),
    [appId, data.projectBindings],
  );
  const selectedTargetObject = data.targets.find(
    (target) => target.target_key === selectedTarget,
  );

  const handleRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const openMetadataDialog = () => {
    if (!data.app) return;
    setMetadataName(data.app.name);
    setMetadataDescription(data.app.description ?? "");
    setMetadataVisibility(
      data.app.visibility === "shared" || data.app.visibility === "public"
        ? data.app.visibility
        : "private",
    );
    setMetadataTargetKey(data.app.default_target_key ?? null);
    setMetadataDialogVisible(true);
  };

  const handleMetadataSave = async () => {
    if (!data.app || !canAdmin || !online) return;
    const name = metadataName.trim();
    if (!name) {
      Alert.alert("App設定", "App名を入力してください");
      return;
    }
    setMetadataSaving(true);
    try {
      const updated = await appsRepo.update(
        data.app.id,
        {
          name,
          description: metadataDescription.trim(),
          visibility: metadataVisibility,
          default_target_key: metadataTargetKey,
        },
        { projectId: effectiveProjectId, permission },
      );
      setData((previous) => ({
        ...previous,
        app: updated,
        context: previous.context
          ? { ...previous.context, app: updated }
          : previous.context,
      }));
      setMetadataDialogVisible(false);
    } catch (cause) {
      Alert.alert(
        "App設定",
        cause instanceof Error ? cause.message : "App設定を保存できませんでした",
      );
    } finally {
      setMetadataSaving(false);
    }
  };

  const handleArchive = async () => {
    if (!data.app || !canAdmin || !online) return;
    setMetadataSaving(true);
    try {
      await appsRepo.archive(data.app.id, {
        projectId: effectiveProjectId,
        permission,
      });
      setArchiveDialogVisible(false);
      router.replace("/(tabs)/apps");
    } catch (cause) {
      Alert.alert(
        "Appをアーカイブ",
        cause instanceof Error ? cause.message : "Appをアーカイブできませんでした",
      );
    } finally {
      setMetadataSaving(false);
    }
  };

  const openBindingDialog = () => {
    if (!binding) return;
    setBindingMode(binding.binding_mode);
    setBindingEnabled(binding.enabled);
    setBindingPinned(binding.pinned);
    setBindingReleaseId(binding.installed_release_id ?? data.releases[0]?.id ?? null);
    setBindingDialogVisible(true);
  };

  const handleBindingSave = async () => {
    if (!binding || !effectiveProjectId || !canRun || !online) return;
    if (bindingMode === "installed" && !bindingReleaseId) {
      Alert.alert("Project App", "installed モードでは Release を選択してください");
      return;
    }
    setBindingSaving(true);
    try {
      const updated = await appsRepo.updateProjectApp(
        effectiveProjectId,
        binding.app_id,
        {
          binding_mode: bindingMode,
          enabled: bindingEnabled,
          pinned: bindingPinned,
          installed_release_id:
            bindingMode === "installed" ? bindingReleaseId : null,
        },
        permission,
      );
      setData((previous) => ({
        ...previous,
        projectBindings: [
          ...previous.projectBindings.filter((item) => item.app_id !== updated.app_id),
          updated,
        ],
      }));
      setBindingDialogVisible(false);
    } catch (cause) {
      Alert.alert(
        "Project App",
        cause instanceof Error ? cause.message : "Project App設定を保存できませんでした",
      );
    } finally {
      setBindingSaving(false);
    }
  };

  const handleFilePreview = async (file: AppFile) => {
    if (file.is_dir) return;
    const targetAppId = data.app?.id ?? appId;
    if (!targetAppId) return;
    setFilePreview({ path: file.path, content: null, loading: true });
    try {
      const content = await appsRepo.getFile(targetAppId, file.path, {
        projectId: effectiveProjectId,
      });
      setFilePreview({
        path: file.path,
        content,
        loading: false,
        ...(content ? {} : { error: "ファイル内容を取得できませんでした" }),
      });
    } catch (cause) {
      setFilePreview({
        path: file.path,
        content: null,
        loading: false,
        error: cause instanceof Error ? cause.message : "ファイル内容を取得できませんでした",
      });
    }
  };

  const handleOpenChat = async () => {
    if (!data.app) return;
    try {
      const session = await createCurrentCharacterSession(effectiveProjectId, {
        localFirst: true,
      });
      router.push({
        pathname: "/(tabs)/chat/[sessionId]",
        params: {
          sessionId: session.id,
          appId: data.app.id,
          appTargetId: selectedTargetObject?.id || "",
          projectId: effectiveProjectId || "",
        },
      });
    } catch (cause) {
      Alert.alert("Chat", cause instanceof Error ? cause.message : "Chatを開けませんでした");
    }
  };

  const handleStartJob = async () => {
    if (!data.app || !selectedTarget || !canRun || !online) return;
    setBusyJob(true);
    try {
      const job = await appsRepo.startJob(
        data.app.id,
        {
          target_key: selectedTarget,
          job_type: "run",
          project_id: effectiveProjectId,
        },
        permission,
      );
      setData((previous) => ({ ...previous, jobs: [job, ...previous.jobs.filter((item) => item.id !== job.id)] }));
    } catch (cause) {
      Alert.alert("App実行", cause instanceof Error ? cause.message : "実行を開始できませんでした");
    } finally {
      setBusyJob(false);
    }
  };

  const handleStopJob = async (job: AppJob) => {
    if (!data.app || !canRun || !online) return;
    setBusyJob(true);
    try {
      const next = await appsRepo.stopJob(data.app.id, job.id, {
        projectId: effectiveProjectId,
        permission,
      });
      setData((previous) => ({ ...previous, jobs: [next, ...previous.jobs.filter((item) => item.id !== next.id)] }));
    } catch (cause) {
      Alert.alert("App実行", cause instanceof Error ? cause.message : "停止できませんでした");
    } finally {
      setBusyJob(false);
    }
  };

  const handleLogs = async (job: AppJob) => {
    if (!data.app) return;
    const result = await appsRepo.getJobLogs(data.app.id, job.id, {
      projectId: effectiveProjectId,
    });
    setLogs(result?.logs || "ログはありません");
  };

  const handleProjectLink = async () => {
    if (!data.app || !effectiveProjectId || !canRun || !online) return;
    setLinkBusy(true);
    try {
      if (binding) {
        await appsRepo.unlinkProjectApp(effectiveProjectId, data.app.id, permission);
        setData((previous) => ({
          ...previous,
          projectBindings: previous.projectBindings.filter((item) => item.app_id !== data.app?.id),
        }));
      } else {
        const linked = await appsRepo.linkProjectApp(
          effectiveProjectId,
          { app_id: data.app.id, binding_mode: "development", enabled: true, pinned: false },
          permission,
        );
        setData((previous) => ({
          ...previous,
          projectBindings: [...previous.projectBindings.filter((item) => item.app_id !== linked.app_id), linked],
        }));
      }
    } catch (cause) {
      Alert.alert("Project連携", cause instanceof Error ? cause.message : "Project連携を変更できませんでした");
    } finally {
      setLinkBusy(false);
    }
  };

  if (!appId) {
    return <View style={styles.container}><ScreenHeader title="App" onBack={() => router.back()} /><Text style={styles.error}>App IDがありません</Text></View>;
  }

  return (
    <View style={styles.container}>
      <ScreenHeader
        title={data.app?.name || "App"}
        subtitle={data.app?.slug}
        onBack={() => router.back()}
        right={
          <View style={styles.headerActions}>
            <IconButton
              icon="pencil-outline"
              iconColor="#89b4fa"
              onPress={openMetadataDialog}
              disabled={!data.app || !canAdmin || !online}
              accessibilityLabel="App情報を編集"
            />
            <IconButton
              icon="archive-outline"
              iconColor="#f38ba8"
              onPress={() => setArchiveDialogVisible(true)}
              disabled={!data.app || !canAdmin || !online}
              accessibilityLabel="Appをアーカイブ"
            />
            <IconButton
              icon="chat-outline"
              iconColor="#89b4fa"
              onPress={() => void handleOpenChat()}
              disabled={!data.app}
              accessibilityLabel="AppコンテキストでChatを開く"
            />
          </View>
        }
      />
      {error ? <Text style={styles.error}>{error}</Text> : null}
      {loading && !data.app ? (
        <View style={styles.centered}><ActivityIndicator size="large" color="#89b4fa" /></View>
      ) : (
        <ScrollView
          contentContainerStyle={styles.content}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={handleRefresh} tintColor="#89b4fa" />}
        >
          <Surface style={styles.identityCard} elevation={1}>
            <View style={styles.identityIcon}><Icon source="application-brackets-outline" size={32} color="#89b4fa" /></View>
            <View style={styles.identityCopy}>
              <Text style={styles.identityTitle}>{data.app?.name || "App"}</Text>
              <Text style={styles.identityMeta}>{data.app?.slug || ""} · {permission || "権限情報なし"}</Text>
              {data.app?.description ? <Text style={styles.identityDescription}>{data.app.description}</Text> : null}
            </View>
          </Surface>
          {!canAdmin ? <Text style={styles.mutedText}>viewer/runner は App metadata編集・アーカイブを実行できません</Text> : null}
          {canAdmin && !online ? <Text style={styles.offlineText}>オフラインでは App metadata編集・アーカイブを実行できません</Text> : null}

          {effectiveProjectId ? (
            <Section title="Projectとの関係">
              <Text style={styles.bodyText}>{binding ? `${binding.binding_mode} / ${binding.enabled ? "有効" : "無効"} / ${binding.pinned ? "固定" : "未固定"}` : "このProjectには未連携です"}</Text>
              {binding?.binding_mode === "installed" ? (
                <Text style={styles.mutedText}>
                  Release: {binding.installed_release_id || "未選択"}
                </Text>
              ) : null}
              {binding ? (
                <Button
                  mode="outlined"
                  icon="tune-variant"
                  onPress={openBindingDialog}
                  disabled={!canRun || !online || linkBusy || bindingSaving}
                  style={styles.actionButton}
                >
                  Project App設定
                </Button>
              ) : null}
              <Button
                mode="outlined"
                onPress={() => void handleProjectLink()}
                disabled={!canRun || !online || linkBusy}
                loading={linkBusy}
                style={styles.actionButton}
              >
                {binding ? "Project連携を解除" : "Projectに追加"}
              </Button>
              {!canRun ? <Text style={styles.mutedText}>viewer は Project連携を変更できません</Text> : null}
            </Section>
          ) : null}

          <Section title={`関連 Tasks (${data.taskLinks.length})`}>
            {data.taskLinks.length === 0 ? (
              <Text style={styles.mutedText}>関連付けられた Task はありません</Text>
            ) : (
              data.taskLinks.slice(0, 20).map((link) => (
                <Button
                  key={link.id}
                  mode="text"
                  icon="checkbox-marked-outline"
                  contentStyle={styles.taskLinkButtonContent}
                  onPress={() => router.push({ pathname: "/(tabs)/tasks/[taskId]", params: { taskId: link.task_id } })}
                >
                  {(link as TaskAppLink & { task?: Record<string, unknown> }).task?.title as string || link.task_id} · {link.relation_type}
                </Button>
              ))
            )}
          </Section>

          <Section title="Chat App Context">
            <Text style={styles.bodyText}>{data.context?.readme?.trim() || "App Context の README はありません"}</Text>
            <Button mode="contained-tonal" icon="chat-outline" onPress={() => void handleOpenChat()} disabled={!data.app} style={styles.actionButton}>
              このAppでChatを開く
            </Button>
          </Section>

          <Section title={`Targets (${data.targets.length})`}>
            {data.targets.length === 0 ? <Text style={styles.mutedText}>Targetはありません</Text> : null}
            <View style={styles.chipRow}>
              {data.targets.map((target) => (
                <Chip key={target.id} selected={selectedTarget === target.target_key} onPress={() => setSelectedTarget(target.target_key)} style={styles.chip}>
                  {target.display_name || target.target_key}
                </Chip>
              ))}
            </View>
            <Button mode="contained" icon="play" onPress={() => void handleStartJob()} disabled={!canRun || !online || !selectedTarget || busyJob} loading={busyJob} style={styles.actionButton}>
              実行
            </Button>
            {!canRun ? <Text style={styles.mutedText}>viewer には実行ボタンを表示しません（runner権限が必要）</Text> : null}
            {!online ? <Text style={styles.offlineText}>オフラインでは実行できません</Text> : null}
          </Section>

          <Section title={`実行履歴 (${data.jobs.length})`}>
            {data.jobs.length === 0 ? <Text style={styles.mutedText}>実行履歴はありません</Text> : null}
            {data.jobs.slice(0, 20).map((job) => (
              <View key={job.id} style={styles.jobRow}>
                <View style={styles.jobCopy}>
                  <Text style={styles.bodyText}>{job.job_type} · {formatJobStatus(job)}</Text>
                  <Text style={styles.mutedText}>{job.started_at || job.ended_at || "時刻情報なし"}</Text>
                </View>
                <View style={styles.jobActions}>
                  <IconButton icon="text-box-outline" iconColor="#89b4fa" onPress={() => void handleLogs(job)} accessibilityLabel="ジョブログを表示" />
                  {canRun && online && ["queued", "running", "started"].includes(job.status) ? <IconButton icon="stop-circle-outline" iconColor="#f38ba8" onPress={() => void handleStopJob(job)} disabled={busyJob} accessibilityLabel="ジョブを停止" /> : null}
                </View>
              </View>
            ))}
          </Section>

          <Section title={`Files (${data.files.length})`}>
            <Text style={styles.mutedText}>読み取り専用。モバイルから source import / ファイル編集はできません。</Text>
            {data.files.slice(0, 30).map((file) => (
              <Button
                key={file.path}
                mode="text"
                icon={file.is_dir ? "folder-outline" : "file-document-outline"}
                contentStyle={styles.fileButtonContent}
                onPress={() => void handleFilePreview(file)}
                disabled={Boolean(file.is_dir)}
                accessibilityLabel={file.is_dir ? `${file.path}フォルダー` : `${file.path}をプレビュー`}
              >
                {file.path}
              </Button>
            ))}
          </Section>

          <Section title={`Releases (${data.releases.length})`}>
            <Text style={styles.mutedText}>Releaseの閲覧のみ。作成・公開はWeb/desktopで行います。</Text>
            {data.releases.slice(0, 20).map((release) => <Text key={release.id} style={styles.fileRow}>{release.version} · {release.status}</Text>)}
          </Section>

          <Divider style={styles.bottomDivider} />
          <Text style={styles.mutedText}>Git復元、README/manifest編集、Grant管理、embedded runtime はモバイル対象外です。</Text>
        </ScrollView>
      )}
      <Portal>
        <Dialog visible={logs !== null} onDismiss={() => setLogs(null)}>
          <Dialog.Title>ジョブログ</Dialog.Title>
          <Dialog.Content><Text selectable style={styles.logText}>{logs || ""}</Text></Dialog.Content>
          <Dialog.Actions><Button onPress={() => setLogs(null)}>閉じる</Button></Dialog.Actions>
        </Dialog>
        <Dialog
          visible={metadataDialogVisible}
          onDismiss={() => !metadataSaving && setMetadataDialogVisible(false)}
        >
          <Dialog.Title>App情報を編集</Dialog.Title>
          <Dialog.Content>
            <TextInput
              label="App名"
              mode="outlined"
              value={metadataName}
              onChangeText={setMetadataName}
              style={styles.dialogInput}
            />
            <TextInput
              label="説明"
              mode="outlined"
              value={metadataDescription}
              onChangeText={setMetadataDescription}
              multiline
              style={styles.dialogInput}
            />
            <Text style={styles.dialogLabel}>公開範囲</Text>
            <Menu
              visible={visibilityMenuVisible}
              onDismiss={() => setVisibilityMenuVisible(false)}
              anchor={
                <Button mode="outlined" onPress={() => setVisibilityMenuVisible(true)}>
                  {metadataVisibility}
                </Button>
              }
            >
              {(["private", "shared", "public"] as const).map((value) => (
                <Menu.Item
                  key={value}
                  title={value}
                  leadingIcon={metadataVisibility === value ? "check" : undefined}
                  onPress={() => {
                    setMetadataVisibility(value);
                    setVisibilityMenuVisible(false);
                  }}
                />
              ))}
            </Menu>
            <Text style={styles.dialogLabel}>既定Target</Text>
            <Menu
              visible={targetMenuVisible}
              onDismiss={() => setTargetMenuVisible(false)}
              anchor={
                <Button mode="outlined" onPress={() => setTargetMenuVisible(true)}>
                  {metadataTargetKey || "未選択"}
                </Button>
              }
            >
              <Menu.Item
                title="未選択"
                onPress={() => {
                  setMetadataTargetKey(null);
                  setTargetMenuVisible(false);
                }}
              />
              {data.targets.map((target) => (
                <Menu.Item
                  key={target.id}
                  title={target.target_key}
                  leadingIcon={metadataTargetKey === target.target_key ? "check" : undefined}
                  onPress={() => {
                    setMetadataTargetKey(target.target_key);
                    setTargetMenuVisible(false);
                  }}
                />
              ))}
            </Menu>
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setMetadataDialogVisible(false)} disabled={metadataSaving}>キャンセル</Button>
            <Button onPress={() => void handleMetadataSave()} loading={metadataSaving} disabled={!canAdmin || !online || metadataSaving}>保存</Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={archiveDialogVisible}
          onDismiss={() => !metadataSaving && setArchiveDialogVisible(false)}
        >
          <Dialog.Title>Appをアーカイブ</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.bodyText}>「{data.app?.name || "App"}」をアーカイブしますか？</Text>
            {!online ? <Text style={styles.offlineText}>オフラインでは実行できません</Text> : null}
            {!canAdmin ? <Text style={styles.mutedText}>admin権限が必要です</Text> : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setArchiveDialogVisible(false)} disabled={metadataSaving}>キャンセル</Button>
            <Button
              onPress={() => void handleArchive()}
              loading={metadataSaving}
              disabled={!canAdmin || !online || metadataSaving}
              textColor="#f38ba8"
            >
              アーカイブ
            </Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={bindingDialogVisible}
          onDismiss={() => !bindingSaving && setBindingDialogVisible(false)}
        >
          <Dialog.Title>Project App設定</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogLabel}>Binding mode</Text>
            <View style={styles.chipRow}>
              {(["development", "installed"] as const).map((value) => (
                <Chip
                  key={value}
                  selected={bindingMode === value}
                  onPress={() => setBindingMode(value)}
                  style={styles.chip}
                >
                  {value}
                </Chip>
              ))}
            </View>
            <View style={styles.switchRow}>
              <Text style={styles.bodyText}>有効</Text>
              <Switch value={bindingEnabled} onValueChange={setBindingEnabled} />
            </View>
            <View style={styles.switchRow}>
              <Text style={styles.bodyText}>Pinned</Text>
              <Switch value={bindingPinned} onValueChange={setBindingPinned} />
            </View>
            {bindingMode === "installed" ? (
              <>
                <Text style={styles.dialogLabel}>Installed Release</Text>
                <Menu
                  visible={releaseMenuVisible}
                  onDismiss={() => setReleaseMenuVisible(false)}
                  anchor={
                    <Button mode="outlined" onPress={() => setReleaseMenuVisible(true)}>
                      {data.releases.find((release) => release.id === bindingReleaseId)?.version || "選択してください"}
                    </Button>
                  }
                >
                  {data.releases.map((release) => (
                    <Menu.Item
                      key={release.id}
                      title={`${release.version} (${release.status})`}
                      disabled={release.status !== "published"}
                      leadingIcon={bindingReleaseId === release.id ? "check" : undefined}
                      onPress={() => {
                        setBindingReleaseId(release.id);
                        setReleaseMenuVisible(false);
                      }}
                    />
                  ))}
                </Menu>
              </>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setBindingDialogVisible(false)} disabled={bindingSaving}>キャンセル</Button>
            <Button
              onPress={() => void handleBindingSave()}
              loading={bindingSaving}
              disabled={!canRun || !online || bindingSaving || (bindingMode === "installed" && !bindingReleaseId)}
            >
              保存
            </Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={filePreview !== null}
          onDismiss={() => setFilePreview(null)}
        >
          <Dialog.Title>{filePreview?.path || "ファイル"}</Dialog.Title>
          <Dialog.Content>
            {filePreview?.loading ? <ActivityIndicator color="#89b4fa" /> : null}
            {filePreview?.error ? <Text style={styles.error}>{filePreview.error}</Text> : null}
            {filePreview?.content ? (
              <ScrollView style={styles.previewScroll}>
                <Text selectable style={styles.previewText}>{filePreview.content.content}</Text>
              </ScrollView>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions><Button onPress={() => setFilePreview(null)}>閉じる</Button></Dialog.Actions>
        </Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: BACKGROUND },
  centered: { flex: 1, justifyContent: "center", alignItems: "center" },
  content: { padding: 12, paddingBottom: 48 },
  headerActions: { flexDirection: "row", alignItems: "center" },
  identityCard: { flexDirection: "row", alignItems: "center", backgroundColor: SURFACE, padding: 16, borderRadius: 14, marginBottom: 10 },
  identityIcon: { width: 52, height: 52, borderRadius: 26, backgroundColor: "#313244", justifyContent: "center", alignItems: "center", marginRight: 12 },
  identityCopy: { flex: 1 },
  identityTitle: { color: TEXT, fontSize: 20, fontWeight: "700" },
  identityMeta: { color: MUTED, marginTop: 3 },
  identityDescription: { color: "#bac2de", marginTop: 8, lineHeight: 19 },
  section: { backgroundColor: SURFACE, padding: 14, borderRadius: 14, marginBottom: 10 },
  sectionTitle: { color: TEXT, fontWeight: "700", fontSize: 16, marginBottom: 10 },
  bodyText: { color: TEXT, lineHeight: 20 },
  mutedText: { color: MUTED, fontSize: 12, lineHeight: 18 },
  offlineText: { color: "#f9e2af", marginTop: 8, fontSize: 12 },
  actionButton: { alignSelf: "flex-start", marginTop: 12 },
  taskLinkButtonContent: { justifyContent: "flex-start" },
  fileButtonContent: { justifyContent: "flex-start" },
  dialogInput: { marginBottom: 10 },
  dialogLabel: { color: MUTED, marginTop: 8, marginBottom: 6 },
  switchRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginTop: 8 },
  previewScroll: { maxHeight: 360 },
  previewText: { color: TEXT, fontFamily: "monospace", fontSize: 12 },
  chipRow: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  chip: { marginBottom: 4 },
  jobRow: { flexDirection: "row", alignItems: "center", borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: "#313244", paddingVertical: 8 },
  jobCopy: { flex: 1 },
  jobActions: { flexDirection: "row" },
  fileRow: { color: "#bac2de", fontSize: 13, paddingVertical: 3 },
  bottomDivider: { marginVertical: 12, backgroundColor: "#313244" },
  error: { color: "#f38ba8", padding: 16 },
  logText: { color: TEXT, fontFamily: "monospace", maxHeight: 320 },
});
