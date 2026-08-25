import React, { useCallback, useMemo, useRef, useState } from "react";
import { Alert, Pressable, StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import {
  ActivityIndicator,
  Button,
  Chip,
  Icon,
  IconButton,
  Surface,
  Text,
} from "react-native-paper";
import { goBackOrReplace } from "../../../lib/navigation";
import { ScreenHeader } from "../../../components/screen-header";
import { ScreenShell } from "../../../components/screen-primitives";
import { useAuth } from "../../../contexts/AuthContext";
import { characterApi } from "../../../lib/character-api";
import {
  getCurrentCharacterSlug,
  saveCurrentCharacterSlug,
} from "../../../lib/preferences";
import {
  createCurrentCharacterSession,
  findSelectedCharacter,
  isCharacterEnabled,
} from "../../../features/characters/current-character";
import type { ManagedCharacter } from "../../../types/api";

export default function SettingsCharacterScreen() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const [currentSlug, setCurrentSlug] = useState<string | null>(null);
  const [characters, setCharacters] = useState<ManagedCharacter[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [savingSlug, setSavingSlug] = useState<string | null>(null);
  const [startingChat, setStartingChat] = useState(false);
  const characterLoadVersionRef = useRef(0);
  const focusReadVersionRef = useRef(0);
  const selectionMutationVersionRef = useRef(0);
  const committedSelectionVersionRef = useRef(0);

  const loadCharacters = useCallback(async () => {
    const loadVersion = ++characterLoadVersionRef.current;
    setLoading(true);

    const savedSlug = await getCurrentCharacterSlug().catch(() => null);
    const offlineCharacters = await characterApi
      .getOfflineList(false, savedSlug)
      .catch(() => []);
    if (loadVersion !== characterLoadVersionRef.current) return;

    // 一覧表示は通信結果を待たず、端末内の定義・cacheから先に確定する。
    setCharacters(offlineCharacters);
    setLoading(false);

    if (!isAuthenticated) return;

    // ログイン中だけサーバーの最新一覧でバックグラウンド更新する。
    try {
      const list = await characterApi.list();
      if (loadVersion === characterLoadVersionRef.current) {
        setCharacters(list.length > 0 ? list : offlineCharacters);
      }
    } catch (error) {
      if (
        loadVersion === characterLoadVersionRef.current &&
        offlineCharacters.length === 0
      ) {
        setCharacters(null);
        Alert.alert(
          "現在のキャラクター",
          error instanceof Error
            ? error.message
            : "キャラクター一覧の取得に失敗しました。",
        );
      }
    }
  }, [isAuthenticated]);

  useFocusEffect(
    useCallback(() => {
      const focusReadVersion = ++focusReadVersionRef.current;
      const mutationVersion = selectionMutationVersionRef.current;
      const committedMutationVersion = committedSelectionVersionRef.current;
      void getCurrentCharacterSlug().then((slug) => {
        if (
          focusReadVersion === focusReadVersionRef.current &&
          mutationVersion === selectionMutationVersionRef.current &&
          mutationVersion === committedMutationVersion
        ) {
          setCurrentSlug(slug);
        }
      });
      void loadCharacters();
      return () => {
        focusReadVersionRef.current += 1;
        characterLoadVersionRef.current += 1;
      };
    }, [loadCharacters]),
  );

  const selectedCharacter = useMemo(
    () =>
      characters
        ? findSelectedCharacter(characters, currentSlug)
        : null,
    [characters, currentSlug],
  );

  const selectionNeedsAttention =
    characters !== null && !selectedCharacter;
  const canStartChat =
    Boolean(currentSlug) &&
    (characters === null || Boolean(selectedCharacter));

  const handleSelect = useCallback(async (character: ManagedCharacter) => {
    if (!isCharacterEnabled(character)) return;
    const mutationVersion = ++selectionMutationVersionRef.current;
    setSavingSlug(character.slug);
    try {
      await saveCurrentCharacterSlug(character.slug);
      if (mutationVersion === selectionMutationVersionRef.current) {
        committedSelectionVersionRef.current = mutationVersion;
        setCurrentSlug(character.slug);
      }
    } catch (error) {
      if (mutationVersion === selectionMutationVersionRef.current) {
        committedSelectionVersionRef.current = mutationVersion;
      }
      Alert.alert(
        "現在のキャラクター",
        error instanceof Error
          ? error.message
          : "キャラクターを保存できませんでした。",
      );
    } finally {
      if (mutationVersion === selectionMutationVersionRef.current) {
        setSavingSlug(null);
      }
    }
  }, []);

  const handleStartChat = useCallback(async () => {
    setStartingChat(true);
    try {
      const session = await createCurrentCharacterSession(undefined, {
        localFirst: true,
      });
      router.push(`/(tabs)/chat/${session.id}`);
    } catch (error) {
      Alert.alert(
        "現在のキャラクター",
        error instanceof Error
          ? error.message
          : "チャットを開始できませんでした。",
      );
    } finally {
      setStartingChat(false);
    }
  }, [router]);

  return (
    <ScreenShell
      scroll
      style={styles.container}
      contentContainerStyle={styles.content}
      header={
        <ScreenHeader
          title="現在のキャラクター"
          subtitle="新しく始める通常チャットで使用するキャラクター"
          onBack={() => goBackOrReplace(router, "/(tabs)/settings")}
        />
      }
    >

      <Surface style={styles.card} elevation={0}>
        <View style={styles.sectionHeader}>
          <View style={styles.sectionCopy}>
            <Text style={styles.cardTitle}>キャラクターを選択</Text>
            <Text style={styles.helperText}>
              利用可能なカードをタップすると、以後の新規チャットに使用します。
            </Text>
          </View>
          {isAuthenticated ? (
            <IconButton
              icon="refresh"
              accessibilityLabel="キャラクター一覧を再読み込み"
              iconColor="#cdd6f4"
              size={20}
              onPress={() => {
                void loadCharacters();
              }}
            />
          ) : null}
        </View>

        {selectionNeedsAttention ? (
          <Surface style={styles.warning} elevation={0}>
            <Icon source="alert-circle-outline" size={20} color="#f9e2af" />
            <Text style={styles.warningText}>
              {currentSlug
                ? `保存されていた「${currentSlug}」は現在利用できません。代わりのキャラクターを選択してください。`
                : "現在のキャラクターが未選択です。利用可能なキャラクターを選択してください。"}
            </Text>
          </Surface>
        ) : null}

        <Text style={styles.helperText}>
          サーバー未接続でも、端末内のキャラクターと取得済みキャッシュから
          選択できます。サーバーに接続すると追加キャラクターを更新します。
        </Text>

        {loading ? (
          <ActivityIndicator color="#7c3aed" style={styles.loader} />
        ) : characters === null ? (
          <Text style={styles.emptyText}>
            一覧を確認できません。通信状態を確認して再読み込みしてください。
          </Text>
        ) : characters.length === 0 ? (
          <Text style={styles.emptyText}>
            選択できるキャラクターがありません。
          </Text>
        ) : (
          characters.map((character) => {
            const enabled = isCharacterEnabled(character);
            const selected =
              enabled && character.slug === selectedCharacter?.slug;
            return (
              <Pressable
                key={character.id}
                accessibilityRole="radio"
                accessibilityLabel={`${character.name} (${character.slug})`}
                accessibilityState={{
                  checked: selected,
                  disabled: !enabled,
                }}
                disabled={!enabled || savingSlug !== null}
                onPress={() => {
                  void handleSelect(character);
                }}
                style={({ pressed }) => pressed && styles.pressed}
              >
                <Surface
                  style={[
                    styles.characterCard,
                    selected && styles.characterCardSelected,
                    !enabled && styles.characterCardDisabled,
                  ]}
                  elevation={0}
                >
                  <View style={styles.characterTop}>
                    <View style={styles.characterCopy}>
                      <Text style={styles.characterName}>{character.name}</Text>
                      <Text style={styles.characterSlug}>{character.slug}</Text>
                    </View>
                    {savingSlug === character.slug ? (
                      <ActivityIndicator size={20} color="#a6e3a1" />
                    ) : selected ? (
                      <Icon source="check-circle" size={24} color="#a6e3a1" />
                    ) : null}
                  </View>
                  <View style={styles.metaRow}>
                    <Chip
                      compact
                      style={[
                        styles.metaChip,
                        enabled ? styles.enabledChip : styles.disabledChip,
                      ]}
                      textStyle={styles.metaChipText}
                    >
                      {selected ? "使用中" : enabled ? "利用可能" : "利用不可"}
                    </Chip>
                    <Chip
                      compact
                      style={styles.metaChip}
                      textStyle={styles.metaChipText}
                    >
                      {character.character_type || "assistant"}
                    </Chip>
                    {character.model ? (
                      <Chip
                        compact
                        style={styles.metaChip}
                        textStyle={styles.metaChipText}
                      >
                        {character.model}
                      </Chip>
                    ) : null}
                  </View>
                  {character.description || character.personality_summary ? (
                    <Text
                      style={styles.characterDescription}
                      numberOfLines={3}
                    >
                      {character.description || character.personality_summary}
                    </Text>
                  ) : null}
                </Surface>
              </Pressable>
            );
          })
        )}
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>新しいチャット</Text>
        <Text style={styles.helperText}>
          {selectedCharacter
            ? `「${selectedCharacter.name}」を使って通常チャットを開始します。`
            : currentSlug && characters === null
              ? `保存済みの「${currentSlug}」を使って開始します。一覧を取得できた場合は利用可能か再確認します。`
              : "利用可能なキャラクターを選択すると開始できます。"}
        </Text>
        <Button
          mode="contained"
          icon="message-plus-outline"
          buttonColor="#7c3aed"
          textColor="#cdd6f4"
          style={styles.startButton}
          disabled={!canStartChat || savingSlug !== null}
          loading={startingChat}
          onPress={() => {
            void handleStartChat();
          }}
        >
          現在のキャラクターでチャットを開始
        </Button>
      </Surface>
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { paddingBottom: 32 },
  card: {
    backgroundColor: "#1e1e2e",
    borderRadius: 12,
    padding: 16,
    marginHorizontal: 16,
    marginTop: 16,
  },
  cardTitle: {
    color: "#7c3aed",
    fontSize: 13,
    fontWeight: "700",
    marginBottom: 8,
  },
  helperText: { color: "#a6adc8", fontSize: 13, lineHeight: 19 },
  sectionHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  sectionCopy: { flex: 1 },
  loader: { marginVertical: 20 },
  emptyText: { color: "#a6adc8", textAlign: "center", paddingVertical: 20 },
  warning: {
    backgroundColor: "#302d26",
    borderColor: "#f9e2af",
    borderWidth: 1,
    borderRadius: 10,
    padding: 12,
    marginTop: 14,
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 8,
  },
  warningText: { color: "#f9e2af", fontSize: 13, lineHeight: 19, flex: 1 },
  pressed: { opacity: 0.82 },
  characterCard: {
    backgroundColor: "#11111b",
    borderRadius: 10,
    padding: 12,
    marginTop: 10,
    borderWidth: 1,
    borderColor: "#313244",
  },
  characterCardSelected: {
    borderColor: "#a6e3a1",
    backgroundColor: "#17221d",
  },
  characterCardDisabled: { opacity: 0.5 },
  characterTop: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  characterCopy: { flex: 1 },
  characterName: { color: "#cdd6f4", fontSize: 15, fontWeight: "700" },
  characterSlug: { color: "#a6adc8", fontSize: 12, marginTop: 2 },
  metaRow: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 10 },
  metaChip: { backgroundColor: "#313244" },
  enabledChip: { backgroundColor: "#21412e" },
  disabledChip: { backgroundColor: "#492832" },
  metaChipText: { color: "#cdd6f4", fontSize: 11 },
  characterDescription: {
    color: "#a6adc8",
    fontSize: 13,
    lineHeight: 18,
    marginTop: 10,
  },
  startButton: { marginTop: 14 },
});
