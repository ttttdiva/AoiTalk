import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ActivityIndicator,
  Platform,
  StyleSheet,
  View,
} from "react-native";
import { Button, Text } from "react-native-paper";
import { useLocalSearchParams, useRouter } from "expo-router";
import { ThemedTextInput } from "../../../components/themed-text-input";
import { ScreenHeader } from "../../../components/screen-header";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import { SOURCE_LABELS } from "../../../features/files/file-browser-model";
import { parseFilesTextEditorParams } from "../../../features/files/files-text-editor-route";
import {
  filesApi,
  isProjectFilesNamespacePath,
  parseProjectFilesPath,
} from "../../../lib/files-api";
import { goBackOrReplace } from "../../../lib/navigation";
import { getProjectCapabilities } from "../../../lib/project-api";
import {
  isServerKnownUnreachable,
  useNetworkStore,
} from "../../../stores/network";

export default function FilesTextEditorScreen() {
  const router = useRouter();
  const { isAuthenticated, user } = useAuth();
  const { projects } = useProject();
  const networkConnected = useNetworkStore(
    (state) => state.connected !== false,
  );
  const networkServerReachable = useNetworkStore(
    (state) => state.serverReachable,
  );
  const networkCheckedAt = useNetworkStore((state) => state.serverCheckedAt);
  const isOffline = useMemo(
    () => isServerKnownUnreachable() || !networkConnected,
    // Internet reachability is not the same as AoiTalk reachability.  A LAN
    // server must remain usable while Android reports online=false.
    [networkCheckedAt, networkConnected, networkServerReachable],
  );
  const rawParams = useLocalSearchParams<{
    source?: string;
    path?: string;
    name?: string;
  }>();
  const identity = parseFilesTextEditorParams(rawParams);
  const projectPath =
    identity?.source === "server" ? parseProjectFilesPath(identity.path) : null;
  const usesProjectNamespace =
    identity?.source === "server" && isProjectFilesNamespacePath(identity.path);
  const project = projectPath
    ? projects.find(
        (candidate) =>
          candidate.id.toLowerCase() === projectPath.projectId.toLowerCase(),
      ) ?? null
    : null;
  const projectCanWrite = Boolean(
    projectPath?.relativePath &&
      project &&
      getProjectCapabilities(project, user).canWrite,
  );
  const canUseServerSave =
    identity?.source !== "server" ||
    (isAuthenticated &&
      !isOffline &&
      (!usesProjectNamespace || projectCanWrite));

  const [sessionKey, setSessionKey] = useState(0);
  const [initialContent, setInitialContent] = useState("");
  const [loading, setLoading] = useState(Boolean(identity));
  const [readError, setReadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);
  const savingRef = useRef(false);
  const saveGenerationRef = useRef(0);
  const contentRef = useRef("");
  const mountedRef = useRef(true);
  const navigationRequestedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const handleBack = useCallback(() => {
    // Native-stack transitions can leave this screen mounted briefly.  Make
    // back idempotent and invalidate an in-flight save so it cannot request a
    // second transition after a manual back.
    if (navigationRequestedRef.current) return;
    navigationRequestedRef.current = true;
    saveGenerationRef.current += 1;
    goBackOrReplace(router, "/(tabs)/filer");
  }, [router]);

  useEffect(() => {
    navigationRequestedRef.current = false;
    // Invalidate any in-flight save before handling a new set of route
    // parameters, including an invalid route.  Otherwise a save started for
    // the previous file could resolve after the route changes and navigate
    // away from the replacement editor.
    saveGenerationRef.current += 1;

    if (!identity) {
      setLoading(false);
      setReadError("ファイル情報が不正です。");
      return;
    }

    let cancelled = false;
    const { source, path } = identity;

    // Do not start a server request without a physical network path.  Local
    // files remain available regardless of NetInfo state.
    if (source === "server" && !networkConnected) {
      setLoading(false);
      setReadError("オフラインのためファイルを読み込めません。");
      return () => {
        cancelled = true;
      };
    }

    savingRef.current = false;
    setSaving(false);
    setLoading(true);
    setReadError(null);
    setSaveError(null);
    setInitialContent("");
    contentRef.current = "";
    setSessionKey((key) => key + 1);

    void filesApi
      .readText(source, path)
      .then((text) => {
        if (cancelled || !mountedRef.current) return;
        setInitialContent(text);
        contentRef.current = text;
      })
      .catch((error) => {
        if (cancelled || !mountedRef.current) return;
        setReadError(
          error instanceof Error
            ? error.message
            : "テキスト読み込みに失敗しました。",
        );
      })
      .finally(() => {
        if (!cancelled && mountedRef.current) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [identity?.source, identity?.path, networkConnected, reloadNonce]);

  const handleSave = useCallback(async () => {
    if (
      !identity ||
      loading ||
      savingRef.current ||
      readError ||
      !canUseServerSave
    ) {
      return;
    }

    const saveGeneration = saveGenerationRef.current;
    const { source, path } = identity;
    const content = contentRef.current;

    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    let saveSucceeded = false;
    try {
      await filesApi.saveText(source, path, content);
      saveSucceeded = true;
    } catch (error) {
      if (
        mountedRef.current &&
        saveGenerationRef.current === saveGeneration
      ) {
        setSaveError(
          error instanceof Error ? error.message : "保存に失敗しました。",
        );
      }
    } finally {
      if (
        mountedRef.current &&
        saveGenerationRef.current === saveGeneration
      ) {
        savingRef.current = false;
        setSaving(false);
      }
    }

    // Navigation is deliberately sequenced after the awaited save.  Keep the
    // generation/mounted guards so a stale request cannot close a replacement
    // editor, and failed saves remain on this screen for retry.
    if (
      saveSucceeded &&
      mountedRef.current &&
      saveGenerationRef.current === saveGeneration
    ) {
      handleBack();
    }
  }, [canUseServerSave, handleBack, identity, loading, readError]);

  const handleRetryRead = useCallback(() => {
    if (!identity) return;
    setReloadNonce((n) => n + 1);
  }, [identity]);

  const title = identity?.name ?? "Editor";
  const subtitle = identity ? SOURCE_LABELS[identity.source] : undefined;
  const canSave =
    Boolean(identity) &&
    !loading &&
    !readError &&
    !saving &&
    canUseServerSave;

  return (
    <View style={styles.container}>
      <ScreenHeader
        title={title}
        subtitle={subtitle}
        onBack={handleBack}
        right={
          <Button
            mode="text"
            textColor="#7c3aed"
            disabled={!canSave}
            loading={saving}
            onPress={() => void handleSave()}
            accessibilityLabel="保存"
          >
            保存
          </Button>
        }
      />

      {loading ? (
        <View style={styles.centered}>
          <ActivityIndicator size="small" color="#7c3aed" />
        </View>
      ) : readError ? (
        <View style={styles.centered}>
          <Text style={styles.errorText}>{readError}</Text>
          {identity ? (
            <Button
              mode="outlined"
              textColor="#cdd6f4"
              onPress={handleRetryRead}
              style={styles.retryButton}
            >
              再試行
            </Button>
          ) : null}
        </View>
      ) : (
        <View style={styles.editorArea}>
          {saveError ? (
            <Text style={styles.saveErrorText}>{saveError}</Text>
          ) : null}
          <ThemedTextInput
            key={`editor-${sessionKey}`}
            mode="flat"
            multiline
            defaultValue={initialContent}
            cursorColor="#ffffff"
            onChangeText={(text) => {
              contentRef.current = text;
            }}
            editable={!saving}
            style={styles.editorInput}
            contentStyle={styles.editorInputContent}
            underlineColor="transparent"
            activeUnderlineColor="transparent"
            textColor="#cdd6f4"
            autoCorrect={false}
            autoCapitalize="none"
            textAlignVertical={Platform.OS === "android" ? "top" : undefined}
          />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: "#11111b",
  },
  centered: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    padding: 16,
    gap: 12,
  },
  errorText: {
    color: "#f38ba8",
    fontSize: 15,
    textAlign: "center",
    lineHeight: 22,
  },
  editorArea: {
    flex: 1,
    padding: 12,
  },
  saveErrorText: {
    color: "#f38ba8",
    fontSize: 14,
    marginBottom: 8,
    lineHeight: 20,
  },
  retryButton: {
    borderColor: "#45475a",
  },
  editorInput: {
    flex: 1,
    backgroundColor: "#1e1e2e",
    fontSize: 16,
    lineHeight: 24,
  },
  editorInputContent: {
    paddingHorizontal: 12,
    paddingVertical: 12,
    textAlignVertical: "top",
  },
});
