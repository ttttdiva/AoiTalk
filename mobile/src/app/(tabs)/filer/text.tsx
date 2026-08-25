import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  Platform,
  StyleSheet,
  View,
} from "react-native";
import { Button, Text, TextInput } from "react-native-paper";
import { useLocalSearchParams, useRouter } from "expo-router";
import { ScreenHeader } from "../../../components/screen-header";
import { SOURCE_LABELS } from "../../../features/files/file-browser-model";
import { parseFilesTextEditorParams } from "../../../features/files/files-text-editor-route";
import { filesApi } from "../../../lib/files-api";
import { goBackOrReplace } from "../../../lib/navigation";

export default function FilesTextEditorScreen() {
  const router = useRouter();
  const rawParams = useLocalSearchParams<{
    source?: string;
    path?: string;
    name?: string;
  }>();
  const identity = parseFilesTextEditorParams(rawParams);

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

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const handleBack = useCallback(() => {
    goBackOrReplace(router, "/(tabs)/filer");
  }, [router]);

  useEffect(() => {
    if (!identity) {
      setLoading(false);
      setReadError("ファイル情報が不正です。");
      return;
    }

    let cancelled = false;
    const { source, path } = identity;

    saveGenerationRef.current += 1;
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
  }, [identity?.source, identity?.path, reloadNonce]);

  const handleSave = useCallback(async () => {
    if (!identity || loading || savingRef.current || readError) return;

    const saveGeneration = saveGenerationRef.current;
    const { source, path } = identity;
    const content = contentRef.current;

    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      await filesApi.saveText(source, path, content);
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
  }, [identity, loading, readError]);

  const handleRetryRead = useCallback(() => {
    if (!identity) return;
    setReloadNonce((n) => n + 1);
  }, [identity]);

  const title = identity?.name ?? "Editor";
  const subtitle = identity ? SOURCE_LABELS[identity.source] : undefined;
  const canSave = Boolean(identity) && !loading && !readError && !saving;

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
          <TextInput
            key={`editor-${sessionKey}`}
            mode="flat"
            multiline
            defaultValue={initialContent}
            onChangeText={(text) => {
              contentRef.current = text;
            }}
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
