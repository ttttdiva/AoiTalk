import React, { useMemo, useState } from "react";
import {
  Pressable,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Button,
  Dialog,
  Portal,
  Surface,
  Text,
} from "react-native-paper";
import { characterApi } from "../../lib/character-api";
import {
  isApiConnectionError,
  isApiHttpError,
} from "../../lib/api-client";
import type {
  ConversationSession,
  ManagedCharacter,
} from "../../types/api";
import type { RunState } from "./models";
import { getCharacterChangeAvailability } from "./character-session";

type CharacterSelectorProps = {
  session: ConversationSession | null;
  runState: RunState;
  onChange: (slug: string) => Promise<void>;
};

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) {
    return `${fallback}: ${error.message.trim()}`;
  }
  return fallback;
}

function canKeepOfflineCharacters(error: unknown): boolean {
  if (isApiConnectionError(error)) return true;
  return isApiHttpError(error) && error.status >= 500 && error.status < 600;
}

export function CharacterSelector({
  session,
  runState,
  onChange,
}: CharacterSelectorProps) {
  const [visible, setVisible] = useState(false);
  const [characters, setCharacters] = useState<ManagedCharacter[]>([]);
  const [loading, setLoading] = useState(false);
  const [savingSlug, setSavingSlug] = useState<string | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const availability = getCharacterChangeAvailability(session, runState);
  const currentSlug = session?.character_name?.trim() || "";
  const currentCharacter = useMemo(
    () => characters.find((character) => character.slug === currentSlug),
    [characters, currentSlug],
  );
  const currentLabel = currentCharacter?.name || currentSlug || "未設定";

  const open = () => {
    setVisible(true);
    setDialogError(null);
    if (!availability.allowed) return;
    setLoading(true);
    void (async () => {
      let offlineCharacters: ManagedCharacter[];
      try {
        offlineCharacters = await characterApi.getOfflineList(
          false,
          currentSlug,
        );
        setCharacters(offlineCharacters);
        setLoading(false);
      } catch (error) {
        setLoading(false);
        setDialogError(
          errorMessage(error, "キャラクター一覧を取得できませんでした"),
        );
        return;
      }

      // 端末内の一覧を表示した後で、オンラインならサーバーの最新一覧へ更新する。
      try {
        const onlineCharacters = await characterApi.list();
        if (onlineCharacters.length > 0) setCharacters(onlineCharacters);
      } catch (error) {
        if (!canKeepOfflineCharacters(error)) {
          setDialogError(
            errorMessage(error, "キャラクター一覧を取得できませんでした"),
          );
        }
      }
    })();
  };

  const selectCharacter = async (character: ManagedCharacter) => {
    if (
      character.is_enabled === false ||
      character.slug === currentSlug ||
      savingSlug
    ) {
      return;
    }
    const latestAvailability = getCharacterChangeAvailability(session, runState);
    if (!latestAvailability.allowed) {
      setDialogError(latestAvailability.reason);
      return;
    }
    setDialogError(null);
    setSavingSlug(character.slug);
    try {
      await onChange(character.slug);
      setVisible(false);
    } catch (error) {
      setDialogError(
        errorMessage(error, "キャラクターを変更できませんでした"),
      );
    } finally {
      setSavingSlug(null);
    }
  };

  return (
    <>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`キャラクターを選択。現在は${currentLabel}`}
        onPress={open}
        style={[
          styles.chip,
          !availability.allowed ? styles.chipUnavailable : null,
        ]}
      >
        <Text style={styles.chipLabel} numberOfLines={1}>
          キャラ: {currentLabel}
        </Text>
        <Text style={styles.chevron}>⌄</Text>
      </Pressable>

      <Portal>
        <Dialog
          visible={visible}
          onDismiss={() => {
            if (!savingSlug) setVisible(false);
          }}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            キャラクターを変更
          </Dialog.Title>
          <Dialog.Content>
            {!availability.allowed ? (
              <Surface style={styles.reasonCard} elevation={0}>
                <Text style={styles.reasonText}>{availability.reason}</Text>
              </Surface>
            ) : null}
            {dialogError ? (
              <Text accessibilityRole="alert" style={styles.errorText}>
                {dialogError}
              </Text>
            ) : null}
            {availability.allowed && !loading ? (
              <Text style={styles.offlineHint}>
                サーバー未接続でも、端末内のキャラクターを選択できます。
              </Text>
            ) : null}
            {loading ? (
              <ActivityIndicator
                accessibilityLabel="キャラクター一覧を読み込み中"
                color="#7c3aed"
              />
            ) : null}
            {availability.allowed && !loading ? (
              <ScrollView
                style={styles.list}
                contentContainerStyle={styles.listContent}
              >
                {characters.map((character) => {
                  const selected = character.slug === currentSlug;
                  const enabled = character.is_enabled !== false;
                  const saving = savingSlug === character.slug;
                  return (
                    <Pressable
                      key={character.id}
                      accessibilityRole="radio"
                      accessibilityLabel={`${character.name} (${character.slug})`}
                      accessibilityState={{
                        selected,
                        disabled: !enabled || Boolean(savingSlug),
                      }}
                      disabled={!enabled || Boolean(savingSlug)}
                      onPress={() => void selectCharacter(character)}
                    >
                      <Surface
                        style={[
                          styles.characterCard,
                          selected ? styles.characterCardSelected : null,
                          !enabled ? styles.characterCardDisabled : null,
                        ]}
                        elevation={0}
                      >
                        <View style={styles.characterText}>
                          <Text style={styles.characterName}>
                            {character.name}
                          </Text>
                          <Text style={styles.characterMeta}>
                            {enabled ? character.slug : `${character.slug} · 利用不可`}
                          </Text>
                        </View>
                        <Text style={styles.selectionState}>
                          {saving ? "変更中…" : selected ? "✓ 使用中" : ""}
                        </Text>
                      </Surface>
                    </Pressable>
                  );
                })}
                {characters.length === 0 && !dialogError ? (
                  <Text style={styles.emptyText}>
                    選択できるキャラクターがありません。
                  </Text>
                ) : null}
              </ScrollView>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button
              textColor="#a6adc8"
              disabled={Boolean(savingSlug)}
              onPress={() => setVisible(false)}
            >
              閉じる
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </>
  );
}

const styles = StyleSheet.create({
  chip: {
    maxWidth: 172,
    flexDirection: "row",
    alignItems: "center",
    borderRadius: 12,
    backgroundColor: "#313244",
    paddingHorizontal: 9,
    paddingVertical: 5,
  },
  chipUnavailable: { opacity: 0.72 },
  chipLabel: {
    minWidth: 0,
    flexShrink: 1,
    color: "#cdd6f4",
    fontSize: 11,
    fontWeight: "700",
  },
  chevron: { color: "#a6adc8", fontSize: 12, marginLeft: 3 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  reasonCard: {
    backgroundColor: "#2d2537",
    borderColor: "#c084fc",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
  },
  reasonText: { color: "#e6d5f7", fontSize: 13, lineHeight: 18 },
  errorText: { color: "#f38ba8", fontSize: 12, marginBottom: 8 },
  offlineHint: { color: "#a6adc8", fontSize: 12, marginBottom: 8 },
  list: { maxHeight: 420 },
  listContent: { gap: 8, paddingVertical: 4 },
  characterCard: {
    minHeight: 58,
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: "#181825",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 9,
  },
  characterCardSelected: {
    borderColor: "#89b4fa",
    backgroundColor: "#20283a",
  },
  characterCardDisabled: { opacity: 0.52 },
  characterText: { flex: 1 },
  characterName: { color: "#cdd6f4", fontSize: 14, fontWeight: "700" },
  characterMeta: { color: "#a6adc8", fontSize: 11, marginTop: 2 },
  selectionState: { color: "#89b4fa", fontSize: 12, fontWeight: "700" },
  emptyText: { color: "#a6adc8", fontSize: 12, paddingVertical: 18 },
});
