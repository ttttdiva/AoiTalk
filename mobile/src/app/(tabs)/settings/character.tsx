import React, { useCallback, useEffect, useState } from "react";
import { Alert, ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import {
  ActivityIndicator,
  Button,
  Chip,
  IconButton,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import { useAuth } from "../../../contexts/AuthContext";
import { conversationsRepo } from "../../../repositories/conversations";
import { characterApi } from "../../../lib/character-api";
import {
  getDefaultCharacterName,
  saveDefaultCharacterName,
} from "../../../lib/preferences";
import type { ManagedCharacter } from "../../../types/api";

export default function SettingsCharacterScreen() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const [characterName, setCharacterName] = useState("default");
  const [characters, setCharacters] = useState<ManagedCharacter[]>([]);
  const [loading, setLoading] = useState(false);
  const [savingDefault, setSavingDefault] = useState(false);

  useEffect(() => {
    void getDefaultCharacterName().then(setCharacterName);
  }, []);

  const loadCharacters = useCallback(async () => {
    if (!isAuthenticated) {
      setCharacters([]);
      return;
    }
    setLoading(true);
    try {
      setCharacters(await characterApi.list());
    } catch (error) {
      Alert.alert(
        "Characters",
        error instanceof Error
          ? error.message
          : "キャラクター一覧の取得に失敗しました。",
      );
    } finally {
      setLoading(false);
    }
  }, [isAuthenticated]);

  useFocusEffect(
    useCallback(() => {
      void loadCharacters();
    }, [loadCharacters]),
  );

  const saveDefault = useCallback(
    async (nextName = characterName) => {
      const normalized = nextName.trim() || "default";
      setSavingDefault(true);
      try {
        await saveDefaultCharacterName(normalized);
        setCharacterName(normalized);
      } finally {
        setSavingDefault(false);
      }
    },
    [characterName],
  );

  const handleToggle = useCallback(
    async (character: ManagedCharacter) => {
      try {
        const updated = await characterApi.toggle(character.id);
        setCharacters((prev) =>
          prev.map((item) => (item.id === updated.id ? updated : item)),
        );
      } catch (error) {
        Alert.alert(
          "Characters",
          error instanceof Error
            ? error.message
            : "キャラクター状態の更新に失敗しました。",
        );
      }
    },
    [],
  );

  const handleStartChat = useCallback(
    async (character: ManagedCharacter) => {
      try {
        await saveDefault(character.slug);
        const session = await conversationsRepo.createSession(character.slug);
        router.push(`/(tabs)/chat/${session.id}`);
      } catch (error) {
        Alert.alert(
          "Characters",
          error instanceof Error
            ? error.message
            : "キャラクターチャットを開始できませんでした。",
        );
      }
    },
    [router, saveDefault],
  );

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton
            icon="arrow-left"
            iconColor="#cdd6f4"
            onPress={() => goBackOrReplace(router, '/(tabs)/settings')}
          />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              Characters
            </Text>
            <Text style={styles.headerSubtext}>
              既定キャラクターとサーバー側キャラクター管理
            </Text>
          </View>
        </View>
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>Default Chat Character</Text>
        <Text style={styles.helperText}>
          新しいチャットで使う character slug を保存します。
        </Text>
        <TextInput
          mode="outlined"
          value={characterName}
          onChangeText={setCharacterName}
          style={styles.input}
          autoCapitalize="none"
          autoCorrect={false}
        />
        <Button
          mode="contained"
          buttonColor="#7c3aed"
          textColor="#cdd6f4"
          onPress={() => {
            void saveDefault();
          }}
          loading={savingDefault}
        >
          Save Default
        </Button>
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <View style={styles.sectionHeader}>
          <Text style={styles.cardTitle}>Server Characters</Text>
          <IconButton
            icon="refresh"
            iconColor="#cdd6f4"
            size={20}
            onPress={() => {
              void loadCharacters();
            }}
          />
        </View>
        {!isAuthenticated ? (
          <Text style={styles.helperText}>
            キャラクター一覧・有効化切替・RP開始はサーバーログイン中のみ利用できます。
          </Text>
        ) : loading ? (
          <ActivityIndicator color="#7c3aed" style={styles.loader} />
        ) : characters.length === 0 ? (
          <Text style={styles.emptyText}>キャラクターがありません。</Text>
        ) : (
          characters.map((character) => {
            const enabled = character.is_enabled !== false;
            return (
              <Surface key={character.id} style={styles.characterCard} elevation={0}>
                <View style={styles.characterTop}>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.characterName}>{character.name}</Text>
                    <Text style={styles.characterSlug}>{character.slug}</Text>
                  </View>
                  <Switch
                    value={enabled}
                    onValueChange={() => {
                      void handleToggle(character);
                    }}
                  />
                </View>
                <View style={styles.metaRow}>
                  <Chip compact style={styles.metaChip} textStyle={styles.metaChipText}>
                    {character.character_type || "assistant"}
                  </Chip>
                  {character.model ? (
                    <Chip compact style={styles.metaChip} textStyle={styles.metaChipText}>
                      {character.model}
                    </Chip>
                  ) : null}
                </View>
                {character.description || character.personality_summary ? (
                  <Text style={styles.characterDescription} numberOfLines={3}>
                    {character.description || character.personality_summary}
                  </Text>
                ) : null}
                <View style={styles.actions}>
                  <Button
                    compact
                    mode="outlined"
                    textColor="#89b4fa"
                    onPress={() => {
                      void saveDefault(character.slug);
                    }}
                  >
                    Set Default
                  </Button>
                  <Button
                    compact
                    mode="outlined"
                    textColor="#a6e3a1"
                    disabled={!enabled}
                    onPress={() => {
                      void handleStartChat(character);
                    }}
                  >
                    Start Chat
                  </Button>
                </View>
              </Surface>
            );
          })
        )}
      </Surface>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { paddingBottom: 32 },
  header: {
    paddingTop: 52,
    paddingHorizontal: 8,
    paddingBottom: 16,
    backgroundColor: "#1e1e2e",
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtext: { color: "#a6adc8", marginTop: 2 },
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
    marginBottom: 10,
  },
  helperText: { color: "#a6adc8", fontSize: 13, lineHeight: 19 },
  input: { marginTop: 10, marginBottom: 12 },
  sectionHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  loader: { marginVertical: 20 },
  emptyText: { color: "#a6adc8", textAlign: "center", paddingVertical: 20 },
  characterCard: {
    backgroundColor: "#11111b",
    borderRadius: 10,
    padding: 12,
    marginTop: 10,
    borderWidth: 1,
    borderColor: "#313244",
  },
  characterTop: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  characterName: { color: "#cdd6f4", fontSize: 15, fontWeight: "700" },
  characterSlug: { color: "#a6adc8", fontSize: 12, marginTop: 2 },
  metaRow: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 10 },
  metaChip: { backgroundColor: "#313244" },
  metaChipText: { color: "#cdd6f4", fontSize: 11 },
  characterDescription: {
    color: "#a6adc8",
    fontSize: 13,
    lineHeight: 18,
    marginTop: 10,
  },
  actions: {
    flexDirection: "row",
    justifyContent: "flex-end",
    gap: 8,
    marginTop: 12,
  },
});
