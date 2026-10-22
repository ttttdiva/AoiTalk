import React, { useEffect, useState } from "react";
import { StyleSheet, View } from "react-native";
import { Divider, RadioButton, Switch, Text } from "react-native-paper";
import { ScreenShell } from "../../../components/screen-primitives";
import { taskApi } from "../../../lib/task-api";
import {
  DEFAULT_AUDIO_PLAYER_SETTINGS,
  getAudioPlayerSettings,
  serializeAudioPlayerSettings,
  type AudioPlayerSettings,
} from "../../../lib/audio-player-settings";

export default function AudioSettingsScreen() {
  const [settings, setSettings] = useState<AudioPlayerSettings>(
    DEFAULT_AUDIO_PLAYER_SETTINGS,
  );
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void taskApi
      .getUserSettings()
      .then((value) => setSettings(getAudioPlayerSettings(value)));
  }, []);

  const update = async (patch: Partial<AudioPlayerSettings>) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    setSaving(true);
    try {
      const saved = await taskApi.updateUserSettings({
        audio_player: serializeAudioPlayerSettings(next),
      });
      setSettings(getAudioPlayerSettings(saved));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ScreenShell
      scroll
      style={styles.container}
      contentContainerStyle={styles.content}
    >
      <Text style={styles.description}>
        曲終了時は常に次の曲を再生します。ここでは次曲の選び方を設定します。
      </Text>

      <Text style={styles.sectionLabel}>再生範囲</Text>
      <RadioButton.Group
        value={settings.playbackScope}
        onValueChange={(value) =>
          void update({
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
          disabled={saving}
        />
        <RadioButton.Item
          label="フォルダを跨いで続ける"
          value="global_next"
          labelStyle={styles.radioLabel}
          color="#7c3aed"
          uncheckedColor="#585b70"
          disabled={saving}
        />
      </RadioButton.Group>

      <Divider style={styles.divider} />

      <View style={styles.row}>
        <View style={styles.text}>
          <Text style={styles.label}>シャッフル</Text>
          <Text style={styles.hint}>再生範囲内からランダムに選びます。</Text>
        </View>
        <Switch
          value={settings.shuffle}
          disabled={saving}
          onValueChange={(value) => void update({ shuffle: value })}
        />
      </View>

      <View style={styles.row}>
        <View style={styles.text}>
          <Text style={styles.label}>1曲リピート</Text>
          <Text style={styles.hint}>同じ曲を繰り返し再生します。</Text>
        </View>
        <Switch
          value={settings.repeatOne}
          disabled={saving}
          onValueChange={(value) => void update({ repeatOne: value })}
        />
      </View>
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#10101b" },
  content: { padding: 16, gap: 8 },
  description: {
    color: "#a9a7c5",
    fontSize: 13,
    lineHeight: 19,
    marginBottom: 8,
  },
  sectionLabel: {
    color: "#8f8aa8",
    fontSize: 12,
    fontWeight: "600",
    marginTop: 4,
  },
  radioLabel: { color: "#f0efff", fontSize: 14 },
  divider: { backgroundColor: "#26243a", marginVertical: 4 },
  row: {
    minHeight: 64,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    borderBottomWidth: 1,
    borderBottomColor: "#26243a",
    paddingVertical: 10,
  },
  text: { flex: 1 },
  label: { color: "#f0efff", fontSize: 14, fontWeight: "500" },
  hint: { color: "#8f8aa8", fontSize: 12, marginTop: 3 },
});
